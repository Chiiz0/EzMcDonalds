from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from ezmcd.errors import EzMcdError
from ezmcd.models import Policy, SHANGHAI, fingerprint
from ezmcd.preferences import MealPreferences, save_preferences, get_preferences, plan_meal
from ezmcd.service import Service
from ezmcd.storage import Store


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.record = {"alias": "test", "identity": fingerprint({"test": "preferences"}),
                       "provider": "mcd-cn", "policy": Policy().model_dump()}
        self.store.add_account(self.record)
        self.provider = Mock()
        self.service = Service(self.store, lambda _: self.provider)
        self.when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.provider.pickup_slots.return_value = {"dates": [{"date": self.when.date().isoformat(),
            "periods": [{"pickup_times": [self.when.isoformat()]}]}]}
        self.offer = {"name": "示例组合", "product_code": "100001", "product_type": 7,
                      "offer_id": "offer", "display_price_cny": "10.00", "card_name": "示例早餐卡"}
        self.provider.menu.return_value = {"products": [self.offer]}
        self.provider.coupons.return_value = {"coupons": []}
        choice = {"product_code": "200001", "name": "咖啡", "available": True,
                  "standard_configuration_supported": True, "extra_cny": "0.00"}
        self.detail = {"max_quantity": 10, "groups": [{"round": 1, "name": "饮品", "min_quantity": 1,
                                                     "max_quantity": 1, "choices": [choice]}]}
        self.provider.product_detail.return_value = self.detail
        self.preferences = MealPreferences(store_code="9000001", items=[{"name": "示例组合", "component_names": ["咖啡"]}],
                                           pickup_time="10:15", max_amount_cny="20", require_card_discount=True)
        save_preferences(self.store, self.preferences)

    def test_saving_is_not_spending_authorization_or_scheduling(self):
        before = deepcopy(self.store.account("test"))
        result = get_preferences(self.store)
        self.assertFalse(result["scheduling_enabled"])
        self.assertFalse(result["spending_authorized_by_preferences"])
        self.assertEqual(self.store.account("test"), before)
        self.assertEqual((self.store.root / "preferences.json").stat().st_mode & 0o777, 0o600)

    def test_plan_resolves_live_offer_component_time_and_stable_purchase_key_without_writes(self):
        result = plan_meal(self.service, "test")
        self.assertTrue(result["selection_ready"])
        self.assertEqual(result["request"]["pickup_at"], self.when.isoformat())
        self.assertEqual(result["estimated_amount_cny"], "10.00")
        self.assertEqual(result["items"][0]["combo"][0]["items"][0]["product_code"], "200001")
        self.assertEqual(plan_meal(self.service, "test")["idempotency_key"], result["idempotency_key"])
        self.assertFalse(result["order_created"])
        self.assertFalse(result["cart_changed"])
        self.assertFalse(result["estimated_price_is_final_quote"])
        for call in self.provider.method_calls:
            self.assertIn(call[0], ("pickup_slots", "menu", "product_detail"))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 0)

    def test_account_must_be_selected_and_missing_alias_is_not_guessed(self):
        with self.assertRaisesRegex(EzMcdError, "select_account"):
            plan_meal(self.service)
        with self.assertRaisesRegex(EzMcdError, "account_not_connected"):
            plan_meal(self.service, "absent")

    def test_unavailable_time_does_not_choose_another_time_or_load_menu(self):
        self.provider.pickup_slots.return_value["dates"][0]["periods"][0]["pickup_times"] = [self.when.replace(hour=9).isoformat()]
        result = plan_meal(self.service, "test")
        self.assertFalse(result["selection_ready"])
        self.assertEqual(result["issues"][0]["reason"], "preferred_pickup_time_unavailable")
        self.provider.menu.assert_not_called()

    def test_requested_date_and_weekdays_are_respected(self):
        result = plan_meal(self.service, "test", (self.when.date() + timedelta(days=1)).isoformat())
        self.assertFalse(result["selection_ready"])
        self.preferences.weekdays = [(self.when.weekday() + 1) % 7]
        save_preferences(self.store, self.preferences)
        self.assertFalse(plan_meal(self.service, "test")["selection_ready"])

    def test_missing_card_offer_does_not_silently_use_regular_price(self):
        self.offer["card_name"] = None
        result = plan_meal(self.service, "test")
        self.assertFalse(result["selection_ready"])
        self.assertEqual(result["items"], [])
        self.provider.product_detail.assert_not_called()

    def test_different_products_with_same_name_require_selection(self):
        self.provider.menu.return_value["products"].append(self.offer | {"product_code": "100002"})
        self.assertFalse(plan_meal(self.service, "test")["selection_ready"])
        self.provider.product_detail.assert_not_called()

    def test_group_landing_items_are_not_mistaken_for_orderable_combos(self):
        self.provider.menu.return_value["products"].append(self.offer | {"product_code": "G100001", "product_type": 3})
        self.assertTrue(plan_meal(self.service, "test")["selection_ready"])

    def test_unavailable_preferred_drink_is_not_replaced_by_available_alternative(self):
        self.detail["groups"][0]["choices"][0]["available"] = False
        self.detail["groups"][0]["choices"].append({"product_code": "200002", "name": "豆浆", "available": True,
            "standard_configuration_supported": True, "extra_cny": "0.00"})
        result = plan_meal(self.service, "test")
        self.assertFalse(result["selection_ready"])
        self.assertEqual(result["items"], [])

    def test_drink_missing_and_unsupported_customization_stop(self):
        for key, value in (("name", "另一杯咖啡"), ("standard_configuration_supported", False)):
            original = deepcopy(self.detail)
            self.detail["groups"][0]["choices"][0][key] = value
            self.assertFalse(plan_meal(self.service, "test")["selection_ready"])
            self.detail.clear(); self.detail.update(original)

    def test_extra_price_and_quantity_are_included_in_estimate(self):
        self.preferences.items[0].quantity = 2
        save_preferences(self.store, self.preferences)
        self.detail["groups"][0]["choices"][0]["extra_cny"] = "1.00"
        result = plan_meal(self.service, "test")
        self.assertFalse(result["selection_ready"])
        self.assertIn("estimated_price_exceeds_preference_limit", json.dumps(result))

    def test_opaque_offer_choice_uses_lowest_known_display_price_for_same_product(self):
        self.provider.menu.return_value["products"].append(self.offer | {"offer_id": "lower", "display_price_cny": "8.00"})
        result = plan_meal(self.service, "test")
        self.assertEqual(result["items"][0]["offer_id"], "lower")
        self.assertEqual(result["estimated_amount_cny"], "8.00")

    def test_preferences_do_not_accept_policy_or_schedule_fields(self):
        for field in ("allow_checkout", "scheduling_enabled", "daily_budget_cents"):
            with self.assertRaises(ValidationError):
                MealPreferences.model_validate(self.preferences.model_dump() | {field: True})

    def ingredient_preferences(self):
        choice = self.detail["groups"][0]["choices"][0]
        choice["modifiers"] = {"supported": True, "groups": [{"min_quantity": 0, "max_quantity": 2,
            "choices": [{"code": "300001", "name": "糖", "default_quantity": 1},
                        {"code": "300002", "name": "奶", "default_quantity": 1}]}]}
        value = self.preferences.model_dump()
        value["items"][0]["component_modifiers"] = [{"component_name": "咖啡", "modifiers": [{"name": "糖", "quantity": 0}]}]
        self.preferences = MealPreferences.model_validate(value)
        save_preferences(self.store, self.preferences)
        return choice["modifiers"]

    def test_ingredient_preferences_resolve_live_codes_and_keep_other_defaults_without_writes(self):
        public = self.ingredient_preferences()
        first = plan_meal(self.service, "test")
        self.assertTrue(first["selection_ready"])
        self.assertEqual(first["items"][0]["combo"][0]["items"][0]["modifiers"], [
            {"code": "300001", "quantity": 0}, {"code": "300002", "quantity": 1}])
        # A new live product revision maps the same named preference afresh.
        public["groups"][0]["choices"][0]["code"] = "400001"
        second = plan_meal(self.service, "test")
        self.assertTrue(second["selection_ready"])
        self.assertEqual(second["items"][0]["combo"][0]["items"][0]["modifiers"][0]["code"], "400001")
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertFalse(second["cart_changed"])
        self.assertEqual(second["products"][0]["component_modifiers"][0]["modifiers"], [{"name": "糖", "quantity": 0}])

    def test_missing_ambiguous_unsupported_and_invalid_ingredient_preferences_block_plan(self):
        self.ingredient_preferences()
        original = deepcopy(self.detail)
        for case in ("missing", "ambiguous", "unsupported", "minimum", "component"):
            self.detail.clear(); self.detail.update(deepcopy(original))
            public = self.detail["groups"][0]["choices"][0]["modifiers"]
            if case == "missing": public["groups"][0]["choices"][0]["name"] = "另一种糖"
            if case == "ambiguous": public["groups"][0]["choices"][1]["name"] = "糖"
            if case == "unsupported": public["supported"] = False
            if case == "minimum": public["groups"][0]["min_quantity"] = 2
            if case == "component": self.detail["groups"][0]["choices"][0]["name"] = "豆浆"
            with self.subTest(case=case):
                result = plan_meal(self.service, "test")
                self.assertFalse(result["selection_ready"])
                self.assertEqual(result["items"], [])

    def test_ingredient_defaults_are_not_falsely_applied_to_combo_root(self):
        self.ingredient_preferences()
        value = self.preferences.model_dump()
        value["items"][0]["modifiers"] = [{"name": "糖", "quantity": 0}]
        save_preferences(self.store, MealPreferences.model_validate(value))
        result = plan_meal(self.service, "test")
        self.assertFalse(result["selection_ready"])
        self.assertIn("use_component_modifiers", json.dumps(result))

    def test_single_item_ingredient_preference_and_no_accidental_component_selection(self):
        public = self.ingredient_preferences()
        self.offer["product_type"] = 1
        self.detail.update(groups=[], modifiers=public)
        value = self.preferences.model_dump()
        value["items"] = [{"name": "示例组合", "modifiers": [{"name": "糖", "quantity": 0}]}]
        save_preferences(self.store, MealPreferences.model_validate(value))
        result = plan_meal(self.service, "test")
        self.assertTrue(result["selection_ready"])
        self.assertEqual(result["items"][0]["modifiers"][0]["quantity"], 0)
        value["items"][0]["component_modifiers"] = [{"component_name": "咖啡", "modifiers": [{"name": "糖", "quantity": 0}]}]
        save_preferences(self.store, MealPreferences.model_validate(value))
        self.assertFalse(plan_meal(self.service, "test")["selection_ready"])

    def test_duplicate_ingredient_preference_is_rejected_before_saving(self):
        value = self.preferences.model_dump()
        value["items"][0]["modifiers"] = [{"name": "糖", "quantity": 0}, {"name": "糖", "quantity": 1}]
        with self.assertRaises(ValidationError): MealPreferences.model_validate(value)

    def test_optional_round_can_remain_explicitly_empty(self):
        optional = deepcopy(self.detail["groups"][0])
        optional.update(round=2, min_quantity=0)
        optional["choices"][0]["name"] = "加配"
        self.detail["groups"].append(optional)
        result = plan_meal(self.service, "test")
        self.assertTrue(result["selection_ready"])
        self.assertEqual(result["items"][0]["combo"][1], {"round": 2, "items": []})

    def coupon_preferences(self):
        self.preferences.require_card_discount = False
        self.preferences.items[0].coupon_name = "示例优惠券"
        save_preferences(self.store, self.preferences)
        self.provider.coupons.return_value = {"coupons": [{"name": "示例优惠券", "available_for_request": True,
            "coupon_ref": "coupon-" + "a" * 24, "configuration_path": "coupon_products"}]}
        self.provider.coupon_products.return_value = {"products": [self.offer | {
            "card_name": None, "display_price_cny": "9.90", "cart_configuration_supported": True}]}

    def test_coupon_preference_resolves_current_reference_and_preserves_stable_purchase_key(self):
        self.coupon_preferences()
        result = plan_meal(self.service, "test")
        self.assertTrue(result["selection_ready"])
        self.assertEqual(result["estimated_amount_cny"], "9.90")
        self.assertEqual(result["items"][0]["coupon_ref"], "coupon-" + "a" * 24)
        self.assertEqual(result["products"][0]["coupon_name"], "示例优惠券")
        self.provider.menu.assert_not_called()
        self.assertEqual(self.provider.product_detail.call_args.args[-1], "coupon-" + "a" * 24)
        self.provider.coupons.return_value["coupons"][0]["coupon_ref"] = "coupon-" + "b" * 24
        changed = plan_meal(self.service, "test")
        self.assertEqual(changed["idempotency_key"], result["idempotency_key"])
        self.assertEqual(changed["items"][0]["coupon_ref"], "coupon-" + "b" * 24)

    def test_expired_missing_ambiguous_or_special_coupon_does_not_fall_back_to_menu_price(self):
        self.coupon_preferences()
        original = deepcopy(self.provider.coupons.return_value)
        for case in ("expired", "missing", "ambiguous", "special"):
            value = deepcopy(original)
            if case == "expired": value["coupons"][0]["available_for_request"] = False
            if case == "missing": value["coupons"] = []
            if case == "ambiguous": value["coupons"] *= 2
            if case == "special": value["coupons"][0]["configuration_path"] = "official_app"
            self.provider.coupons.return_value = value
            result = plan_meal(self.service, "test")
            self.assertFalse(result["selection_ready"])
            self.assertEqual(result["items"], [])
        self.provider.menu.assert_not_called()
        self.provider.coupon_products.assert_not_called()
        self.provider.product_detail.assert_not_called()

    def test_card_requirement_and_explicit_coupon_cannot_be_silently_combined(self):
        self.coupon_preferences()
        self.preferences.require_card_discount = True
        save_preferences(self.store, self.preferences)
        result = plan_meal(self.service, "test")
        self.assertFalse(result["selection_ready"])
        self.assertEqual(result["issues"][0]["reason"], "explicit_coupon_conflicts_with_required_card_discount")
        self.provider.coupons.assert_not_called()

    def test_preferred_product_outside_selected_coupon_is_not_eligible(self):
        self.coupon_preferences()
        self.provider.coupon_products.return_value["products"][0]["name"] = "另一种餐品"
        result = plan_meal(self.service, "test")
        self.assertFalse(result["selection_ready"])
        self.provider.product_detail.assert_not_called()

    def test_single_choice_preference_resolves_named_option_to_current_key(self):
        from test_project_modifiers import iced_coffee
        from ezmcd.modifiers import public_modifiers
        public = public_modifiers(iced_coffee())
        self.detail["groups"][0]["choices"][0]["modifiers"] = public
        value = self.preferences.model_dump()
        value["items"][0]["component_modifiers"] = [{"component_name": "咖啡", "modifiers": [
            {"name": "少冰", "quantity": 1, "group_name": "冰(单选)"}]}]
        save_preferences(self.store, MealPreferences.model_validate(value))
        result = plan_meal(self.service, "test")
        self.assertTrue(result["selection_ready"])
        self.assertEqual(result["items"][0]["combo"][0]["items"][0]["modifiers"], [{"code": "222", "quantity": 1, "key": "1-1"}])
        # A live revision changes the key, and the next plan resolves it afresh.
        public["groups"][0]["choices"][3]["key"] = "2-1"
        self.assertEqual(plan_meal(self.service, "test")["items"][0]["combo"][0]["items"][0]["modifiers"][0]["key"], "2-1")

    def test_conflicting_paid_inactive_or_ambiguous_single_choices_do_not_silently_default(self):
        from test_project_modifiers import iced_coffee
        from ezmcd.modifiers import public_modifiers
        original = public_modifiers(iced_coffee())
        for case in ("conflict", "paid", "inactive", "ambiguous", "quantity_zero"):
            public = deepcopy(original)
            preferences = [{"name": "少冰", "quantity": 1}]
            if case == "conflict": preferences.append({"name": "去冰", "quantity": 1})
            if case == "paid": public["groups"][0]["choices"][3]["extra_cny"] = "5.00"
            if case == "inactive": preferences = [{"name": "不加", "quantity": 1}]
            if case == "ambiguous": public["groups"][0]["choices"][2]["name"] = "少冰"
            if case == "quantity_zero": preferences[0]["quantity"] = 0
            self.detail["groups"][0]["choices"][0]["modifiers"] = public
            value = self.preferences.model_dump()
            value["items"][0]["component_modifiers"] = [{"component_name": "咖啡", "modifiers": preferences}]
            save_preferences(self.store, MealPreferences.model_validate(value))
            with self.subTest(case=case):
                result = plan_meal(self.service, "test")
                self.assertFalse(result["selection_ready"])
                self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
