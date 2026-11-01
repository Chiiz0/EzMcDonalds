from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from ezmcd.catalog import checkout, pickup_methods, cart_regular_subtotal
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput
from ezmcd.provider import McdProvider


class PickupMethodsTests(unittest.TestCase):
    def setUp(self):
        self.args = CheckoutInput(account="test", store_code="9000001",
            pickup_at="2099-01-01T10:15:00+08:00", max_amount_cny="20")
        self.confirmation = {"submit": 1, "orderType": "1", "storeInfo": {"storeCode": "9000001"},
            "dayPart": {"dayPartCode": "2"}, "storePickUpInfo": {
                "pickupExpectTimeValue": "2099-01-01 10:15", "eatTypeOptions": [
                    {"code": "eat-in", "text": "堂食", "subText": "店内用餐", "selected": True},
                    {"code": "take-in-store", "text": "外带", "subText": "店内自提"}]}}

    def test_counter_option_is_not_reported_as_locker_support(self):
        before = deepcopy(self.confirmation)
        result = pickup_methods(self.args, self.confirmation, "2")
        self.assertFalse(result["requested_method_available"])
        self.assertFalse(result["requested_method_selected"])
        self.assertEqual(result["selected_method"], "eat-in")
        self.assertEqual(result["next_action"], "use_official_app_or_choose_supported_store")
        self.assertTrue(all(not row["checkout_supported"] for row in result["methods"]))
        self.assertEqual(self.confirmation, before)

    def test_available_and_selected_are_separate(self):
        rows = self.confirmation["storePickUpInfo"]["eatTypeOptions"]
        rows.append({"code": "locker-in", "text": "外带", "subText": "店内取餐柜"})
        result = pickup_methods(self.args, self.confirmation, "2")
        self.assertTrue(result["requested_method_available"])
        self.assertFalse(result["requested_method_selected"])
        self.assertEqual(result["next_action"], "select_requested_method_in_official_app")
        rows[0]["selected"] = False
        rows[2]["selected"] = True
        result = pickup_methods(self.args, self.confirmation, "2")
        self.assertTrue(result["requested_method_selected"])
        self.assertEqual(result["next_action"], "checkout_quote")

    def test_ambiguous_or_invalid_options_stop(self):
        for rows in (None, [], [{}], ["locker-in"], [{"code": "locker-in", "selected": 1}],
                     [{"code": "locker-in"}, {"code": "locker-in"}],
                     [{"code": "eat-in", "selected": True}, {"code": "locker-in", "selected": True}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(EzMcdError, "pickup_methods_schema_changed"):
                self.confirmation["storePickUpInfo"]["eatTypeOptions"] = rows
                pickup_methods(self.args, self.confirmation, "2")

    def test_store_time_and_daypart_cannot_be_mixed(self):
        for group, key, value in (("storeInfo", "storeCode", "9000002"),
                                  ("dayPart", "dayPartCode", "3"),
                                  ("storePickUpInfo", "pickupExpectTimeValue", "2099-01-01 10:20")):
            changed = deepcopy(self.confirmation)
            changed[group][key] = value
            with self.subTest(key=key), self.assertRaises(EzMcdError):
                pickup_methods(self.args, changed, "2")

    def test_provider_only_reads_confirmation_and_does_not_send_a_switch(self):
        http = Mock()
        http.send.return_value = {"http_status": 200, "response": {"success": True, "data": self.confirmation}}
        provider = McdProvider({}, http)
        with patch.object(provider, "context", return_value=("2", {"date": "2099-01-01", "time": "10:15"})):
            result = provider.pickup_methods(self.args)
        self.assertEqual(http.send.call_count, 1)
        self.assertEqual(http.send.call_args.args[:2], ("GET", "/bff/order/confirmation/info"))
        self.assertFalse(result["order_created"])
        self.assertFalse(result["payment_attempted"])


class CreatedOrderComponentsTests(unittest.TestCase):
    def setUp(self):
        self.attempt = {"order_id": "order-test", "pay_id": "pay-test", "wallet_before_cents": 10000}
        self.quote = {"store_code": "9000001", "pickup_at": "2099-01-01T10:15:00+08:00", "amount_cents": 1800,
            "items": [{"product_code": code, "quantity": qty,
                       "components": [{"round": 1, "comboProducts": [{"code": part, "quantity": 1}]}]}
                      for code, qty, part in (("7001", 1, "101"), ("7002", 2, "102"))]}
        self.order = {"orderId": "order-test", "payId": "pay-test", "storeCode": "9000001", "lockerOrder": True,
            "expectPickUpTime": "2099-01-01 10:15:00", "realTotalAmount": "18.00", "realPayAmount": "",
            "orderStatusTitle": "待支付", "leftPaySecond": 500,
            "orderProductList": [{"productCode": code, "quantity": qty,
                                  "comboItemList": [{"productCode": part, "quantity": 1}]}
                                 for code, qty, part in (("7001", 1, "101"), ("7002", 2, "102"))]}

    def validate(self):
        McdProvider.validate_order(self.attempt, self.quote, self.order)

    def test_row_order_can_change_without_changing_meal(self):
        self.order["orderProductList"].reverse()
        self.validate()

    def test_components_swapped_between_distinct_products_are_rejected(self):
        rows = self.order["orderProductList"]
        rows[0]["comboItemList"], rows[1]["comboItemList"] = rows[1]["comboItemList"], rows[0]["comboItemList"]
        with self.assertRaisesRegex(EzMcdError, "created_order_components_changed"):
            self.validate()

    def test_component_configuration_stays_bound_to_parent_quantity(self):
        self.quote["items"][1]["product_code"] = "7001"
        rows = self.order["orderProductList"]
        rows[1]["productCode"] = "7001"
        self.validate()
        rows[0]["comboItemList"], rows[1]["comboItemList"] = rows[1]["comboItemList"], rows[0]["comboItemList"]
        with self.assertRaisesRegex(EzMcdError, "created_order_components_changed"):
            self.validate()

    def test_missing_extra_or_invalid_component_quantities_stop(self):
        for items in ([], [{"productCode": "101", "quantity": 1}] * 2,
                      [{"productCode": "101", "quantity": True}], [{"productCode": "101", "quantity": 0}],
                      [{"name": "unverified display row", "quantity": 1}], None):
            changed = deepcopy(self.order)
            changed["orderProductList"][0]["comboItemList"] = items
            with self.subTest(items=items), self.assertRaisesRegex(EzMcdError, "created_order_components_changed"):
                McdProvider.validate_order(self.attempt, self.quote, changed)

    def test_boolean_parent_quantity_cannot_compare_equal_to_one(self):
        self.order["orderProductList"][0]["quantity"] = True
        with self.assertRaisesRegex(EzMcdError, "created_order_does_not_match_quote"):
            self.validate()

    def test_changed_components_stop_before_cashier_channel_or_preorder(self):
        self.order["orderProductList"][0]["comboItemList"][0]["productCode"] = "102"
        self.order["orderProductList"][1]["comboItemList"][0]["productCode"] = "101"
        provider = McdProvider({}, Mock())
        with patch.object(provider, "status", return_value={"order": self.order,
                "cashier": {"payStatus": "PAYING"}, "wallet_cents": 10000}):
            with self.assertRaisesRegex(EzMcdError, "created_order_components_changed"):
                provider.payment_ready(self.attempt, self.quote)
        provider.http.send.assert_not_called()


class OrdinaryCartPriceTests(unittest.TestCase):
    def setUp(self):
        self.args = CheckoutInput(account="test", store_code="9000001",
            pickup_at="2099-01-01T10:15:00+08:00", max_amount_cny="20")
        self.product = {"sequence": 1, "code": "1001", "name": "Sample", "type": 1,
            "quantity": 2, "saleStatus": 1, "originPrice": 850, "realPrice": 850, "realSubTotalPrice": 1700}
        self.cart = {"storeCode": "9000001", "beType": 1, "orderType": 1, "submit": 1, "daypartCode": "2",
            "realTotalPrice": 1700, "totalPrice": 1700, "discountAmount": 0, "products": [self.product]}
        self.row = {"sequence": 1, "productCode": "1001", "productName": "Sample", "productType": 1,
            "quantity": 2, "saleStatus": 1, "uniqueKey": "test-row", "price": "8.50",
            "realSubtotal": "17.00", "subtotal": "17.00", "productImage": "", "comboItemList": []}
        self.confirmation = {"submit": 1, "orderType": "1", "storeInfo": {"storeCode": "9000001"},
            "dayPart": {"dayPartCode": "2"}, "storePickUpInfo": {
                "pickupExpectTimeValue": "2099-01-01 10:15", "eatTypeOptions": [{"code": "locker-in", "selected": True}]},
            "productPriceInfo": {"realTotalAmount": "17.00", "totalAmount": "17.00", "discountAmount": "0",
                                 "cartProductList": [self.row]},
            "tablewareInfo": {"options": [{"code": "no", "selected": True}]}}

    def test_multiple_quantity_without_redundant_subtotal_is_fully_quoted(self):
        q = checkout(self.args, self.cart, self.confirmation, "2")
        self.assertEqual(q["amount_cents"], 1700)
        self.assertEqual(q["items"][0]["quantity"], 2)
        self.assertEqual(q["body"]["cartItems"][0]["quantity"], 2)

    def test_two_distinct_rows_keep_independent_unit_prices_and_sequences(self):
        self.product.update(quantity=1, realSubTotalPrice=850)
        self.row.update(quantity=1, realSubtotal="8.50", subtotal="8.50")
        self.cart["products"].append(self.product | {"code": "1002", "sequence": 2})
        self.confirmation["productPriceInfo"]["cartProductList"].append(
            self.row | {"productCode": "1002", "sequence": 2, "uniqueKey": "test-row-2"})
        self.confirmation["productPriceInfo"]["cartProductList"].reverse()
        q = checkout(self.args, self.cart, self.confirmation, "2")
        self.assertEqual(q["amount_cents"], 1700)
        self.assertEqual(len(q["items"]), 2)

    def test_unit_prices_do_not_override_contradicting_confirmation(self):
        self.row["subtotal"] = "18.00"
        with self.assertRaisesRegex(EzMcdError, "confirmation_product_mismatch"):
            checkout(self.args, self.cart, self.confirmation, "2")

    def test_confirmation_unit_price_must_also_match_the_cart(self):
        self.row["price"] = "9.00"
        with self.assertRaisesRegex(EzMcdError, "confirmation_product_mismatch"):
            checkout(self.args, self.cart, self.confirmation, "2")

    def test_incomplete_inconsistent_or_unobserved_fallback_stops(self):
        for change in ({"originPrice": None}, {"originPrice": True}, {"originPrice": 900}, {"realPrice": "850"}, {"realPrice": 800},
                       {"realSubTotalPrice": 850}, {"subTotalPrice": None}, {"type": 7},
                       {"coupon": {"id": "test-coupon"}}, {"customization": {"items": [{}]}},
                       {"comboItems": [{}]}, {"modification": {"values": [{}]}}, {"grills": [{}]}):
            with self.subTest(change=change), self.assertRaisesRegex(EzMcdError, "cart_product_price_structure_unverified"):
                cart_regular_subtotal(self.product | change)
