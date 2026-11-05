"""One coupon bundle can contain multiple portions of the same component."""
from copy import deepcopy
import unittest

from ezmcd.catalog import cart_items, verify_confirmation_components
from ezmcd.errors import EzMcdError
from ezmcd.models import ItemSelection
from ezmcd.preferences import MealPreferences, plan_meal, save_preferences
from ezmcd.products import verify_cart_contents, verify_configured_cart
import test_project_coupons as coupons
import test_project_preferences as preferences


class CouponComboTests(unittest.TestCase):
    def setUp(self):
        coupons.CouponProviderTests.setUp(self)
        self.products["productList"][0].update(productType=7, productName="示例双份套餐",
            priceInfo={"discountPrice": 1350, "eatInPrice": 2700})
        self.detail["product"].update(type=7, name="示例双份套餐", comboItems=[{
            "round": 1, "isChoices": 1, "choicesCode": "900012", "name": "小食",
            "minQuantity": 2, "maxQuantity": 2, "comboProducts": [{
                "code": "900013", "name": "示例小食", "quantity": 2, "comboMaxQuantity": -1,
                "unsellable": False, "diffPrice": 0}]}])
        self.selection = ItemSelection(product_code="900011", offer_id=self.offer, coupon_ref=self.ref,
            combo=[{"round": 1, "items": [{"product_code": "900013", "quantity": 2}]}])

    def draft(self, selection=None):
        return self.provider.configured_cart_draft(self.args, [selection or self.selection])

    def coupon_cart(self, draft):
        product = deepcopy(draft["products"][0])
        product.update(name="示例双份套餐", realSubTotalPrice=1350,
            coupon=deepcopy(self.raw["couponTabList"][0]["coupons"][0]["coupons"][0]))
        return {"items": cart_items({"products": [product]})}

    def test_coupon_count_and_component_portions_remain_distinct(self):
        view = self.provider.product_detail(self.args, "900011", self.offer, self.ref)
        self.assertTrue(view["standard_configuration_supported"])
        self.assertEqual(view["max_quantity"], 1)
        self.assertEqual(view["groups"][0]["min_quantity"], 2)
        self.assertEqual(view["groups"][0]["max_quantity"], 2)
        draft = self.draft(); product = draft["products"][0]
        self.assertEqual(product["quantity"], 1)
        self.assertEqual(product["comboItems"][0]["comboProducts"][0]["quantity"], 2)
        self.assertEqual(product["couponId"], "private-coupon-id")
        verify_configured_cart(draft, self.coupon_cart(draft))
        verify_cart_contents(draft, self.coupon_cart(draft))
        self.assertTrue(all(c.args[0] == "POST" and c.args[1].startswith("/bff/spc/products/detail/")
                            for c in self.http.send.call_args_list))

    def test_two_components_do_not_authorize_two_coupon_redemptions_or_partial_bundle(self):
        with self.assertRaisesRegex(EzMcdError, "coupon_configuration_or_quantity"):
            self.draft(self.selection.model_copy(update={"quantity": 2}))
        for quantity in (1, 3):
            selection = self.selection.model_dump()
            selection["combo"][0]["items"][0]["quantity"] = quantity
            with self.assertRaisesRegex(EzMcdError, "combo_round_quantity_outside_live_limits"):
                self.draft(ItemSelection.model_validate(selection))

    def test_missing_free_portion_or_wrong_parent_quantity_cannot_match_paid_bundle(self):
        draft = self.draft()
        for field in ("component_quantity", "component_code", "parent_quantity"):
            cart = self.coupon_cart(draft); row = cart["items"][0]
            if field == "parent_quantity": row["quantity"] = 2
            elif field == "component_quantity": row["components"][0]["comboProducts"][0]["quantity"] = 1
            else: row["components"][0]["comboProducts"][0]["code"] = "900099"
            with self.subTest(field=field), self.assertRaisesRegex(EzMcdError, "cart_contents_do_not_match"):
                verify_cart_contents(draft, cart)

    def test_correct_components_do_not_hide_a_missing_or_changed_coupon(self):
        draft = self.draft()
        for value in (None, "different-coupon-fingerprint"):
            cart = self.coupon_cart(draft); cart["items"][0]["coupon_fingerprint"] = value
            with self.assertRaisesRegex(EzMcdError, "cart_coupon_not_confirmed"):
                verify_configured_cart(draft, cart)

    def test_confirmation_must_include_both_portions_even_if_coupon_total_is_unchanged(self):
        product = self.draft()["products"][0]
        verify_confirmation_components(product, [{"productCode": "900013", "quantity": 2}])
        for rows in ([], [{"productCode": "900013", "quantity": 1}],
                     [{"productCode": "900013", "quantity": 2}, {"productCode": "900099", "quantity": 1}]):
            with self.assertRaisesRegex(EzMcdError, "confirmation_components_mismatch"):
                verify_confirmation_components(product, rows)


class CouponComboPreferenceTests(unittest.TestCase):
    def setUp(self):
        preferences.PreferenceTests.setUp(self)
        self.offer.update(name="示例双份套餐", card_name=None, display_price_cny="13.50",
                          cart_configuration_supported=True)
        self.provider.coupons.return_value = {"coupons": [{"name": "示例双份券",
            "coupon_ref": "coupon-" + "1" * 24, "available_for_request": True,
            "configuration_path": "coupon_products"}]}
        self.provider.coupon_products.return_value = {"products": [self.offer]}
        self.detail.update(max_quantity=1)
        self.detail["groups"][0].update(min_quantity=2, max_quantity=2)
        self.detail["groups"][0]["choices"][0].update(name="示例小食", max_quantity=-1)
        self.preferences = MealPreferences(store_code="9000001", pickup_time="10:15", max_amount_cny="20",
            items=[{"name": "示例双份套餐", "coupon_name": "示例双份券", "quantity": 1,
                    "component_choices": [{"round": 1, "name": "示例小食", "quantity": 2}]}])
        save_preferences(self.store, self.preferences)

    def test_plan_prices_one_coupon_bundle_and_keeps_both_portions(self):
        plan = plan_meal(self.service, "test")
        self.assertTrue(plan["selection_ready"])
        self.assertEqual(plan["estimated_amount_cny"], "13.50")
        self.assertEqual(plan["items"][0]["quantity"], 1)
        self.assertEqual(plan["items"][0]["combo"][0]["items"][0]["quantity"], 2)
        self.assertEqual(plan["items"][0]["coupon_ref"], "coupon-" + "1" * 24)
        self.preferences.max_amount_cny = "13.49"; save_preferences(self.store, self.preferences)
        limited = plan_meal(self.service, "test")
        self.assertFalse(limited["selection_ready"])
        self.assertIn("estimated_price_exceeds_preference_limit", [i["reason"] for i in limited["issues"]])

    def test_two_coupon_bundles_cannot_be_inferred_from_two_component_portions(self):
        self.preferences.items[0].quantity = 2; save_preferences(self.store, self.preferences)
        plan = plan_meal(self.service, "test")
        self.assertFalse(plan["selection_ready"])
        self.assertEqual(plan["items"], [])
        self.assertFalse(plan["cart_changed"])


if __name__ == "__main__":
    unittest.main()
