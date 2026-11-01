from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ezmcd.demo import DemoProvider, setup_demo
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, ItemSelection, SHANGHAI
from ezmcd.service import Service
from ezmcd.storage import Store


class PickupQuoteTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        setup_demo(self.store)
        self.service = Service(self.store)
        when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account="demo", store_code="9000001", pickup_at=when.isoformat(),
                                  max_amount_cny="20", eat_type="take-in-store")
        self.ref = self.service.cart(self.args)["cart_ref"]
        self.identity = self.store.account("demo")["identity"]

    def prepare(self, key="pick", ref=None):
        return self.service.quote_pickup(self.args, ref or self.ref, key)

    def test_counter_quote_restarts_and_simulated_purchase_preserve_method(self):
        original = DemoProvider.switch_pickup
        with patch.object(DemoProvider, "switch_pickup", autospec=True, side_effect=original) as switch:
            first = self.prepare()
            self.assertEqual(first["state"], "READY")
            self.assertEqual(first["eat_type"], "take-in-store")
            self.assertEqual(first["pickup_confirmation"]["selected_method"], "take-in-store")
            q = self.store.quote(self.identity, first["quote_id"])
            self.assertEqual(q["body"]["eatTypeCode"], "take-in-store")
            self.service = Service(Store(self.store.root))
            recovered = self.service.pickup_quote_status("demo")
            repeated = self.prepare()
            self.assertEqual({first["quote_id"], recovered["quote_id"], repeated["quote_id"]}, {first["quote_id"]})
            self.assertEqual(repeated["expires_at"], first["expires_at"])
            self.assertFalse(repeated["switch_attempted"])
            # Initial confirmation still selects its default, as the real API does.
            _, p = self.service.provider("demo")
            self.assertEqual(p.pickup_methods(self.args)["selected_method"], "locker-in")
            paid = self.service.submit("demo", first["quote_id"], "buy")
            self.assertTrue(paid["paid_verified"])
            self.assertFalse(p.read()["orders"][paid["order_id"]]["lockerOrder"])
            self.assertEqual(switch.call_count, 1)
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute("SELECT stage,count(*) FROM writes GROUP BY stage")),
                             {"pickup_quote": 1, "order": 1, "preorder": 1, "consume": 1})

    def test_changed_key_input_and_reused_snapshot_do_not_switch_again(self):
        first = self.prepare()
        for args, ref in ((self.args.model_copy(update={"max_amount_cny": "30.00"}), self.ref),
                          (self.args, self.service.cart(self.args)["cart_ref"])):
            with self.assertRaisesRegex(EzMcdError, "pickup_key_reused_for_different_request"):
                self.service.quote_pickup(args, ref, "pick")
        with self.assertRaisesRegex(EzMcdError, "pickup_quote_reference_already_used"):
            self.prepare(key="another")
        self.assertEqual(self.service.pickup_quote_status("demo")["quote_id"], first["quote_id"])

    def test_unknown_response_never_retries_and_blocks_new_preparation_and_order(self):
        with patch.object(DemoProvider, "switch_pickup", side_effect=EzMcdError("transport_unknown_do_not_retry_writes")) as switch:
            first = self.prepare()
            self.assertEqual(first["state"], "UNKNOWN")
            self.assertIn("不重发", first["recovery_action"])
            self.assertNotIn("quote_id", first)
            self.assertEqual(self.prepare()["operation_id"], first["operation_id"])
            self.assertEqual(switch.call_count, 1)
        with self.assertRaisesRegex(EzMcdError, "pickup_preparation_unresolved"):
            self.prepare(key="new", ref=self.service.cart(self.args)["cart_ref"])
        quote = self.service.quote(self.args.model_copy(update={"eat_type": "locker-in"}))
        with self.assertRaisesRegex(EzMcdError, "pickup_preparation_unresolved"), patch.object(DemoProvider, "create_order") as create:
            self.service.submit("demo", quote["quote_id"], "do-not-buy")
        create.assert_not_called()

    def test_explicit_clear_resolves_unknown_but_old_call_does_not_resend(self):
        with patch.object(DemoProvider, "switch_pickup", side_effect=EzMcdError("lost")):
            first = self.prepare()
        viewed = self.service.cart(self.args)
        self.service.clear_cart(self.args, viewed["cart_ref"], "clear")
        self.assertFalse(self.store.pending_pickup_quote(self.identity))
        with patch.object(DemoProvider, "switch_pickup") as switch:
            self.assertEqual(self.prepare()["operation_id"], first["operation_id"])
            switch.assert_not_called()
        self.service.add_simple_items(self.args, [ItemSelection(product_code="100001")], "new-meal")
        ready = self.prepare(key="reviewed-new-quote", ref=self.service.cart(self.args)["cart_ref"])
        self.assertEqual(ready["state"], "READY")

    def test_wrong_scope_clear_cannot_resolve_unknown_preparation(self):
        with patch.object(DemoProvider, "switch_pickup", side_effect=EzMcdError("lost")):
            self.prepare()
        wrong = self.args.model_copy(update={"store_code": "9000002"})
        viewed = self.service.cart(wrong)
        self.service.clear_cart(wrong, viewed["cart_ref"], "wrong-scope")
        self.assertTrue(self.store.pending_pickup_quote(self.identity))

    def test_stored_receipt_recovers_after_atomic_quote_save_failure(self):
        original = DemoProvider.switch_pickup
        with patch.object(DemoProvider, "switch_pickup", autospec=True, side_effect=original) as switch:
            with patch.object(self.store, "complete_pickup_quote", side_effect=OSError("simulated disk failure")):
                first = self.prepare()
            self.assertEqual(first["state"], "UNKNOWN")
            self.assertNotIn("quote_id", self.store.pickup_quote(self.identity))
            self.service = Service(Store(self.store.root))
            recovered = self.service.pickup_quote_status("demo", first["operation_id"])
            self.assertEqual(recovered["state"], "READY")
            self.assertEqual(switch.call_count, 1)

    def test_claim_failure_is_before_network_write(self):
        with patch.object(self.store, "claim_pickup_quote", side_effect=OSError("disk")), \
                patch.object(DemoProvider, "switch_pickup") as switch:
            with self.assertRaises(OSError): self.prepare()
        switch.assert_not_called()

    def test_lost_receipt_save_is_unknown_and_never_replayed(self):
        original = DemoProvider.switch_pickup
        with patch.object(DemoProvider, "switch_pickup", autospec=True, side_effect=original) as switch:
            with patch.object(self.store, "receipt", side_effect=OSError("disk")):
                result = self.prepare()
            self.assertEqual(result["state"], "UNKNOWN")
            self.assertEqual(self.prepare()["state"], "UNKNOWN")
            self.assertEqual(switch.call_count, 1)

    def test_stale_changed_and_wrong_scope_snapshot_stop_before_switch(self):
        _, provider = self.service.provider("demo")
        provider.write_cart({"quantity": 2})
        with patch.object(DemoProvider, "switch_pickup") as switch:
            with self.assertRaisesRegex(EzMcdError, "cart_changed"):
                self.prepare()
            with self.assertRaisesRegex(EzMcdError, "cart_snapshot_scope_changed"):
                self.service.quote_pickup(self.args.model_copy(update={"store_code": "9000002"}), self.ref, "pick")
            with patch("ezmcd.pickup.time.time", return_value=time.time() + 130), self.assertRaisesRegex(EzMcdError, "expired"):
                self.prepare()
        switch.assert_not_called()

    def test_cart_change_between_snapshot_read_and_preparation_stops(self):
        original = DemoProvider.prepare_pickup
        def changed(provider, args):
            provider.write_cart({"quantity": 2})
            return original(provider, args)
        with patch.object(DemoProvider, "prepare_pickup", changed), patch.object(DemoProvider, "switch_pickup") as switch:
            with self.assertRaisesRegex(EzMcdError, "cart_changed"):
                self.prepare()
        switch.assert_not_called()

    def test_snapshot_expiring_during_reads_stops_before_switch(self):
        original = DemoProvider.prepare_pickup
        finished = [False]
        now = time.time()
        def slow(provider, args):
            result = original(provider, args)
            finished[0] = True
            return result
        with patch.object(DemoProvider, "prepare_pickup", slow), patch.object(DemoProvider, "switch_pickup") as switch, \
                patch("ezmcd.pickup.time.time", side_effect=lambda: now + 130 if finished[0] else now):
            with self.assertRaisesRegex(EzMcdError, "cart_snapshot_expired"):
                self.prepare()
        switch.assert_not_called()

    def test_cart_or_price_change_after_quote_stops_submit_without_repeating_switch(self):
        result = self.prepare()
        _, provider = self.service.provider("demo")
        provider.write_cart({"quantity": 2})
        with patch.object(DemoProvider, "create_order") as create, patch.object(DemoProvider, "switch_pickup") as switch:
            with self.assertRaisesRegex(EzMcdError, "pickup_cart_changed"):
                self.service.submit("demo", result["quote_id"], "stop")
            self.assertEqual(self.service.pickup_quote_status("demo")["state"], "UNKNOWN")
        create.assert_not_called(); switch.assert_not_called()

    def test_quote_expiry_is_not_extended_by_status_or_replay(self):
        first = self.prepare()
        with patch("ezmcd.pickup.time.time", return_value=first["expires_at"] + 1), patch.object(DemoProvider, "switch_pickup") as switch:
            self.assertEqual(self.prepare()["state"], "EXPIRED")
            with self.assertRaisesRegex(EzMcdError, "quote_expired"):
                self.service.submit("demo", first["quote_id"], "expired")
        switch.assert_not_called()

    def test_revoked_account_retains_intent_and_other_identity_cannot_read_it(self):
        first = self.prepare()
        self.store.modify_account("demo", lambda a: a | {"revoked": True})
        result = self.service.pickup_quote_status("demo")
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["request"]["eat_type"], "take-in-store")
        self.assertNotIn("prepared", result)
        setup_demo(self.store, "other")
        with self.assertRaisesRegex(EzMcdError, "not_found_for_account"):
            self.service.pickup_quote_status("other", first["operation_id"])

    def test_default_already_selected_uses_readonly_quote(self):
        args = self.args.model_copy(update={"eat_type": "locker-in"})
        ref = self.service.cart(args)["cart_ref"]
        with patch.object(DemoProvider, "switch_pickup") as switch, self.assertRaisesRegex(EzMcdError, "already_selected"):
            self.service.quote_pickup(args, ref, "default")
        switch.assert_not_called()

    def test_partial_response_may_omit_tableware_but_not_critical_fields(self):
        original = DemoProvider.switch_pickup
        for field in ("productPriceInfo", "storeInfo", "storePickUpInfo", "dayPart", "submit"):
            with self.subTest(field=field):
                def missing(provider, body):
                    r = original(provider, body)
                    del r["response"]["data"]["confirmInfo"][field]
                    return r
                with patch.object(DemoProvider, "switch_pickup", missing):
                    result = self.prepare(key=field, ref=self.service.cart(self.args)["cart_ref"])
                self.assertEqual(result["state"], "UNKNOWN")
                self.assertNotIn("quote_id", result)
                self.service.clear_cart(self.args, self.service.cart(self.args)["cart_ref"], "clear-" + field)
                _, p = self.service.provider("demo"); p.write_cart({"quantity": 1})

    def test_false_switch_result_is_not_ready(self):
        original = DemoProvider.switch_pickup
        def rejected(provider, body):
            r = original(provider, body); r["response"]["data"]["switchResult"] = False
            return r
        with patch.object(DemoProvider, "switch_pickup", rejected):
            self.assertEqual(self.prepare()["error"], "pickup_switch_not_confirmed")

    def test_new_plate_requirement_in_response_stops_before_quote(self):
        original = DemoProvider.switch_pickup
        def plate(provider, body):
            r = original(provider, body)
            r["response"]["data"]["confirmInfo"]["requireLicensePlate"] = True
            return r
        with patch.object(DemoProvider, "switch_pickup", plate):
            result = self.prepare()
        self.assertEqual(result["error"], "pickup_special_flow_requires_official_app")
        self.assertNotIn("quote_id", result)

    def test_switch_transport_is_fixed_method_path_and_business_headers(self):
        from unittest.mock import Mock
        from ezmcd.transport import Transport
        from ezmcd.pickup import SWITCH_PATH
        http = Transport({}); http.signer = Mock()
        http.signer.headers.return_value = {}
        with patch.object(http, "_open", return_value={}) as request:
            http.send("PUT", SWITCH_PATH, body={"eatTypeCodeNew": "take-in-store"})
            self.assertEqual(http.signer.headers.call_args.args[-1], ("1029", "202"))
            self.assertEqual(request.call_args.args[:2], ("PUT", "https://api.mcd.cn" + SWITCH_PATH))
            for method in ("GET", "POST", "DELETE"):
                with self.assertRaisesRegex(EzMcdError, "endpoint_not_supported"):
                    http.send(method, SWITCH_PATH)
            self.assertEqual(request.call_count, 1)

    def test_current_cart_change_after_successful_reply_prevents_quote(self):
        original = DemoProvider.switch_pickup
        def changed(provider, body):
            response = original(provider, body)
            provider.write_cart({"quantity": 2})
            return response
        with patch.object(DemoProvider, "switch_pickup", changed):
            result = self.prepare()
        self.assertEqual(result["error"], "pickup_cart_changed_get_new_quote")
        self.assertNotIn("quote_id", result)

    def test_counter_order_validation_rejects_locker_dinein_delivery_and_dt(self):
        result = self.prepare()
        paid = self.service.submit("demo", result["quote_id"], "purchase")
        _, p = self.service.provider("demo")
        q = self.store.quote(self.identity, result["quote_id"])
        attempt = self.store.attempt(self.identity, paid["attempt_id"])
        order = p.status(attempt)["order"]
        for change in ({"lockerOrder": True}, {"lockerOrder": None}, {"eatType": 1}, {"eatType": "2"},
                       {"beType": "2"}, {"orderType": "2"}, {"dtStatus": "WAITING"}):
            with self.subTest(change=change), self.assertRaisesRegex(EzMcdError, "created_order_does_not_match_quote"):
                p.validate_order(attempt, q, order | change)
