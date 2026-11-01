from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from ezmcd.card_variants import CARD_FIELDS, matching_card, resolve_card_variant
from ezmcd.catalog import menu_products, verify_confirmation_components
from ezmcd.coupons import coupon_signature
from ezmcd.errors import EzMcdError
from ezmcd.models import ItemSelection
from ezmcd.preferences import MealPreferences, plan_meal, save_preferences
from ezmcd.provider import McdProvider
from ezmcd.products import verify_cart_contents
from ezmcd.service import Service
import test_project_variants as variants


def card():
    return {"cardId": "synthetic-card", "id": "synthetic-coupon", "code": "synthetic-code",
        "promotionId": "synthetic-promotion", "title": "示例咖啡卡", "enable": 1, "useStatus": 1,
        "currentChannelCanRedeem": True, "onlyPosCanRedeem": False,
        "currentDayAvailableCount": 2, "totalAvailableCount": 10,
        "couponCardType": 2, "couponPromotionType": 1, "isPrepaid": 0, "paymentType": 0,
        "multipleProducts": False, "conditionalProductCodes": ["100001"], "specialProductCodes": ["100001"]}


def products():
    return {"multipleProducts": True, "isPrepaid": 0, "maxPurchaseQuantity": 99,
        "productList": [{"productCode": "100001", "productType": 1, "count": 1, "saleStatus": 1,
            "maxPurchaseQuantity": 0, "priceInfo": {"eatInPrice": 1000, "discountPrice": 500}}]}


class CardVariantDemo(variants.VariantDemo):
    _card_variant_evidence = McdProvider._card_variant_evidence

    def __init__(self, account, store):
        super().__init__(account, store)
        self.card = card()
        self.eligible = products()
        self.evidence_calls = []
        self.menu_raw["menu"][0]["productList"][0].update(cardId=self.card["cardId"], cardType=2,
            couponId=self.card["id"], couponCode=self.card["code"], promotionId=self.card["promotionId"],
            cardName="菜单泛称", priceInfo={"eatInPrice": 500}, productPromotions=[{
                "productCode": "100001", "promotionInfo": {"cardId": self.card["cardId"],
                    "promotionId": self.card["promotionId"], "cardType": 2, "pmtType": 1,
                    "hasRights": True, "withOrder": False, "promotionLimitNum": 2}}])

    def _coupons(self, args, daypart, params):
        self.evidence_calls.append("member")
        return [deepcopy(self.card)]

    def get(self, path, params):
        if path == "/bff/promotion/coupons/products":
            self.evidence_calls.append("products")
            assert params["couponId"] == self.card["id"] and params["couponCode"] == self.card["code"]
            assert params["promotionId"] == self.card["promotionId"]
            return deepcopy(self.eligible)
        return super().get(path, params)


class CardVariantTests(unittest.TestCase):
    def setUp(self):
        variants.VariantTests.setUp(self)
        self.provider = CardVariantDemo(self.store.account("demo"), self.store)
        self.offer = menu_products(self.provider.menu_raw)[0]
        self.selection = ItemSelection(product_code="100001", variant_of="G900", offer_id=self.offer["offer_id"])

    def resolve(self, **changes):
        return resolve_card_variant(changes.get("detail", variants.detail()), changes.get("offer", self.offer),
            changes.get("code", "100001"), (changes.get("card", card()), changes.get("products", products())))

    def test_parent_lists_only_explicitly_eligible_variants_and_fetches_evidence_once(self):
        view = self.provider.product_detail(self.args, "G900", self.offer["offer_id"])
        small, large = view["variants"]
        self.assertIsNotNone(small["parent_detail_selection"])
        self.assertEqual((small["display_price_cny"], small["regular_display_price_cny"], small["card_name"]),
                         ("5.00", "10.00", "示例咖啡卡"))
        self.assertIsNone(large["parent_detail_selection"])
        self.assertEqual(large["parent_detail_issue"], "variant_not_eligible_for_selected_card")
        self.assertEqual(self.provider.evidence_calls, ["member", "products"])
        for identifier in ("synthetic-card", "synthetic-coupon", "synthetic-code", "synthetic-promotion"):
            self.assertNotIn(identifier, json.dumps(view))

    def test_selected_detail_reports_discount_without_claiming_final_quote(self):
        view = self.provider.product_detail(self.args, "100001", self.offer["offer_id"], variant_of="G900")
        self.assertEqual(view["pricing_source"], "verified_card_variant")
        self.assertEqual(view["configuration_base_cny"], "5.00")
        self.assertEqual(view["regular_display_price_cny"], "10.00")
        self.assertEqual(view["max_quantity"], 2)
        self.assertFalse(view["display_price_is_final_quote"])

    def test_add_reloads_member_eligibility_and_preserves_exact_discount_identity(self):
        self.provider.product_detail(self.args, "G900", self.offer["offer_id"])
        draft = self.provider.configured_cart_draft(self.args, [self.selection])
        product = draft["products"][0]
        self.assertTrue(product["useRightCard"])
        self.assertEqual({k: product[k] for k in CARD_FIELDS}, {k: self.offer[k] for k in CARD_FIELDS})
        self.assertEqual(self.provider.evidence_calls, ["member", "products"] * 2)
        self.provider.card["currentDayAvailableCount"] = 0
        with self.assertRaisesRegex(EzMcdError, "card_not_available"):
            self.provider.configured_cart_draft(self.args, [self.selection])

    def test_card_and_menu_identity_must_match_all_four_fields(self):
        for field in ("cardId", "id", "code", "promotionId"):
            changed = card(); changed[field] = "another-member-or-card"
            with self.subTest(field=field), self.assertRaisesRegex(EzMcdError, "card_not_available"):
                self.resolve(card=changed)
        for rows in ([], [card(), card()], [None], None):
            with self.assertRaises(EzMcdError): matching_card(rows, self.offer)

    def test_expired_disabled_channel_ineligible_and_exhausted_cards_stop(self):
        for field, value in (("enable", 0), ("useStatus", 0), ("currentChannelCanRedeem", False),
            ("onlyPosCanRedeem", True), ("currentDayAvailableCount", 0), ("totalAvailableCount", 0),
            ("currentDayAvailableCount", True), ("totalAvailableCount", "10")):
            changed = card(); changed[field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(EzMcdError, "card_not_available"):
                self.resolve(card=changed)

    def test_unobserved_card_types_and_prepaid_bundles_are_not_reinterpreted(self):
        for field, value in (("couponCardType", 0), ("couponPromotionType", 2), ("isPrepaid", 1),
            ("paymentType", 1), ("multipleProducts", True), ("title", None)):
            with self.subTest(field=field), self.assertRaisesRegex(EzMcdError, "offer_not_supported"):
                self.resolve(card=card() | {field: value})
        for update in ({"cardType": True}, {"couponId": ""}, {"_coupon_source": True}):
            with self.assertRaisesRegex(EzMcdError, "offer_not_supported"): self.resolve(offer=self.offer | update)

    def test_eligibility_lists_must_agree_without_duplicates(self):
        for name in ("conditionalProductCodes", "specialProductCodes"):
            for value in ([], ["100002"], ["100001", "100001"], [None], "100001"):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(EzMcdError, "product_list_changed"):
                    self.resolve(card=card() | {name: value})

    def test_mixed_or_malformed_eligible_product_lists_stop(self):
        for case in ("duplicate", "type", "count", "bundle", "prepaid", "limit", "missing", "object"):
            raw = products()
            if case == "duplicate": raw["productList"] *= 2
            if case == "type": raw["productList"][0]["productType"] = 7
            if case == "count": raw["productList"][0]["count"] = 2
            if case == "bundle": raw["multipleProducts"] = False
            if case == "prepaid": raw["isPrepaid"] = 1
            if case == "limit": raw["maxPurchaseQuantity"] = True
            if case == "missing": raw.pop("productList")
            if case == "object": raw["productList"] = [None]
            with self.subTest(case=case), self.assertRaisesRegex(EzMcdError, "product_list_changed"):
                self.resolve(products=raw)

    def test_missing_or_unsellable_variant_cannot_use_parent_price(self):
        with self.assertRaisesRegex(EzMcdError, "not_eligible"): self.resolve(code="100002")
        raw = products(); raw["productList"][0]["saleStatus"] = 0
        with self.assertRaisesRegex(EzMcdError, "not_eligible"): self.resolve(products=raw)
        with self.assertRaisesRegex(EzMcdError, "availability_changed"): self.resolve(offer=self.offer | {"unsellable": True})
        detail = variants.detail(); detail["product"]["products"][0]["unsellable"] = True
        with self.assertRaisesRegex(EzMcdError, "requires_official_configuration"): self.resolve(detail=detail)

    def test_prices_are_exact_positive_cents_and_regular_detail_must_agree(self):
        for value in (0, -1, True, "500", 1001):
            raw = products(); raw["productList"][0]["priceInfo"]["discountPrice"] = value
            with self.subTest(value=value), self.assertRaisesRegex(EzMcdError, "product_list_changed"):
                self.resolve(products=raw)
        raw = products(); raw["productList"][0]["priceInfo"]["eatInPrice"] = 1100
        with self.assertRaisesRegex(EzMcdError, "regular_price_mismatch"): self.resolve(products=raw)

    def test_remaining_uses_and_per_product_limits_are_enforced(self):
        for target in ("currentDayAvailableCount", "totalAvailableCount"):
            _, detail = self.resolve(card=card() | {target: 1})
            self.assertEqual(detail["maxPurchaseQuantity"], 1)
        raw = products(); raw["productList"][0]["maxPurchaseQuantity"] = 1
        self.assertEqual(self.resolve(products=raw)[1]["maxPurchaseQuantity"], 1)
        with self.assertRaisesRegex(EzMcdError, "quantity_exceeds_live_limit"):
            self.provider.configured_cart_draft(self.args, [self.selection.model_copy(update={"quantity": 3})])
        raw["productList"][0]["maxPurchaseQuantity"] = -1
        with self.assertRaisesRegex(EzMcdError, "product_list_changed"): self.resolve(products=raw)

    def test_child_discount_fields_cannot_override_verified_card(self):
        detail = variants.detail(); detail["product"]["products"][0]["couponId"] = "another-coupon"
        with self.assertRaisesRegex(EzMcdError, "discount_requires_own_menu_offer"): self.resolve(detail=detail)

    def test_combined_card_quantities_cannot_exceed_remaining_uses(self):
        self.provider.card["conditionalProductCodes"].append("100002")
        self.provider.card["specialProductCodes"].append("100002")
        self.provider.eligible["productList"].append(products()["productList"][0] |
            {"productCode": "100002", "priceInfo": {"eatInPrice": 1500, "discountPrice": 500}})
        promos = self.provider.menu_raw["menu"][0]["productList"][0]["productPromotions"]
        promos.append(deepcopy(promos[0]) | {"productCode": "100002"})
        second = self.selection.model_copy(update={"product_code": "100002"})
        self.assertEqual(len(self.provider.configured_cart_draft(self.args, [self.selection, second])["products"]), 2)
        with self.assertRaisesRegex(EzMcdError, "cart_quantity_exceeds_remaining_uses"):
            self.provider.configured_cart_draft(self.args, [self.selection.model_copy(update={"quantity": 2}), second])

    def test_parent_promotion_must_bind_the_selected_product_to_the_same_card(self):
        for key, value in (("cardId", "other"), ("promotionId", "other"), ("cardType", True),
            ("pmtType", 2), ("hasRights", False), ("withOrder", True), ("promotionLimitNum", 0)):
            offer = deepcopy(self.offer); offer["productPromotions"][0]["promotionInfo"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(EzMcdError, "promotion_binding_changed"):
                self.resolve(offer=offer)
        for value in (None, [], [{}], self.offer["productPromotions"] * 2):
            with self.assertRaisesRegex(EzMcdError, "promotion_binding_changed"):
                self.resolve(offer=self.offer | {"productPromotions": value})
        offer = deepcopy(self.offer); offer["productPromotions"][0]["promotionInfo"]["promotionLimitNum"] = 1
        self.assertEqual(self.resolve(offer=offer)[1]["maxPurchaseQuantity"], 1)

    def test_split_cart_rows_cannot_bypass_variant_promotion_limit(self):
        row = self.provider.menu_raw["menu"][0]["productList"][0]
        row["productPromotions"][0]["promotionInfo"]["promotionLimitNum"] = 1
        with self.assertRaisesRegex(EzMcdError, "product_quantity_exceeds_live_limit"):
            self.provider.configured_cart_draft(self.args, [self.selection, self.selection])

    def test_cart_echo_cannot_switch_discount_or_fall_back_to_regular_price(self):
        draft = self.provider.configured_cart_draft(self.args, [self.selection])
        c = card()
        item = {"product_code": "100001", "product_type": "1", "quantity": 1, "components": [],
            "configuration": {}, "coupon_fingerprint": coupon_signature(c | {"rightCardId": c["cardId"]})}
        verify_cart_contents(draft, {"items": [item]})
        for value in (None, coupon_signature(c | {"rightCardId": "another-card"}),
                      coupon_signature(c | {"rightCardId": c["cardId"], "id": "another-coupon"})):
            with self.assertRaisesRegex(EzMcdError, "contents_do_not_match"):
                verify_cart_contents(draft, {"items": [item | {"coupon_fingerprint": value}]})

    def test_unavailable_card_parent_is_explainable_but_explicit_selection_raises(self):
        self.provider.card["enable"] = 0
        view = self.provider.product_detail(self.args, "G900", self.offer["offer_id"])
        self.assertTrue(all(v["parent_detail_selection"] is None for v in view["variants"]))
        self.assertEqual(self.provider.evidence_calls, ["member"])
        with self.assertRaisesRegex(EzMcdError, "card_not_available"):
            self.provider.product_detail(self.args, "100001", self.offer["offer_id"], variant_of="G900")

    def plan(self, **updates):
        prefs = {"store_code": "9000001", "pickup_time": "10:15", "max_amount_cny": "20",
            "require_card_discount": True, "items": [{"name": "演示汉堡", "variant_of": "演示规格"}]}
        save_preferences(self.store, MealPreferences.model_validate(prefs | updates))
        with patch.object(Service, "provider", return_value=(self.store.account("demo"), self.provider)):
            return plan_meal(Service(self.store), "demo", self.when.date().isoformat())

    def test_preferences_use_fresh_card_price_and_do_not_write_cart(self):
        result = self.plan()
        self.assertTrue(result["selection_ready"])
        self.assertEqual(result["estimated_amount_cny"], "5.00")
        self.assertEqual(result["products"][0]["card_name"], "示例咖啡卡")
        self.assertEqual(result["items"][0]["variant_of"], "G900")
        self.assertFalse(result["cart_changed"])
        with self.store.connect() as db: self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 0)

    def test_preferences_do_not_silently_fall_back_or_ignore_budget(self):
        for updates in ({"max_amount_cny": "4"}, {"require_card_discount": False},
            {"items": [{"name": "演示大汉堡", "variant_of": "演示规格"}]},
            {"items": [{"name": "演示汉堡", "variant_of": "演示规格", "coupon_name": "另一张券"}]}):
            with self.subTest(updates=updates): self.assertFalse(self.plan(**updates)["selection_ready"])


class StandardIceConfirmationTests(unittest.TestCase):
    def product(self):
        return {"type": 1, "customization": {"options": [], "items": [
            {"code": "200002", "key": "0-1", "name": "冰",
             "values": [{"code": "1", "name": "标准", "price": 0, "quantity": 1}]}]}}

    def test_observed_standard_ice_has_no_confirmation_label(self):
        verify_confirmation_components(self.product(), [])

    def test_missing_label_is_not_permission_to_ignore_changed_or_paid_ice(self):
        for target, field, value in (("item", "code", "other"), ("item", "key", "0-2"),
            ("item", "name", "other"), ("item", "displayText", None), ("value", "name", "少冰"),
            ("value", "code", "2"), ("value", "price", 200), ("value", "price", True)):
            p = self.product(); item = p["customization"]["items"][0]
            (item if target == "item" else item["values"][0])[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(EzMcdError):
                verify_confirmation_components(p, [])

    def test_missing_default_label_does_not_allow_unexpected_confirmation_rows(self):
        for rows in ([{"name": "少冰", "quantity": 1}], [{"productCode": "100001", "quantity": 1}]):
            with self.assertRaisesRegex(EzMcdError, "confirmation_components_mismatch"):
                verify_confirmation_components(self.product(), rows)

    def test_other_selected_labels_still_require_exact_confirmation(self):
        p = self.product(); item = p["customization"]["items"][0]
        item["displayText"] = "冰(标准)"
        verify_confirmation_components(p, [{"name": "冰(标准)", "quantity": 1}])
        with self.assertRaises(EzMcdError): verify_confirmation_components(p, [])


if __name__ == "__main__":
    unittest.main()
