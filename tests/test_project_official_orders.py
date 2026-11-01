from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from ezmcd.cli import main
from ezmcd.demo import DemoProvider, setup_demo
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, SHANGHAI
from ezmcd.order_views import public_order, public_orders
from ezmcd.provider import McdProvider
from ezmcd.service import Service
from ezmcd.storage import Store


def order():
    return {"orderId": "TEST-1", "payId": "private-pay-reference", "storeCode": "9000001", "storeName": "演示门店",
        "orderType": "1", "beType": "1", "eatType": 2, "lockerOrder": True, "orderTypeName": "到店取餐",
        "realTotalAmount": "12.6", "realPayAmount": "12.6", "totalDiscountAmount": "8.4",
        "createTime": "2026-11-01 10:00:00", "saleTime": "2026-11-01 10:00:01", "expectPickUpTime": "2026-11-02 10:15:00",
        "orderStatus": "确认中", "orderStatusTitle": "预约完成", "pickupCode": "A1001", "paymentChannel": "ARCHCARD",
        "paymentTransactionId": "private-transaction", "phone": "private-phone", "remark": "private-remark",
        "orderDetailUrl": "https://example.invalid/?token=private-token", "orderActionList": [{"cancel": "private-action"}],
        "couponList": [{"couponName": "示例早餐卡", "couponId": "private-coupon"}],
        "orderProductList": [{"productCode": "100001", "productName": "示例套餐", "quantity": 1, "realSubtotal": "12.6",
            "id": "private-product-id", "productImage": "https://example.invalid/?sid=private-image", "comboItemList": [
                {"productCode": "200001", "name": "示例咖啡", "quantity": 1}]}]}


class OfficialOrderViewsTests(unittest.TestCase):
    def test_allowlist_keeps_meal_pickup_and_amount_but_no_payment_or_contact_references(self):
        result = public_order(order(), detail=True)
        self.assertEqual(result["amount_cny"], "12.60")
        self.assertEqual(result["discount_names"], ["示例早餐卡"])
        self.assertEqual(result["pickup_at"], "2026-11-02T10:15:00+08:00")
        self.assertEqual(result["status_title"], "预约完成")
        self.assertEqual(result["items"][0]["components"][0]["name"], "示例咖啡")
        self.assertEqual(result["eat_type"], "locker-in")
        self.assertNotIn("private-", json.dumps(result))

    def test_sale_time_never_substitutes_for_missing_pickup_time(self):
        raw = order(); del raw["expectPickUpTime"]
        result = public_orders({"list": [raw]}, 1, 10)
        self.assertIsNone(result["orders"][0]["pickup_at"])
        self.assertNotIn("saleTime", json.dumps(result))

    def test_full_page_only_suggests_next_page_without_claiming_total_history(self):
        result = public_orders({"list": [order()]}, 2, 1)
        self.assertEqual(result["next_page"], 3)
        self.assertTrue(result["page_may_have_more"])
        self.assertFalse(result["complete_history"])
        self.assertIsNone(public_orders({"list": []}, 3, 1)["next_page"])
        self.assertTrue(public_orders({"list": [order()]}, 100, 1)["pagination_limit_reached"])

    def test_bad_pagination_duplicate_ids_and_oversized_pages_stop(self):
        for page, limit in ((0, 1), (101, 1), (True, 1), (1, True), (1, 21)):
            with self.assertRaises(EzMcdError): public_orders({"list": []}, page, limit)
        with self.assertRaisesRegex(EzMcdError, "identity_duplicated"):
            public_orders({"list": [order(), order()]}, 1, 10)
        with self.assertRaisesRegex(EzMcdError, "list_schema_changed"):
            public_orders({"list": [order(), order()]}, 1, 1)

    def test_malformed_amount_identity_product_quantity_time_and_depth_fail_closed(self):
        for change in ({"realTotalAmount": "NaN"}, {"orderId": "../../other"}, {"createTime": "yesterday"},
                       {"realTotalAmount": None}, {"orderProductList": [{}]}):
            with self.subTest(change=change), self.assertRaisesRegex(EzMcdError, "response_schema_changed"):
                public_order(order() | change)
        raw = order(); raw["orderProductList"][0]["quantity"] = True
        with self.assertRaises(EzMcdError): public_order(raw)
        raw = order(); leaf = raw["orderProductList"][0]
        for _ in range(5): leaf["comboItemList"] = [{"quantity": 1}]; leaf = leaf["comboItemList"][0]
        with self.assertRaises(EzMcdError): public_order(raw)

    def test_unknown_fulfillment_is_not_mislabelled_as_counter_pickup(self):
        for change in ({"lockerOrder": None}, {"eatType": 2.0}, {"beType": "2"}, {"dtStatus": "drive"}):
            self.assertIsNone(public_order(order() | change, detail=True)["eat_type"])
        self.assertEqual(public_order(order() | {"lockerOrder": False}, detail=True)["eat_type"], "take-in-store")

    def test_provider_uses_only_validated_gets_and_keeps_pay_reference_internal(self):
        transport = Mock()
        transport.send.side_effect = [{"http_status": 200, "response": {"success": True, "data": data}}
                                      for data in ({"list": [order()]}, order(), {"payStatus": "PAID"})]
        provider = McdProvider({}, transport)
        provider.official_orders(1, 10)
        provider.official_order("TEST-1")
        calls = transport.send.call_args_list
        self.assertEqual([c.args[:2] for c in calls], [("GET", "/bff/order/orders"),
            ("GET", "/bff/order/orders/TEST-1"), ("GET", "/bff/cashier/private-pay-reference/status")])
        self.assertEqual(calls[0].args[2], {"orderCategoryId": "0", "page": "1", "size": "10"})
        with self.assertRaises(EzMcdError): provider.official_order("TEST-1?pay=1")
        self.assertEqual(transport.send.call_count, 3)

    def test_wrong_order_response_cannot_trigger_cashier_lookup(self):
        transport = Mock(); transport.send.return_value = {"http_status": 200, "response": {"success": True, "data": order()}}
        with self.assertRaisesRegex(EzMcdError, "identity_changed"):
            McdProvider({}, transport).official_order("TEST-2")
        self.assertEqual(transport.send.call_count, 1)

    def test_cashier_cannot_report_another_order_or_payment_reference(self):
        for key in ("orderId", "payId"):
            transport = Mock()
            transport.send.side_effect = [{"http_status": 200, "response": {"success": True, "data": raw}} for raw in
                                          (order(), {"payStatus": "PAID", key: "ANOTHER"})]
            with self.assertRaisesRegex(EzMcdError, "cashier_identity_changed"):
                McdProvider({}, transport).official_order("TEST-1")
            self.assertEqual(transport.send.call_count, 2)


class OfficialOrderServiceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name); self.store = Store(self.root); setup_demo(self.store)
        self.service = Service(self.store)
        self.when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account="demo", store_code="9000001", pickup_at=self.when.isoformat(), max_amount_cny="20")
        self.quote = self.service.quote(self.args)

    def pay(self):
        return self.service.submit("demo", self.quote["quote_id"], "only-purchase")

    def test_detail_of_known_order_matches_only_declared_fields_without_mutation(self):
        paid = self.pay()
        before = sha256((self.root / "journal.sqlite3").read_bytes()).hexdigest()
        result = self.service.official_order("demo", paid["order_id"], paid["attempt_id"])
        comparison = result["local_attempt_comparison"]
        self.assertTrue(comparison["order_fields_match"])
        self.assertTrue(comparison["already_linked"])
        self.assertFalse(comparison["association_changed"])
        self.assertIn("customizations", comparison["not_verified"])
        self.assertEqual(result["cashier_status"], "PAID")
        self.assertFalse(result["payment_attempted"])
        self.assertEqual(before, sha256((self.root / "journal.sqlite3").read_bytes()).hexdigest())

    def test_lost_create_receipt_can_be_inspected_but_not_guessed_linked_or_paid(self):
        original = DemoProvider.create_order
        def lose(provider, quote):
            original(provider, quote)
            raise EzMcdError("synthetic_lost_reply")
        with patch.object(DemoProvider, "create_order", lose): lost = self.pay()
        self.assertIsNone(lost["order_id"])
        listing = self.service.official_orders("demo")
        identifier = listing["orders"][0]["order_id"]
        result = self.service.official_order("demo", identifier, lost["attempt_id"])
        comparison = result["local_attempt_comparison"]
        self.assertTrue(comparison["order_fields_match"])
        self.assertFalse(comparison["matching_content_proves_origin"])
        self.assertFalse(comparison["already_linked"])
        self.assertEqual(result["cashier_status"], "PAYING")
        saved = self.store.attempt(self.store.account("demo")["identity"], lost["attempt_id"])
        self.assertNotIn("order_id", saved)
        self.assertEqual(saved["state"], "UNKNOWN")
        with self.store.connect() as db: self.assertEqual(dict(db.execute("SELECT stage,count(*) FROM writes GROUP BY stage")), {"order": 1})

    def test_official_only_demo_order_is_visible_without_creating_local_attempt(self):
        _, provider = self.service.provider("demo")
        q = self.store.quote(self.store.account("demo")["identity"], self.quote["quote_id"])
        provider.create_order(q)
        self.assertEqual(self.service.orders("demo")["total"], 0)
        result = self.service.official_orders("demo")
        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(result["source"], "offline_demo")

    def test_other_member_attempt_is_rejected_before_provider_or_network(self):
        paid = self.pay(); setup_demo(self.store, "other")
        with patch.object(Service, "provider", side_effect=AssertionError("No network")):
            with self.assertRaisesRegex(EzMcdError, "attempt_not_found_for_account"):
                self.service.official_order("other", paid["order_id"], paid["attempt_id"])

    def test_changed_amount_or_already_different_order_does_not_match(self):
        paid = self.pay(); _, provider = self.service.provider("demo")
        raw, cashier = provider.official_order(paid["order_id"])
        with patch.object(DemoProvider, "official_order", return_value=(raw | {"realTotalAmount": "19"}, cashier)):
            result = self.service.official_order("demo", paid["order_id"], paid["attempt_id"])
            self.assertFalse(result["local_attempt_comparison"]["order_fields_match"])
        with patch.object(DemoProvider, "official_order", return_value=(raw | {"orderId": "ANOTHER"}, cashier)):
            result = self.service.official_order("demo", "ANOTHER", paid["attempt_id"])
            self.assertEqual(result["local_attempt_comparison"]["reason"], "local_attempt_already_has_another_order")

    def test_missing_cashier_state_does_not_claim_payment_verified(self):
        paid = self.pay(); _, provider = self.service.provider("demo")
        raw, _ = provider.official_order(paid["order_id"])
        with patch.object(DemoProvider, "official_order", return_value=(raw, {})), \
                self.assertRaisesRegex(EzMcdError, "cashier_response_schema_changed"):
            self.service.official_order("demo", paid["order_id"])

    def test_cli_lists_and_inspects_official_order_without_new_purchase(self):
        paid = self.pay()
        for arguments in (["orders-live", "demo", "--page", "1", "--limit", "2"],
                          ["order-view", "demo", paid["order_id"], "--attempt-id", paid["attempt_id"]]):
            with patch.object(sys, "argv", ["ezmcd", "--data-dir", str(self.root), *arguments]), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                main()
            result = json.loads(output.getvalue())
            self.assertTrue(result["simulation"])
            self.assertFalse(result["payment_attempted"])


class OfficialOrderMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_official_read_tools_and_local_comparison_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)); setup_demo(store); service = Service(store)
            when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
            args = CheckoutInput(account="demo", store_code="9000001", pickup_at=when.isoformat(), max_amount_cny="20")
            paid = service.submit("demo", service.quote(args)["quote_id"], "only-purchase")
            params = StdioServerParameters(command=sys.executable, args=["-m", "ezmcd.mcp_server"], env={"EZMCD_DATA_DIR": str(store.root)})
            for _ in range(2):
                async with Client(params) as client:
                    toolmap = {t.name: t for t in (await client.list_tools()).tools}
                    self.assertTrue(toolmap["official_orders_list"].annotations.read_only_hint)
                    self.assertTrue(toolmap["official_order_detail"].annotations.read_only_hint)
                    rows = json.loads((await client.call_tool("official_orders_list", {"account": "demo"})).content[0].text)
                    result = json.loads((await client.call_tool("official_order_detail", {"account": "demo",
                        "order_id": rows["orders"][0]["order_id"], "attempt_id": paid["attempt_id"]})).content[0].text)
                    self.assertEqual(result["cashier_status"], "PAID")
                    self.assertTrue(result["local_attempt_comparison"]["order_fields_match"])
            with store.connect() as db: self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
