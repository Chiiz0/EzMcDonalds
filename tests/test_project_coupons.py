from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from ezmcd.catalog import cart_items
from ezmcd.coupons import (coupon_rows, choose_coupon, public_coupon, coupon_offers,
                          public_coupon_products, verify_coupon_cart)
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, ItemSelection, SHANGHAI
from ezmcd.provider import McdProvider
from ezmcd.products import verify_configured_cart
from ezmcd.transport import Transport


def fixture():
    coupon = {"id": "private-coupon-id", "code": "private-coupon-code", "promotionId": "private-promotion",
        "title": "示例咖啡9.9元", "enable": 1, "useStatus": 1, "currentChannelCanRedeem": True,
        "onlyPosCanRedeem": False, "currentDayAvailableCount": 1, "totalAvailableCount": 1,
        "couponCardType": 0, "couponPromotionType": 1, "isPrepaid": 0, "paymentType": 0,
        "multipleProducts": False, "conditionalProductCodes": [], "specialProductCodes": ["900011"],
        "discountInfo": {"discountDesc": "用券价格", "discountTypeText": "¥", "discountValue": "9.9"},
        "instructions": {"availableTime": [{"text": "05:00-23:59"}], "labels": []},
        "cardNo": "private-never-expose", "jumpUseScenes": "https://example.invalid/private-link"}
    raw = {"couponTabList": [{"tabName": "到店取餐", "coupons": [{"id": "private-group", "coupons": [coupon]}]}]}
    products = {"maxPurchaseQuantity": 99, "multipleProducts": False, "isPrepaid": 0,
        "productList": [{"productCode": "900011", "productName": "示例咖啡", "productType": 1,
            "maxPurchaseQuantity": 0, "saleStatus": 1, "count": 1, "priceInfo": {"discountPrice": 990, "eatInPrice": 1900}}]}
    detail = {"maxPurchaseQuantity": 99, "product": {"code": "900011", "name": "示例咖啡",
        "type": 1, "unsellable": False, "comboItems": []}}
    return raw, products, detail


class CouponTests(unittest.TestCase):
    def setUp(self):
        self.raw, self.products, self.detail = fixture()
        self.coupon = self.raw["couponTabList"][0]["coupons"][0]["coupons"][0]
        self.scope = {"identity": "synthetic-user", "store_code": "9000001", "pickup_at": "future"}
        self.rows = coupon_rows(self.raw, self.scope)
        self.ref = self.rows[0]["coupon_ref"]

    def test_public_rows_and_products_expose_choices_without_coupon_or_card_credentials(self):
        public = public_coupon(self.rows[0])
        self.assertTrue(public["available_for_request"])
        self.assertEqual(public["configuration_path"], "coupon_products")
        self.assertEqual(public["rules"], ["05:00-23:59"])
        offers = coupon_offers(self.rows[0], self.products)
        view = public_coupon_products(self.rows[0], offers)
        self.assertEqual(view["products"][0]["display_price_cny"], "9.90")
        self.assertEqual(view["products"][0]["max_quantity_before_detail_check"], 1)
        self.assertFalse(view["cart_changed"])
        self.assertNotIn("private-", json.dumps(view))

    def test_references_are_bound_to_account_store_and_pickup_time(self):
        self.assertEqual(coupon_rows(self.raw, self.scope)[0]["coupon_ref"], self.ref)
        for field in self.scope:
            rows = coupon_rows(self.raw, self.scope | {field: "different"})
            with self.assertRaisesRegex(EzMcdError, "not_found_for_account_store_time"):
                choose_coupon(rows, self.ref)

    def test_unavailable_channel_exhausted_or_unrecognized_status_never_selects_coupon(self):
        for field, value in (("enable", 0), ("enable", True), ("useStatus", 0),
                             ("currentChannelCanRedeem", False), ("onlyPosCanRedeem", True),
                             ("currentDayAvailableCount", 0), ("totalAvailableCount", -1)):
            with self.subTest(field=field):
                row = self.rows[0] | {field: value}
                self.assertFalse(public_coupon(row)["available_for_request"])
                with self.assertRaisesRegex(EzMcdError, "unavailable"):
                    choose_coupon([row], self.ref)

    def test_card_prepaid_conditional_and_multi_product_coupons_require_supported_path(self):
        for changes in ({"cardId": "private-card", "couponCardType": 2}, {"isPrepaid": 1},
                        {"conditionalProductCodes": ["900099"]}, {"multipleProducts": True},
                        {"couponPromotionType": 2}, {"paymentType": 1}):
            row = self.rows[0] | changes
            offers = coupon_offers(row, self.products)
            self.assertTrue(offers[0]["_coupon_configuration_issues"])
            self.assertFalse(public_coupon_products(row, offers)["products"][0]["cart_configuration_supported"])
        self.assertEqual(public_coupon(self.rows[0] | {"cardId": "private-card"})["configuration_path"], "menu_card_offer")

    def test_bundle_free_sold_out_unsupported_and_required_multi_quantity_products_stop(self):
        for changes in ({"multipleProducts": True}, {"isPrepaid": 1}, {"maxPurchaseQuantity": 0}):
            self.assertTrue(coupon_offers(self.rows[0], self.products | changes)[0]["_coupon_configuration_issues"])
        for change in ({"count": 2}, {"saleStatus": 0}, {"productType": 3}, {"priceInfo": {"discountPrice": 0}}):
            data = deepcopy(self.products)
            data["productList"][0].update(change)
            self.assertTrue(coupon_offers(self.rows[0], data)[0]["_coupon_configuration_issues"])

    def test_malformed_lists_and_duplicate_product_codes_stop(self):
        for value in ({}, {"couponTabList": None}, {"couponTabList": [{"coupons": [1]}]}):
            with self.assertRaisesRegex(EzMcdError, "schema_changed"):
                coupon_rows(value, self.scope)
        self.products["productList"].append(deepcopy(self.products["productList"][0]))
        with self.assertRaisesRegex(EzMcdError, "schema_changed"):
            coupon_offers(self.rows[0], self.products)

    def test_repeated_coupon_groups_merge_only_if_the_official_evidence_agrees(self):
        tab = deepcopy(self.raw["couponTabList"][0]); tab["tabName"] = "其他分类"
        self.raw["couponTabList"].append(tab)
        rows = coupon_rows(self.raw, self.scope)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["categories"], ["到店取餐", "其他分类"])
        tab["coupons"][0]["coupons"][0]["enable"] = 0
        with self.assertRaisesRegex(EzMcdError, "schema_changed"):
            coupon_rows(self.raw, self.scope)

    def test_coupon_identity_change_alters_cart_and_quote_content(self):
        product = {"code": "900011", "name": "示例咖啡", "type": 1, "quantity": 1,
                   "realSubTotalPrice": 990, "coupon": self.coupon}
        before = cart_items({"products": [product]})
        product["coupon"] = self.coupon | {"id": "private-another-coupon"}
        self.assertNotEqual(before, cart_items({"products": [product]}))

    def test_opaque_ref_is_not_a_means_to_supply_upstream_identifiers(self):
        with self.assertRaises(ValidationError):
            ItemSelection(product_code="900011", coupon_ref=self.ref, couponId="private-injected-id")
        for ref in ("private-coupon-id", "../coupon", "coupon-" + "0" * 25):
            with self.assertRaises(EzMcdError): choose_coupon(self.rows, ref)

    def test_cart_must_echo_exact_coupon_product_and_quantity_without_leaking_ids(self):
        expected = {"code": "900011", "quantity": 1, "couponId": self.coupon["id"],
                    "couponCode": self.coupon["code"], "promotionId": self.coupon["promotionId"], "useRightCard": False}
        product = {"code": "900011", "name": "示例咖啡", "type": 1, "quantity": 1,
                   "realSubTotalPrice": 990, "coupon": self.coupon}
        rows = cart_items({"products": [product]})
        verify_coupon_cart(expected, rows)
        verify_configured_cart({"products": [expected]}, {"items": rows})
        self.assertNotIn("private-", json.dumps(rows))
        for change in ({"coupon_fingerprint": None}, {"quantity": 2}, {"product_code": "900012"}):
            with self.assertRaisesRegex(EzMcdError, "coupon_not_confirmed"):
                verify_coupon_cart(expected, [rows[0] | change])
        changed = deepcopy(product); changed["coupon"]["id"] = "private-another"
        with self.assertRaisesRegex(EzMcdError, "coupon_not_confirmed"):
            verify_coupon_cart(expected, cart_items({"products": [changed]}))


class CouponProviderTests(unittest.TestCase):
    def setUp(self):
        self.raw, self.products, self.detail = fixture()
        self.http = Mock()
        self.http.send.return_value = {"http_status": 200, "response": {"success": True, "data": self.detail}}
        self.provider = McdProvider({"identity": "test-member"}, self.http)
        when = (datetime.now(SHANGHAI)+timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account="test", store_code="9000001", pickup_at=when.isoformat(), max_amount_cny="20")
        self.params = {"storeCode": "9000001", "orderType": "1", "orderMode": "", "beCode": "",
                       "date": when.date().isoformat(), "time": "10:15"}
        self.cart = {"storeCode": "9000001", "beType": 1, "orderType": 1, "products": []}
        self.provider.context = Mock(return_value=("1", self.params))
        def get(path, params=None):
            return {"/bff/promotion/coupons/menu/available": self.raw,
                    "/bff/promotion/coupons/products": self.products, "/bff/cart/carts": self.cart}[path]
        self.provider.get = Mock(side_effect=get)
        self.ref = self.provider.coupons(self.args)["coupons"][0]["coupon_ref"]
        self.public = self.provider.coupon_products(self.args, self.ref)
        self.offer = self.public["products"][0]["offer_id"]
        self.selection = ItemSelection(product_code="900011", offer_id=self.offer, coupon_ref=self.ref)
        self.provider.get.reset_mock()

    def test_query_uses_fresh_coupon_ids_and_official_store_time_parameters(self):
        view = self.provider.coupon_products(self.args, self.ref)
        self.assertEqual(view["products"][0]["display_price_cny"], "9.90")
        self.provider.get.assert_any_call("/bff/promotion/coupons/menu/available", self.params | {
            "cardId": "", "beType": "1", "isPromoter": "0", "daypartCode": "1", "scene": ""})
        self.provider.get.assert_any_call("/bff/promotion/coupons/products", self.params | {
            "channelCode": "03", "couponCode": "private-coupon-code", "couponId": "private-coupon-id",
            "promotionId": "private-promotion", "daypartCodes": "1"})
        self.http.send.assert_not_called()

    def test_detail_and_cart_draft_preserve_selected_coupon_and_live_quantity_limit(self):
        detail = self.provider.product_detail(self.args, "900011", self.offer, self.ref)
        self.assertEqual(detail["max_quantity"], 1)
        self.assertTrue(detail["standard_configuration_supported"])
        self.assertEqual(detail["coupon_ref"], self.ref)
        draft = self.provider.configured_cart_draft(self.args, [self.selection])
        product = draft["products"][0]
        self.assertEqual(product["couponId"], "private-coupon-id")
        self.assertEqual(product["couponCode"], "private-coupon-code")
        self.assertEqual(product["promotionId"], "private-promotion")
        self.assertFalse(product["useRightCard"])
        self.assertNotIn("price", product)
        self.assertNotIn("_coupon_source", product)
        for call in self.http.send.call_args_list:
            self.assertEqual(call.args[:2], ("POST", "/bff/spc/products/detail/900011"))

    def test_no_fallback_when_coupon_consumed_or_changed_after_listing(self):
        coupon = self.raw["couponTabList"][0]["coupons"][0]["coupons"][0]
        for change in ({"enable": 0}, {"totalAvailableCount": 0}, {"id": "private-new-id"}):
            original = deepcopy(coupon); coupon.update(change)
            self.http.send.reset_mock()
            with self.assertRaises(EzMcdError):
                self.provider.configured_cart_draft(self.args, [self.selection])
            self.http.send.assert_not_called()
            coupon.clear(); coupon.update(original)

    def test_quantity_duplicate_coupon_wrong_offer_and_nonempty_cart_stop_without_writes(self):
        for selections in ([self.selection.model_copy(update={"quantity": 2})], [self.selection, self.selection],
                           [self.selection.model_copy(update={"offer_id": "not-current-offer"})]):
            with self.assertRaises(EzMcdError): self.provider.configured_cart_draft(self.args, selections)
        self.cart["products"] = [{"code": "existing"}]
        with self.assertRaisesRegex(EzMcdError, "cart_not_empty"):
            self.provider.configured_cart_draft(self.args, [self.selection])
        self.assertFalse(any(c.args[0] == "PUT" for c in self.http.send.call_args_list))

    def test_coupon_cannot_be_silently_ignored_by_simple_cart_route(self):
        with self.assertRaisesRegex(EzMcdError, "use_cart_add_configured"):
            self.provider.simple_cart_draft(self.args, [self.selection])

    def test_only_coupon_query_routes_are_allowlisted_not_claim_bind_or_post(self):
        transport = Transport({}); transport.signer = Mock()
        transport.signer.headers.return_value = {}
        with patch.object(transport, "_open", return_value={}) as send:
            for path in ("/bff/promotion/coupons/menu/available", "/bff/promotion/coupons/products"):
                transport.send("GET", path, {"storeCode": "9000001"})
                with self.assertRaises(EzMcdError): transport.send("POST", path, body={})
            for path in ("/bff/member/coupon/bind", "/bff/promotion/coupons/subscribe"):
                with self.assertRaises(EzMcdError): transport.send("GET", path)
            self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
