from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ezmcd.coupons import coupon_signature
from ezmcd.demo import DemoProvider, setup_demo
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, ItemSelection, SHANGHAI, fingerprint
from ezmcd.products import verify_cart_contents
from ezmcd.service import Service
from ezmcd.storage import Store


class CartRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        setup_demo(self.store)
        self.service = Service(self.store)
        self.account, self.provider = self.service.provider("demo")
        self.provider.write_cart({"quantity": 0})
        when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account="demo", store_code="9000001", pickup_at=when.isoformat(), max_amount_cny="20")
        self.items = [ItemSelection(product_code="100001", quantity=1)]

    def add(self, key="meal", args=None, items=None):
        return self.service.add_configured_items(args or self.args, items or self.items, key)

    def counts(self):
        with self.store.connect() as db:
            return dict(db.execute("SELECT stage,count(*) FROM writes GROUP BY stage").fetchall())

    def clear(self, args=None):
        args = args or self.args
        viewed = self.service.cart(args)
        return self.service.clear_cart(args, viewed["cart_ref"], "clear-" + viewed["cart_ref"])

    def test_success_restart_and_key_replay_only_compare_saved_intent(self):
        first = self.add()
        self.assertEqual(first["state"], "MATCHED")
        self.assertTrue(first["write_receipt_verified"])
        self.assertTrue(first["add_attempted"])
        self.service = Service(Store(self.root))
        with patch.object(DemoProvider, "configured_cart_draft") as draft, patch.object(DemoProvider, "write_cart") as write:
            repeated = self.add()
            recovered = self.service.cart_add_status("demo")
            draft.assert_not_called()
            write.assert_not_called()
        self.assertEqual(repeated["operation_id"], first["operation_id"])
        self.assertEqual(recovered["operation_id"], first["operation_id"])
        self.assertFalse(repeated["add_attempted"])
        self.assertEqual(self.counts(), {"cart": 1})
        self.assertEqual(self.provider.read()["orders"], {})
        self.assertEqual(self.provider.wallet()["balance_cents"], 10000)

    def test_reusing_key_with_changed_items_store_time_budget_or_method_stops(self):
        self.add()
        for changes in ({"store_code": "9000002"}, {"pickup_at": self.args.pickup_at.replace("10:15", "10:20")}, {"max_amount_cny": "15.00"}):
            with self.assertRaisesRegex(EzMcdError, "cart_key_reused"):
                self.add(args=self.args.model_copy(update=changes))
        with self.assertRaisesRegex(EzMcdError, "cart_key_reused"):
            self.add(items=[ItemSelection(product_code="100001", quantity=2)])
        with self.assertRaisesRegex(EzMcdError, "cart_key_reused"):
            self.service.add_simple_items(self.args, self.items, "meal")
        self.assertEqual(self.counts(), {"cart": 1})

    def test_lost_success_response_reconciles_without_resending(self):
        original = DemoProvider.write_cart
        def lost(provider, body):
            original(provider, body)
            raise EzMcdError("transport_unknown_do_not_retry_writes")
        with patch.object(DemoProvider, "write_cart", lost):
            result = self.add()
        self.assertEqual(result["state"], "MATCHED")
        self.assertFalse(result["write_receipt_verified"])
        self.assertEqual(result["next_action"], "checkout_quote")
        with patch.object(DemoProvider, "write_cart") as write:
            self.add()
            write.assert_not_called()
        self.assertEqual(self.counts(), {"cart": 1})

    def test_lost_response_with_empty_cart_blocks_new_keys_until_explicit_review_clear(self):
        with patch.object(DemoProvider, "write_cart", side_effect=EzMcdError("synthetic_timeout")):
            first = self.add()
        self.assertEqual(first["state"], "EMPTY")
        self.assertNotEqual(first["next_action"], "checkout_quote")
        with self.assertRaisesRegex(EzMcdError, "cart_addition_unresolved"):
            self.add("new-key")
        repeated = self.add()
        self.assertFalse(repeated["add_attempted"])
        self.clear()
        result = self.add("new-meal-after-review")
        self.assertEqual(result["state"], "MATCHED")
        self.assertEqual(self.counts(), {"cart": 2})

    def test_clear_at_another_store_does_not_resolve_original_unknown(self):
        with patch.object(DemoProvider, "write_cart", side_effect=EzMcdError("synthetic_timeout")):
            self.add()
        self.clear(self.args.model_copy(update={"store_code": "9000002"}))
        with self.assertRaisesRegex(EzMcdError, "cart_addition_unresolved"):
            self.add("new-key")

    def test_changed_or_removed_cart_does_not_restore_old_items(self):
        first = self.add()
        for quantity, state in ((2, "DIFFERENT"), (0, "EMPTY")):
            self.provider.write_cart({"quantity": quantity})
            with patch.object(DemoProvider, "write_cart") as write:
                result = self.add()
                write.assert_not_called()
            self.assertEqual(result["state"], state)
            self.assertTrue(result["ever_matched"])
            self.assertEqual(result["operation_id"], first["operation_id"])

    def test_readback_failure_preserves_record_and_retry_only_reads(self):
        with patch.object(DemoProvider, "cart_state", side_effect=EzMcdError("session_expired_reconnect_locally")):
            first = self.add()
        self.assertEqual(first["state"], "UNAVAILABLE")
        self.assertTrue(first["write_receipt_verified"])
        with patch.object(DemoProvider, "write_cart") as write:
            self.assertEqual(self.add()["state"], "MATCHED")
            write.assert_not_called()
        self.assertEqual(self.counts(), {"cart": 1})

    def test_draft_failure_has_no_claim_and_same_key_can_be_prepared_later(self):
        with patch.object(DemoProvider, "configured_cart_draft", side_effect=EzMcdError("sold_out")):
            with self.assertRaisesRegex(EzMcdError, "sold_out"):
                self.add()
        self.assertEqual(self.counts(), {})
        self.assertIsNone(self.store.cart_addition(self.account["identity"]))
        self.assertEqual(self.add()["state"], "MATCHED")

    def test_journal_failure_prevents_network_write(self):
        with patch.object(self.store, "claim_cart_addition", side_effect=OSError("synthetic disk failure")), \
                patch.object(DemoProvider, "write_cart") as write:
            with self.assertRaises(OSError):
                self.add()
            write.assert_not_called()
        self.assertEqual(self.counts(), {})

    def test_hard_crash_after_claim_and_before_send_is_still_query_only(self):
        with patch.object(DemoProvider, "write_cart", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.add()
        self.service = Service(Store(self.root))
        with patch.object(DemoProvider, "write_cart") as write:
            result = self.service.cart_add_status("demo")
            self.assertEqual(result["state"], "EMPTY")
            self.add()
            write.assert_not_called()
        with self.assertRaisesRegex(EzMcdError, "cart_addition_unresolved"):
            self.add("replacement")

    def test_receipt_save_failure_does_not_resend_successful_add(self):
        with patch.object(self.store, "receipt", side_effect=OSError("synthetic disk failure")):
            first = self.add()
        self.assertEqual(first["state"], "MATCHED")
        self.assertFalse(first["write_receipt_verified"])
        self.assertEqual(self.add()["operation_id"], first["operation_id"])
        self.assertEqual(self.counts(), {"cart": 1})

    def test_legacy_claim_is_checked_before_draft_and_never_recreated(self):
        operation = "cart-" + fingerprint({"identity": self.account["identity"], "key": "meal"})
        self.store.claim(operation, "cart")
        with patch.object(DemoProvider, "configured_cart_draft") as draft:
            with self.assertRaisesRegex(EzMcdError, "legacy_cart_write_query_only"):
                self.add()
            draft.assert_not_called()
        with self.assertRaisesRegex(EzMcdError, "cart_addition_not_found"):
            self.service.cart_add_status("demo", operation)

    def test_account_isolation_and_local_intent_after_revocation(self):
        first = self.add()
        setup_demo(self.store, "other")
        with self.assertRaisesRegex(EzMcdError, "cart_addition_not_found"):
            self.service.cart_add_status("other", first["operation_id"])
        self.store.modify_account("demo", lambda a: a | {"revoked": True})
        result = self.service.cart_add_status("demo")
        self.assertEqual(result["state"], "UNAVAILABLE")
        self.assertEqual(result["query_error"], "account_revoked_reconnect_locally")
        self.assertEqual(result["requested_items"][0]["product_code"], "100001")
        self.assertNotIn("draft", result)
        self.assertNotIn("request_digest", result)

    def test_account_lock_blocks_add_and_recovery(self):
        first = self.add()
        with self.store.lock(self.account["identity"]):
            with self.assertRaisesRegex(EzMcdError, "account_busy"):
                self.add()
            with self.assertRaisesRegex(EzMcdError, "account_busy"):
                self.service.cart_add_status("demo", first["operation_id"])

    def test_cli_recovers_latest_or_specific_addition_without_new_write(self):
        first = self.add()
        for extra in ([], ["--operation-id", first["operation_id"]]):
            result = subprocess.run([sys.executable, "-m", "ezmcd.cli", "--data-dir", str(self.root),
                "cart-status", "demo", *extra], capture_output=True, text=True, check=True)
            value = json.loads(result.stdout)
            self.assertEqual(value["state"], "MATCHED")
            self.assertEqual(value["operation_id"], first["operation_id"])
            self.assertFalse(value["add_attempted"])
        self.assertEqual(self.counts(), {"cart": 1})


class FullCartComparisonTests(unittest.TestCase):
    def setUp(self):
        self.product = {"code": "900001", "type": "7", "quantity": 1, "couponId": "synthetic-coupon",
            "couponCode": "synthetic-code", "promotionId": "synthetic-promotion", "cardId": "synthetic-card", "comboItems": [
                {"round": 1, "comboProducts": [{"code": "100001", "quantity": 1,
                    "customization": {"items": [], "options": [{"code": "111", "key": "0-1", "quantity": 1}]}}]},
                {"round": 2, "comboProducts": [{"code": "200001", "quantity": 1,
                    "modification": {"values": [{"code": "222", "key": "1-1", "quantity": 1}]}}]}]}
        self.row = {"product_code": "900001", "product_type": "7", "quantity": 1, "configuration": {},
            "components": deepcopy(self.product["comboItems"]), "amount_cents": 1260,
            "coupon_fingerprint": coupon_signature({"id": "synthetic-coupon", "code": "synthetic-code",
                                                    "promotionId": "synthetic-promotion", "rightCardId": "synthetic-card"})}

    def check(self, row=None):
        verify_cart_contents({"products": [self.product]}, {"items": [self.row if row is None else row]})

    def test_every_line_quantity_card_component_and_default_modifier_is_compared(self):
        self.check()
        for change in ("product", "type", "quantity", "coupon", "component", "component_quantity", "round", "default", "extra_part", "extra_modifier"):
            row = deepcopy(self.row)
            if change == "product": row["product_code"] = "900002"
            if change == "type": row["product_type"] = "1"
            if change == "quantity": row["quantity"] = 2
            if change == "coupon": row["coupon_fingerprint"] = None
            if change == "component": row["components"][0]["comboProducts"][0]["code"] = "100002"
            if change == "component_quantity": row["components"][0]["comboProducts"][0]["quantity"] = 2
            if change == "round": row["components"][0]["round"] = 3
            if change == "default": row["components"][0]["comboProducts"][0]["customization"]["options"][0]["key"] = "0-0"
            if change == "extra_part": row["components"][0]["comboProducts"].append({"code": "100002", "quantity": 1})
            if change == "extra_modifier": row["configuration"] = {"modification": {"values": [{"code": "333", "key": "0-1", "quantity": 1}]}}
            with self.subTest(change=change), self.assertRaisesRegex(EzMcdError, "cart_contents_do_not_match"):
                self.check(row)

    def test_extra_rows_missing_rows_and_boolean_quantities_do_not_match(self):
        for rows in ([], [self.row, self.row], [self.row | {"quantity": True}]):
            with self.assertRaises(EzMcdError):
                verify_cart_contents({"products": [self.product]}, {"items": rows})

    def test_merged_quantities_and_row_order_are_supported_without_ignoring_choices(self):
        plain = {"code": "100002", "type": "1", "quantity": 1}
        row = {"product_code": "100002", "product_type": "1", "quantity": 2, "components": [], "configuration": {}}
        verify_cart_contents({"products": [self.product, plain, plain]}, {"items": [row, self.row]})
        with self.assertRaises(EzMcdError):
            verify_cart_contents({"products": [self.product, plain]}, {"items": [row, self.row]})

    def test_empty_optional_round_and_normalized_single_choice_echo(self):
        self.product["comboItems"].append({"round": 3, "comboProducts": []})
        part = self.row["components"][1]["comboProducts"][0]
        part.pop("modification")
        part["customization"] = {"options": [], "items": [{"code": "222", "key": "1-1", "values": [{"code": "4", "quantity": 1, "price": 0}]}]}
        self.check()
        part["customization"]["items"][0]["key"] = "0-1"
        with self.assertRaises(EzMcdError):
            self.check()

    def test_unknown_or_paid_configuration_cannot_match_plain_or_free_items(self):
        draft = {"products": [{"code": "100001", "type": "1", "quantity": 1}]}
        row = {"product_code": "100001", "product_type": "1", "quantity": 1, "components": []}
        for config in ({"modification": {"items": [{"unknown": True}]}},
                       {"customization": {"unknown": True}}, {"grills": ["unexpected"]}):
            with self.subTest(config=config), self.assertRaises(EzMcdError):
                verify_cart_contents(draft, {"items": [row | {"configuration": config}]})
        self.row["components"][0]["comboProducts"][0]["customization"]["options"][0]["price"] = 500
        with self.assertRaises(EzMcdError):
            self.check()
