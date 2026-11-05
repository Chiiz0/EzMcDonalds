from copy import deepcopy
import json
import unittest

from ezmcd.errors import EzMcdError
from ezmcd.preferences import MealPreferences, plan_meal, save_preferences
import test_project_preferences as preference_tests


class CardDiagnosticTests(unittest.TestCase):
    setUp = preference_tests.PreferenceTests.setUp

    def unavailable_card(self):
        self.offer["card_name"] = None
        card = {"name": "示例早餐卡", "configuration_path": "menu_card_offer",
            "validity_text": "仍在有效期内", "remaining_today": 0, "remaining_total": 10,
            "available_for_request": False, "availability_issues": ["coupon_remaining_uses_unavailable"],
            "rules": ["今日已用完"], "cardId": "private-identifier"}
        self.provider.coupons.return_value = {"coupons": [card]}
        return card

    def test_menu_product_is_distinguished_from_missing_discount_without_fallback(self):
        self.unavailable_card()
        before = deepcopy(self.store.account("test"))
        preferences_before = (self.store.root / "preferences.json").read_bytes()
        result = plan_meal(self.service, "test")
        issue = result["issues"][0]
        self.assertEqual(issue["reason"], "required_card_offer_unavailable")
        self.assertEqual(issue["menu_offers"][0]["display_price_cny"], "10.00")
        self.assertFalse(issue["menu_prices_are_final_quotes"])
        self.assertFalse(issue["regular_price_fallback_allowed"])
        self.assertFalse(result["selection_ready"])
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["estimated_amount_cny"])
        self.assertFalse(result["cart_changed"])
        self.assertFalse(result["order_created"])
        self.assertFalse(result["payment_attempted"])
        self.provider.product_detail.assert_not_called()
        self.assertEqual(self.store.account("test"), before)
        self.assertEqual((self.store.root / "preferences.json").read_bytes(), preferences_before)
        self.assertEqual({c[0] for c in self.provider.method_calls}, {"pickup_slots", "menu", "coupons"})
        with self.store.connect() as db:
            for table in ("quotes", "attempts", "writes", "cart_additions", "pickup_quotes"):
                self.assertEqual(db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)

    def test_diagnostic_preserves_reported_daily_limit_without_predicting_tomorrow(self):
        self.unavailable_card()
        result = plan_meal(self.service, "test")
        diagnostic = result["card_diagnostics"]
        self.assertTrue(diagnostic["checked"])
        self.assertEqual(diagnostic["pickup_at"], self.when.isoformat())
        self.assertEqual(diagnostic["store_code"], "9000001")
        self.assertEqual(diagnostic["cards"][0]["rules"], ["今日已用完"])
        self.assertEqual(diagnostic["cards"][0]["remaining_today"], 0)
        self.assertFalse(diagnostic["product_card_link_verified"])
        self.assertFalse(diagnostic["future_eligibility_guaranteed"])
        self.assertFalse(diagnostic["automatic_retry"])
        self.assertNotIn("private-identifier", json.dumps(result))

    def test_unrelated_available_card_is_not_selected_or_claimed_as_eligible(self):
        card = self.unavailable_card()
        card.update(name="另一张卡", available_for_request=True, remaining_today=1,
                    availability_issues=[], rules=[])
        result = plan_meal(self.service, "test")
        self.assertEqual(result["items"], [])
        self.assertTrue(result["card_diagnostics"]["cards"][0]["available_for_request"])
        self.assertFalse(result["card_diagnostics"]["product_card_link_verified"])

    def test_no_card_results_do_not_claim_expiry_or_missing_membership(self):
        self.unavailable_card()
        self.provider.coupons.return_value = {"coupons": []}
        result = plan_meal(self.service, "test")
        self.assertTrue(result["card_diagnostics"]["checked"])
        self.assertEqual(result["card_diagnostics"]["cards"], [])
        self.assertEqual(result["issues"][0]["reason"], "required_card_offer_unavailable")

    def test_ordinary_coupons_are_not_reported_as_card_evidence(self):
        card = self.unavailable_card()
        self.provider.coupons.return_value["coupons"].append(card | {
            "name": "普通券", "configuration_path": "coupon_products"})
        result = plan_meal(self.service, "test")
        self.assertEqual([c["name"] for c in result["card_diagnostics"]["cards"]], ["示例早餐卡"])

    def test_failed_diagnostic_keeps_menu_evidence_and_stops_without_retry(self):
        self.unavailable_card()
        self.provider.coupons.side_effect = EzMcdError("session_expired_reconnect_locally")
        result = plan_meal(self.service, "test")
        self.assertFalse(result["selection_ready"])
        self.assertEqual(result["issues"][0]["reason"], "required_card_offer_unavailable")
        self.assertFalse(result["card_diagnostics"]["checked"])
        self.assertEqual(result["card_diagnostics"]["error"], "session_expired_reconnect_locally")
        self.provider.coupons.assert_called_once()

    def test_multiple_missing_offers_share_one_scoped_card_query(self):
        self.unavailable_card()
        self.preferences.items.append(self.preferences.items[0].model_copy(update={"name": "另一份早餐"}))
        save_preferences(self.store, self.preferences)
        self.provider.menu.return_value["products"].append(self.offer | {"name": "另一份早餐", "product_code": "100002"})
        result = plan_meal(self.service, "test")
        self.assertEqual(len(result["issues"]), 2)
        self.provider.coupons.assert_called_once()
        self.assertEqual(self.provider.coupons.call_args.args[0].pickup_at, result["request"]["pickup_at"])

    def test_parent_variant_missing_card_uses_same_diagnostic_without_leaf_guess(self):
        self.unavailable_card()
        self.offer.update(name="示例咖啡父页", product_type=3)
        value = self.preferences.model_dump()
        value["items"] = [{"name": "示例大杯", "variant_of": "示例咖啡父页"}]
        save_preferences(self.store, MealPreferences.model_validate(value))
        result = plan_meal(self.service, "test")
        self.assertEqual(result["issues"][0]["name"], "示例咖啡父页")
        self.assertEqual(result["issues"][0]["reason"], "required_card_offer_unavailable")
        self.assertEqual(result["items"], [])
        self.provider.product_detail.assert_not_called()

    def test_ready_card_plan_does_not_add_diagnostic_queries(self):
        result = plan_meal(self.service, "test")
        self.assertTrue(result["selection_ready"])
        self.assertNotIn("card_diagnostics", result)
        self.provider.coupons.assert_not_called()

    def test_missing_product_or_ambiguous_card_product_is_not_misclassified(self):
        for offers in ([], [self.offer, self.offer | {"product_code": "100002"}],
                [self.offer | {"card_name": None}, self.offer | {"product_code": "100002", "card_name": None}]):
            self.provider.menu.return_value = {"products": offers}
            result = plan_meal(self.service, "test")
            self.assertEqual(result["issues"][0]["reason"], "preferred_product_unavailable_or_ambiguous")
            self.assertNotIn("card_diagnostics", result)
        self.provider.coupons.assert_not_called()

    def test_ambiguous_variant_parent_keeps_choice_error_before_card_query(self):
        self.unavailable_card()
        self.offer.update(name="示例咖啡父页", product_type=3)
        self.provider.menu.return_value["products"].append(self.offer | {"offer_id": "another-parent"})
        value = self.preferences.model_dump()
        value["items"] = [{"name": "示例大杯", "variant_of": "示例咖啡父页"}]
        save_preferences(self.store, MealPreferences.model_validate(value))
        result = plan_meal(self.service, "test")
        self.assertEqual(result["issues"][0]["reason"], "preferred_variant_parent_unavailable_or_ambiguous")
        self.assertNotIn("card_diagnostics", result)
        self.provider.coupons.assert_not_called()

    def test_noncard_preferences_continue_to_resolve_ordinary_offer(self):
        self.unavailable_card()
        self.preferences.require_card_discount = False
        save_preferences(self.store, self.preferences)
        result = plan_meal(self.service, "test")
        self.assertTrue(result["selection_ready"])
        self.assertNotIn("card_diagnostics", result)
        self.provider.coupons.assert_not_called()

    def test_later_live_card_offer_may_resolve_without_changing_preferences(self):
        self.unavailable_card()
        first = plan_meal(self.service, "test")
        self.offer["card_name"] = "示例早餐卡"
        second = plan_meal(self.service, "test")
        self.assertFalse(first["selection_ready"])
        self.assertTrue(second["selection_ready"])
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertNotIn("card_diagnostics", second)
        self.provider.coupons.assert_called_once()


if __name__ == "__main__":
    unittest.main()
