from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ezmcd.demo import DemoProvider, setup_demo
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, SHANGHAI
from ezmcd.provider import McdProvider
from ezmcd.service import Service
from ezmcd.storage import Store
from ezmcd.transport import Transport


class CartClearTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        setup_demo(self.store)
        self.service = Service(self.store)
        when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account="demo", store_code="9000001", pickup_at=when.isoformat(), max_amount_cny="20")
        self.account, self.provider = self.service.provider("demo")

    def test_clear_restart_and_repeat_preserve_later_items_and_money(self):
        viewed = self.service.cart(self.args)
        result = self.service.clear_cart(self.args, viewed["cart_ref"], "change-meal")
        self.assertTrue(result["cart_empty"])
        self.assertTrue(result["clear_attempted"])
        self.assertFalse(result["order_created"])
        self.provider.write_cart({"quantity": 2})
        restarted = Service(Store(self.root))
        with patch.object(DemoProvider, "clear_cart") as clear:
            repeated = restarted.clear_cart(self.args, viewed["cart_ref"], "change-meal")
            clear.assert_not_called()
        self.assertTrue(repeated["repeated"])
        self.assertTrue(repeated["original_clear_verified"])
        self.assertFalse(repeated["cart_empty"])
        self.assertEqual(repeated["items"][0]["quantity"], 2)
        self.assertEqual(self.provider.wallet()["balance_cents"], 10000)
        self.assertEqual(self.provider.read()["orders"], {})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT stage FROM writes").fetchall()[0][0], "cart_clear")
            self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)

    def test_changed_cart_is_not_cleared(self):
        viewed = self.service.cart(self.args)
        self.provider.write_cart({"quantity": 2})
        with patch.object(DemoProvider, "clear_cart") as clear:
            with self.assertRaisesRegex(EzMcdError, "cart_changed_review"):
                self.service.clear_cart(self.args, viewed["cart_ref"], "change-meal")
            clear.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 0)

    def test_empty_cart_does_not_send_a_mutation(self):
        self.provider.write_cart({"quantity": 0})
        viewed = self.service.cart(self.args)
        with patch.object(DemoProvider, "clear_cart") as clear:
            result = self.service.clear_cart(self.args, viewed["cart_ref"], "empty")
            clear.assert_not_called()
        self.assertTrue(result["cart_empty"])
        self.assertFalse(result["clear_attempted"])

    def test_stale_reference_and_wrong_scope_stop_before_mutation(self):
        viewed = self.service.cart(self.args)
        with patch.object(DemoProvider, "clear_cart") as clear:
            for update in ({"store_code": "9000002"}, {"pickup_at": self.args.pickup_at.replace("10:15", "10:20")}):
                with self.assertRaisesRegex(EzMcdError, "cart_snapshot_scope_changed"):
                    self.service.clear_cart(self.args.model_copy(update=update), viewed["cart_ref"], "clear")
            with self.store.connect() as db:
                db.execute("UPDATE cart_snapshots SET expires=0 WHERE id=?", (viewed["cart_ref"],))
            with self.assertRaisesRegex(EzMcdError, "cart_snapshot_expired"):
                self.service.clear_cart(self.args, viewed["cart_ref"], "clear")
            clear.assert_not_called()

    def test_reference_is_member_bound(self):
        viewed = self.service.cart(self.args)
        setup_demo(self.store, "someone-else")
        with self.assertRaisesRegex(EzMcdError, "cart_snapshot_not_found_for_account"):
            self.service.clear_cart(self.args.model_copy(update={"account": "someone-else"}), viewed["cart_ref"], "clear")

    def test_reusing_key_for_a_new_snapshot_and_new_key_for_used_snapshot_stop(self):
        viewed = self.service.cart(self.args)
        self.service.clear_cart(self.args, viewed["cart_ref"], "clear")
        self.provider.write_cart({"quantity": 1})
        current = self.service.cart(self.args)
        with patch.object(DemoProvider, "clear_cart") as clear:
            with self.assertRaisesRegex(EzMcdError, "cart_clear_key_already_used"):
                self.service.clear_cart(self.args, current["cart_ref"], "clear")
            with self.assertRaisesRegex(EzMcdError, "cart_snapshot_expired_or_already_used"):
                self.service.clear_cart(self.args, viewed["cart_ref"], "new-key")
            clear.assert_not_called()
        self.assertIsNone(self.store.cart_snapshot(self.account["identity"], current["cart_ref"])["clear_key"])

    def test_timeout_before_or_after_mutation_is_query_only_even_after_expiry(self):
        original = DemoProvider.clear_cart
        for happened in (False, True):
            with self.subTest(happened=happened):
                self.provider.write_cart({"quantity": 1})
                viewed = self.service.cart(self.args)
                def timeout(provider, body):
                    if happened:
                        original(provider, body)
                    raise EzMcdError("transport_unknown_do_not_retry_writes")
                with patch.object(DemoProvider, "clear_cart", timeout):
                    with self.assertRaisesRegex(EzMcdError, "transport_unknown"):
                        self.service.clear_cart(self.args, viewed["cart_ref"], "timeout-" + str(happened))
                with self.store.connect() as db:
                    db.execute("UPDATE cart_snapshots SET expires=0 WHERE id=?", (viewed["cart_ref"],))
                with patch.object(DemoProvider, "clear_cart") as clear:
                    result = Service(Store(self.root)).clear_cart(self.args, viewed["cart_ref"], "timeout-" + str(happened))
                    clear.assert_not_called()
                self.assertEqual(result["cart_empty"], happened)
                self.assertFalse(result["original_clear_verified"])

    def test_server_rejection_and_nonempty_readback_cannot_trigger_replay(self):
        for success in (False, True):
            with self.subTest(success=success):
                viewed = self.service.cart(self.args)
                with patch.object(DemoProvider, "clear_cart", return_value={"http_status": 200,
                        "response": {"success": success, "data": {}}}) as clear:
                    with self.assertRaises(EzMcdError):
                        self.service.clear_cart(self.args, viewed["cart_ref"], "reject-" + str(success))
                    self.service.clear_cart(self.args, viewed["cart_ref"], "reject-" + str(success))
                    self.assertEqual(clear.call_count, 1)

    def test_same_account_lock_applies_to_inspection_and_clear(self):
        viewed = self.service.cart(self.args)
        with self.store.lock(self.account["identity"]):
            with self.assertRaisesRegex(EzMcdError, "account_busy"):
                self.service.clear_cart(self.args, viewed["cart_ref"], "clear")
            with self.assertRaisesRegex(EzMcdError, "account_busy"):
                self.service.cart(self.args)


class CartProtocolTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.provider = McdProvider({}, transport=self.http)
        when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account="test", store_code="9000001", pickup_at=when.isoformat(), max_amount_cny="20")
        self.params = {"storeCode": "9000001", "orderType": "1", "orderMode": "", "beCode": "",
                       "date": when.date().isoformat(), "time": "10:15"}
        self.raw = {"storeCode": "9000001", "beType": 1, "orderType": 1, "daypartCode": "1",
            "products": [{"code": "100001", "name": "演示单品", "type": "1", "quantity": 1,
                          "realSubTotalPrice": 990, "privateServerRevision": "example"}], "localDateTime": "clock"}
        self.provider.context = Mock(return_value=("1", self.params))
        self.provider.get = Mock(side_effect=lambda *a: deepcopy(self.raw))

    def test_clear_uses_official_put_body_and_only_clock_is_ignored(self):
        public, body, original = self.provider.cart_state(self.args)
        self.assertNotIn("privateServerRevision", str(public))
        self.assertEqual(body, self.params | {"cartType": "1", "channelCode": "03", "pinId": "", "daypartCode": "1"})
        self.provider.clear_cart(body)
        self.http.send.assert_called_once_with("PUT", "/bff/cart/carts/empty", body=body)
        self.raw["localDateTime"] = "next-clock"
        self.assertEqual(self.provider.cart_state(self.args)[2], original)
        for field, value in (("quantity", 2), ("realSubTotalPrice", 1000),
                             ("privateServerRevision", "changed"), ("modification", {"values": []})):
            with self.subTest(field=field):
                original_product = deepcopy(self.raw["products"][0])
                self.raw["products"][0][field] = value
                self.assertNotEqual(self.provider.cart_state(self.args)[2], original)
                self.raw["products"][0] = original_product

    def test_cart_store_daypart_and_fulfillment_must_match(self):
        for field, value in (("storeCode", "9000002"), ("beType", 2), ("orderType", 2),
                             ("daypartCode", "2"), ("products", None)):
            with self.subTest(field=field):
                original = deepcopy(self.raw)
                self.raw[field] = value
                with self.assertRaisesRegex(EzMcdError, "cart_store_fulfillment_or_daypart_mismatch"):
                    self.provider.cart_state(self.args)
                self.raw = original
        self.http.send.assert_not_called()

    def test_only_exact_clear_method_and_route_are_allowed(self):
        transport = Transport({})
        transport.signer = Mock()
        transport.signer.headers.return_value = {}
        with patch.object(transport, "_open", return_value={}) as send:
            transport.send("PUT", "/bff/cart/carts/empty", body={})
            self.assertEqual(transport.signer.headers.call_args.args[4], ("1003", "200"))
            for method, path in (("POST", "/bff/cart/carts/empty"), ("DELETE", "/bff/cart/carts/empty"),
                                 ("PUT", "/bff/cart/carts/empty/other")):
                with self.assertRaisesRegex(EzMcdError, "endpoint_not_supported"):
                    transport.send(method, path, body={})
            self.assertEqual(send.call_count, 1)
