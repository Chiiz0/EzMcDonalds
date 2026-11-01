from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ezmcd.demo import DemoProvider, setup_demo
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, ItemSelection, SHANGHAI
from ezmcd.service import Service
from ezmcd.storage import Store


class CartRetirementTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        setup_demo(self.store)
        self.service = Service(self.store)
        self.account, self.provider = self.service.provider("demo")
        self.identity = self.account["identity"]
        self.provider.write_cart({"quantity": 0})
        past = (datetime.now(SHANGHAI) - timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.past = CheckoutInput(account="demo", store_code="9000001", pickup_at=past.isoformat(), max_amount_cny="20")
        future = (past + timedelta(days=2)).isoformat()
        self.future = self.past.model_copy(update={"pickup_at": future})
        self.earlier = past - timedelta(hours=1)
        self.items = [ItemSelection(product_code="100001")]

    def pending_addition(self, *, args=None, key="old-add", keep_cart=False):
        args = args or self.past
        original = DemoProvider.write_cart
        def lost(provider, body):
            if keep_cart:
                original(provider, body)
            raise EzMcdError("transport_unknown_do_not_retry_writes")
        with patch("ezmcd.demo.datetime", wraps=datetime) as clock, patch.object(DemoProvider, "write_cart", lost), \
                patch.object(self.service, "_cart_snapshot", side_effect=EzMcdError("readback_unavailable")):
            clock.now.return_value = self.earlier
            return self.service.add_simple_items(args, self.items, key)

    def pending_pickup(self, *, args=None):
        args = (args or self.past).model_copy(update={"eat_type": "take-in-store"})
        self.provider.write_cart({"quantity": 1})
        with patch("ezmcd.demo.datetime", wraps=datetime) as clock, \
                patch.object(DemoProvider, "switch_pickup", side_effect=EzMcdError("transport_unknown_do_not_retry_writes")):
            clock.now.return_value = self.earlier
            ref = self.service.cart(args)["cart_ref"]
            result = self.service.quote_pickup(args, ref, "old-pickup")
        return result, args, ref

    def counts(self):
        with self.store.connect() as db:
            return dict(db.execute("SELECT stage,count(*) FROM writes GROUP BY stage"))

    def test_previous_day_unknown_addition_can_be_abandoned_and_next_meal_completed(self):
        original = self.pending_addition()
        status = self.service.cart_add_status("demo")
        self.assertEqual(status["state"], "UNAVAILABLE")
        self.assertEqual(status["query_error"], "pickup_time_has_passed")
        self.assertEqual(status["next_action"], "cart_operations_list")
        with self.assertRaisesRegex(EzMcdError, "cart_addition_unresolved"):
            self.service.add_simple_items(self.future, self.items, "new-meal")
        listed = self.service.cart_operations("demo")
        self.assertTrue(listed["operations"][0]["retirement_available"])
        self.assertEqual(listed["operations"][0]["request"], self.past.model_dump())
        retired = self.service.retire_cart_operation("demo", original["operation_id"])
        self.assertEqual(retired["state"], "RETIRED")
        self.assertFalse(retired["retirement_confirms_upstream_outcome"])
        self.assertFalse(retired["financial_records_changed"])
        self.assertFalse(self.store.pending_cart_addition(self.identity))
        self.assertEqual(self.counts(), {"cart": 1})
        current = self.service.cart(self.future)
        self.assertEqual(current["items"], [])
        added = self.service.add_simple_items(self.future, self.items, "new-meal")
        self.assertEqual(added["state"], "MATCHED")
        quote = self.service.quote(self.future)
        paid = self.service.submit("demo", quote["quote_id"], "new-meal-purchase")
        self.assertTrue(paid["paid_verified"])
        self.assertTrue(paid["simulation"])
        self.assertEqual(self.counts(), {"cart": 2, "order": 1, "preorder": 1, "consume": 1})

    def test_retirement_keeps_cart_and_new_meal_cannot_overwrite_it(self):
        original = self.pending_addition(keep_cart=True)
        before = self.provider.path.read_bytes()
        self.service.retire_cart_operation("demo", original["operation_id"])
        self.assertEqual(before, self.provider.path.read_bytes())
        with self.assertRaisesRegex(EzMcdError, "cart_not_empty"):
            self.service.add_simple_items(self.future, self.items, "new-meal")
        self.assertEqual(self.counts(), {"cart": 1})
        viewed = self.service.cart(self.future)
        self.service.clear_cart(self.future, viewed["cart_ref"], "remove-reviewed-old-items")
        self.assertEqual(self.service.add_simple_items(self.future, self.items, "new-meal")["state"], "MATCHED")

    def test_pickup_retirement_replay_and_restart_never_switch_or_restore_quote(self):
        original, args, ref = self.pending_pickup()
        before = self.provider.path.read_bytes()
        self.assertTrue(self.store.pending_pickup_quote(self.identity))
        self.service.retire_cart_operation("demo", original["operation_id"])
        self.service = Service(Store(self.store.root))
        with patch.object(self.service, "provider", side_effect=AssertionError("no network")):
            results = [self.service.pickup_quote_status("demo", original["operation_id"]),
                       self.service.quote_pickup(args, ref, "old-pickup"),
                       self.service.retire_cart_operation("demo", original["operation_id"])]
        for result in results:
            self.assertEqual(result["state"], "RETIRED")
            self.assertFalse(result["switch_attempted"])
            self.assertNotIn("quote_id", result)
            self.assertTrue(result["repeated"])
        self.assertFalse(self.store.pending_pickup_quote(self.identity))
        self.assertEqual(before, self.provider.path.read_bytes())
        self.assertEqual(self.counts(), {"pickup_quote": 1})
        with self.assertRaisesRegex(EzMcdError, "pickup_key_reused"):
            self.service.quote_pickup(self.future, ref, "old-pickup")

    def test_addition_old_key_and_choices_remain_bound_after_retirement(self):
        original = self.pending_addition()
        self.service.retire_cart_operation("demo", original["operation_id"])
        self.service = Service(Store(self.store.root))
        with patch.object(self.service, "provider", side_effect=AssertionError("no network")):
            for result in (self.service.cart_add_status("demo", original["operation_id"]),
                           self.service.add_simple_items(self.past, self.items, "old-add")):
                self.assertEqual(result["state"], "RETIRED")
                self.assertEqual(result["requested_items"], [i.model_dump() for i in self.items])
                self.assertFalse(result["add_attempted"])
        with self.assertRaisesRegex(EzMcdError, "cart_key_reused"):
            self.service.add_simple_items(self.future, self.items, "old-add")
        self.assertEqual(self.counts(), {"cart": 1})

    def test_future_pickup_and_quote_expiry_alone_cannot_retire(self):
        original, _, _ = self.pending_pickup(args=self.future)
        with patch("ezmcd.pickup.time.time", return_value=datetime.now().timestamp() + 130):
            self.assertEqual(self.service.pickup_quote_status("demo")["state"], "EXPIRED")
        self.assertFalse(self.service.cart_operations("demo")["operations"][0]["retirement_available"])
        with self.assertRaisesRegex(EzMcdError, "pickup_time_not_passed"):
            self.service.retire_cart_operation("demo", original["operation_id"])
        self.assertTrue(self.store.pending_pickup_quote(self.identity))

    def test_future_addition_cannot_be_retired(self):
        original = self.pending_addition(args=self.future)
        with self.assertRaisesRegex(EzMcdError, "pickup_time_not_passed"):
            self.service.retire_cart_operation("demo", original["operation_id"])
        self.assertTrue(self.store.pending_cart_addition(self.identity))

    def test_resolved_record_cannot_be_retired(self):
        with patch("ezmcd.demo.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.earlier
            original = self.service.add_simple_items(self.past, self.items, "matched")
        self.assertEqual(original["state"], "MATCHED")
        with self.assertRaisesRegex(EzMcdError, "already_resolved"):
            self.service.retire_cart_operation("demo", original["operation_id"])

    def test_identity_isolation_and_revocation_do_not_require_network(self):
        original = self.pending_addition()
        setup_demo(self.store, "other")
        with self.assertRaisesRegex(EzMcdError, "not_found_for_account"):
            self.service.retire_cart_operation("other", original["operation_id"])
        self.assertEqual(self.service.cart_operations("other")["total"], 0)
        self.store.modify_account("demo", lambda a: a | {"revoked": True})
        with patch.object(self.service, "provider", side_effect=AssertionError("no network")):
            self.assertEqual(self.service.cart_operations("demo")["total"], 1)
            self.service.retire_cart_operation("demo", original["operation_id"])
            self.assertEqual(self.service.cart_add_status("demo")["state"], "RETIRED")

    def test_storage_failure_keeps_blocker_and_all_write_claims(self):
        original = self.pending_addition()
        with patch.object(self.store, "retire_cart_operation", side_effect=OSError("disk")):
            with self.assertRaises(OSError): self.service.retire_cart_operation("demo", original["operation_id"])
        self.assertTrue(self.store.pending_cart_addition(self.identity))
        self.assertNotEqual(self.store.cart_addition(self.identity, original["operation_id"])["state"], "RETIRED")
        self.assertEqual(self.counts(), {"cart": 1})

    def test_local_list_pagination_both_kinds_and_secret_minimization(self):
        with patch("ezmcd.demo.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.earlier
            self.service.add_simple_items(self.past, self.items, "matched")
        pending, _, _ = self.pending_pickup()
        first = self.service.cart_operations("demo", unresolved_only=False, limit=1)
        second = self.service.cart_operations("demo", unresolved_only=False, limit=1, offset=first["next_offset"])
        self.assertEqual(first["total"], 2)
        self.assertIsNone(second["next_offset"])
        self.assertEqual({first["operations"][0]["kind"], second["operations"][0]["kind"]}, {"addition", "pickup_quote"})
        self.assertEqual(self.service.cart_operations("demo")["operations"][0]["operation_id"], pending["operation_id"])
        serialized = json.dumps(first)
        for private_field in ('"prepared"', '"draft"', '"body"', '"receipt"', '"uniqueKey"', '"identity"'):
            self.assertNotIn(private_field, serialized)
        self.service.retire_cart_operation("demo", pending["operation_id"])
        self.assertEqual(self.service.cart_operations("demo")["total"], 0)
        self.assertEqual(self.service.cart_operations("demo", unresolved_only=False)["total"], 2)

    def test_invalid_filters_and_non_cart_references_are_rejected(self):
        for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"offset": -1}, {"offset": 10001},
                       {"offset": False}, {"unresolved_only": "false"}):
            with self.assertRaisesRegex(EzMcdError, "invalid_cart_operations_filter"):
                self.service.cart_operations("demo", **kwargs)
        for reference in ("../private", "a" * 32, "cart-" + "a" * 32, "payment-" + "a" * 64, None):
            with self.assertRaisesRegex(EzMcdError, "invalid_cart_recovery_reference"):
                self.service.retire_cart_operation("demo", reference)
        with self.assertRaisesRegex(EzMcdError, "not_found_for_account"):
            self.service.retire_cart_operation("demo", "cart-" + "a" * 64)

    def test_financial_quotes_policies_keys_and_writes_remain_byte_identical(self):
        self.provider.write_cart({"quantity": 1})
        quote = self.service.quote(self.future)
        paid = self.service.submit("demo", quote["quote_id"], "original-purchase")
        self.provider.write_cart({"quantity": 0})
        original = self.pending_addition()
        def snapshots():
            with self.store.connect() as db:
                result = {t: [tuple(r) for r in db.execute("SELECT * FROM " + t)]
                          for t in ("quotes", "attempts", "request_keys", "writes")}
            result["account"] = (self.store.root / "accounts" / (self.identity + ".json")).read_bytes()
            return result
        before = snapshots()
        self.service.retire_cart_operation("demo", original["operation_id"])
        self.assertEqual(before, snapshots())
        recovered = self.service.submit("demo", quote["quote_id"], "original-purchase")
        self.assertEqual(recovered["order_id"], paid["order_id"])
        self.assertEqual(self.counts(), {"cart": 1, "order": 1, "preorder": 1, "consume": 1})

    def test_cli_restarts_keep_retirement_and_print_no_credentials(self):
        original = self.pending_addition()
        command = [sys.executable, "-m", "ezmcd.cli", "--data-dir", str(self.store.root)]
        def run(*args):
            p = subprocess.run(command + list(args), capture_output=True, text=True, timeout=20)
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertEqual(run("cart-operations", "demo")["total"], 1)
        first = run("cart-retire", "demo", "--operation-id", original["operation_id"])
        repeated = run("cart-retire", "demo", "--operation-id", original["operation_id"])
        self.assertFalse(first["repeated"])
        self.assertTrue(repeated["repeated"])
        self.assertEqual(first["retired_at"], repeated["retired_at"])
        self.assertEqual(run("cart-operations", "demo")["total"], 0)
        self.assertEqual(run("cart-operations", "demo", "--all")["total"], 1)
        self.assertEqual(run("cart-status", "demo")["state"], "RETIRED")

    def test_existing_account_lock_prevents_retirement_race(self):
        original = self.pending_addition()
        with self.store.lock(self.identity), self.assertRaisesRegex(EzMcdError, "account_busy"):
            self.service.retire_cart_operation("demo", original["operation_id"])
        self.assertTrue(self.store.pending_cart_addition(self.identity))

    def test_unknown_financial_attempt_still_blocks_replacement_purchase(self):
        self.provider.write_cart({"quantity": 1})
        quote = self.service.quote(self.future)
        with patch.object(DemoProvider, "create_order", side_effect=EzMcdError("transport_unknown_do_not_retry_writes")):
            unknown = self.service.submit("demo", quote["quote_id"], "lost-order")
        self.assertEqual(unknown["state"], "UNKNOWN")
        self.provider.write_cart({"quantity": 0})
        old_cart = self.pending_addition()
        self.service.retire_cart_operation("demo", old_cart["operation_id"])
        self.provider.write_cart({"quantity": 2})
        fresh = self.service.quote(self.future)
        with patch.object(DemoProvider, "create_order") as create, self.assertRaisesRegex(EzMcdError, "existing_local_attempt"):
            self.service.submit("demo", fresh["quote_id"], "new-key-cannot-bypass")
        create.assert_not_called()
        self.assertEqual(self.counts(), {"cart": 1, "order": 1})
        self.assertEqual(self.store.attempt(self.identity, unknown["attempt_id"])["state"], "UNKNOWN")

    def test_pickup_boundary_uses_instant_not_local_date_or_machine_timezone(self):
        original = self.pending_addition()
        instant = datetime.fromisoformat(self.past.pickup_at)
        with patch("ezmcd.cart_recovery.datetime", wraps=datetime) as clock:
            clock.now.return_value = instant - timedelta(seconds=1)
            self.assertFalse(self.service.cart_operations("demo")["operations"][0]["retirement_available"])
            with self.assertRaisesRegex(EzMcdError, "pickup_time_not_passed"):
                self.service.retire_cart_operation("demo", original["operation_id"])
            clock.now.return_value = instant
            self.assertTrue(self.service.cart_operations("demo")["operations"][0]["retirement_available"])
            self.assertTrue(self.service.retire_cart_operation("demo", original["operation_id"])["retired"])


if __name__ == "__main__":
    unittest.main()
