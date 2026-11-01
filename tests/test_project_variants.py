from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from ezmcd.catalog import menu_products
from ezmcd.demo import DemoProvider, setup_demo
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, ItemSelection, SHANGHAI, fingerprint
from ezmcd.preferences import MealPreferences, save_preferences, plan_meal
from ezmcd.provider import McdProvider
from ezmcd.service import Service
from ezmcd.storage import Store
from ezmcd.variants import resolve_variant


def detail():
    return {"maxPurchaseQuantity": 20, "product": {"code": "G900", "name": "演示规格", "type": 3,
        "limitedQuantity": 0, "specTypes": [{"code": "17", "name": "规格", "specs": [
            {"code": "1", "name": "标准"}, {"code": "2", "name": "大份"}]}], "products": [
            {"code": code, "name": name, "type": 1, "price": price, "unsellable": False, "matchedSpecsCode": spec}
            for code, name, price, spec in (("100001", "演示汉堡", 1000, "17-1"), ("100002", "演示大汉堡", 1500, "17-2"))]}}


class VariantDemo(DemoProvider):
    """Real selection code with offline-only cart/order/payment transport."""
    product_detail = McdProvider.product_detail
    configured_cart_draft = McdProvider.configured_cart_draft
    menu = McdProvider.menu

    def __init__(self, account, store):
        super().__init__(account, store)
        self.live = detail()
        self.menu_raw = {"menu": [{"categoryName": "演示", "productList": [
            {"productCode": "G900", "productType": 3, "productName": "演示规格"}]}]}
        self.detail_calls = []

    def context(self, args):
        return "1", {"storeCode": args.store_code, "date": args.pickup_at[:10], "time": "10:15", "beType": "1", "orderType": "1"}

    def get(self, path, params):
        if path == "/bff/spc/menu":
            return deepcopy(self.menu_raw)
        if path == "/bff/cart/carts":
            return {"storeCode": params["storeCode"], "beType": 1, "orderType": 1,
                    "products": ["occupied"] if self.read()["cart_quantity"] else []}
        raise AssertionError("Unexpected offline path: " + path)

    def _detail(self, offer, daypart, params):
        self.detail_calls.append(offer["productCode"])
        return deepcopy(self.live)


class VariantTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        setup_demo(self.store)
        self.provider = VariantDemo(self.store.account("demo"), self.store)
        self.provider.write_cart({"quantity": 0})
        self.when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account="demo", store_code="9000001", pickup_at=self.when.isoformat(), max_amount_cny="20")
        self.offer = menu_products(self.provider.menu_raw)[0]
        self.selection = ItemSelection(product_code="100001", variant_of="G900", offer_id=self.offer["offer_id"])

    def test_detail_exposes_parent_route_only_after_validating_live_specs(self):
        view = self.provider.product_detail(self.args, "G900", self.offer["offer_id"])
        variant = view["variants"][0]
        self.assertEqual(variant["menu_offers"], [])
        self.assertEqual(variant["parent_detail_selection"], {"product_code": "100001", "variant_of": "G900", "offer_id": self.offer["offer_id"]})
        self.assertEqual(variant["next_step"], "product_detail_with_parent_detail_selection")
        selected = self.provider.product_detail(self.args, **variant["parent_detail_selection"])
        self.assertEqual(selected["display_price_cny"], "10.00")
        self.assertFalse(selected["display_price_is_final_quote"])

    def test_plain_child_cannot_be_invented_from_a_code_absent_from_menu(self):
        with self.assertRaisesRegex(EzMcdError, "select_exact_live_menu_offer"):
            self.provider.product_detail(self.args, "100001")
        with self.assertRaisesRegex(EzMcdError, "use_cart_add_configured"):
            McdProvider.simple_cart_draft(self.provider, self.args, [self.selection])

    def test_add_reloads_parent_and_serializes_only_selected_child(self):
        self.provider.product_detail(self.args, "G900", self.offer["offer_id"])
        draft = self.provider.configured_cart_draft(self.args, [self.selection])
        self.assertEqual(self.provider.detail_calls, ["G900", "G900"])
        self.assertEqual(draft["products"][0]["code"], "100001")
        self.assertFalse(draft["products"][0]["useRightCard"])
        self.assertNotIn("variant_of", draft["products"][0])
        self.provider.live["product"]["products"][0]["unsellable"] = True
        with self.assertRaisesRegex(EzMcdError, "requires_official_configuration"):
            self.provider.configured_cart_draft(self.args, [self.selection])

    def test_parent_discount_sources_are_never_transferred_to_child(self):
        for key in ("cardId", "cardName", "couponId", "couponCode", "promotionId", "productPromotions", "_coupon_source"):
            with self.subTest(key=key), self.assertRaisesRegex(EzMcdError, "discount_requires_own_menu_offer"):
                resolve_variant(detail(), self.offer | {key: "synthetic"}, "100001")
            if key != "_coupon_source":
                raw = detail(); raw["product"]["products"][0][key] = "synthetic"
                with self.assertRaisesRegex(EzMcdError, "discount_requires_own_menu_offer"):
                    resolve_variant(raw, self.offer, "100001")
        self.provider.menu_raw["menu"][0]["productList"][0]["cardName"] = "演示卡"
        view = self.provider.product_detail(self.args, "G900", self.offer["offer_id"])
        self.assertIsNone(view["variants"][0]["parent_detail_selection"])

    def test_related_promotions_are_disclosed_without_adding_products_or_ids(self):
        self.provider.live["product"]["products"][0].update(promotionInfos=[{"promotionId": "synthetic-promo"}],
            associationInfo={"products": [{"code": "100002"}]})
        selected = self.provider.product_detail(self.args, "100001", self.offer["offer_id"], variant_of="G900")
        self.assertTrue(selected["optional_promotions_present"])
        self.assertFalse(selected["related_products_selected"])
        draft = self.provider.configured_cart_draft(self.args, [self.selection])
        self.assertEqual(len(draft["products"]), 1)
        self.assertNotIn("promotionId", draft["products"][0])

    def test_malformed_or_ambiguous_specifications_fail_closed(self):
        for change in ("duplicate_code", "duplicate_combo", "duplicate_dimension", "unknown_value", "missing_dimension", "null_parent"):
            value = detail(); parent = value["product"]
            if change == "duplicate_code": parent["products"][1]["code"] = "100001"
            if change == "duplicate_combo": parent["products"][1]["matchedSpecsCode"] = "17-1"
            if change == "duplicate_dimension": parent["specTypes"].append(deepcopy(parent["specTypes"][0]))
            if change == "unknown_value": parent["products"][0]["matchedSpecsCode"] = "17-9"
            if change == "missing_dimension": parent["products"][0]["matchedSpecsCode"] = ""
            if change == "null_parent": value["product"] = None
            with self.subTest(change=change), self.assertRaisesRegex(EzMcdError, "specification_schema_changed"):
                resolve_variant(value, self.offer, "100001")

    def test_sold_out_unpriced_complex_or_missing_child_is_not_guessed(self):
        for field, value in (("unsellable", True), ("unsellable", None), ("price", 0), ("price", True),
                             ("price", "1000"), ("type", 8), ("products", [{}]), ("name", None)):
            raw = detail(); raw["product"]["products"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(EzMcdError, "requires_official_configuration"):
                resolve_variant(raw, self.offer, "100001")
        with self.assertRaisesRegex(EzMcdError, "not_in_current_parent"):
            resolve_variant(detail(), self.offer, "100003")

    def test_parent_and_child_purchase_limits_both_apply(self):
        self.provider.live["product"]["limitedQuantity"] = 1
        with self.assertRaisesRegex(EzMcdError, "quantity_exceeds_live_limit"):
            self.provider.configured_cart_draft(self.args, [self.selection.model_copy(update={"quantity": 2})])
        self.provider.live["product"]["limitedQuantity"] = 0
        self.provider.live["product"]["products"][0]["limitedQuantity"] = 1
        with self.assertRaisesRegex(EzMcdError, "quantity_exceeds_live_limit"):
            self.provider.configured_cart_draft(self.args, [self.selection.model_copy(update={"quantity": 2})])

    def test_explicit_parent_offer_is_required_and_cannot_mix_coupon(self):
        for changes in ({"offer_id": None}, {"coupon_ref": "coupon-" + "a" * 24}, {"variant_of": "100001"}):
            with self.assertRaises(ValidationError):
                ItemSelection.model_validate(self.selection.model_dump() | changes)
        with self.assertRaisesRegex(EzMcdError, "explicit_regular_parent"):
            self.provider.product_detail(self.args, "100001", variant_of="G900")

    def test_existing_cart_intent_serialization_is_unchanged(self):
        before = {"product_code": "100001", "quantity": 1, "offer_id": None, "coupon_ref": None, "combo": [], "modifiers": None}
        now = ItemSelection(product_code="100001").model_dump()
        self.assertEqual(fingerprint(now), fingerprint(before))
        self.assertNotIn("variant_of", now)

    def test_demo_refuses_variant_in_both_add_routes(self):
        provider = DemoProvider(self.store.account("demo"), self.store)
        for method in (provider.simple_cart_draft, provider.configured_cart_draft):
            with self.assertRaises(EzMcdError): method(self.args, [self.selection])

    def test_simulated_complete_flow_restart_and_replays_never_rebuild_or_repay(self):
        service = Service(self.store)
        with patch.object(Service, "provider", side_effect=lambda alias: (self.store.account(alias), self.provider)):
            first = service.add_configured_items(self.args, [self.selection], "variant-meal")
            self.assertEqual(first["state"], "MATCHED")
            quote = service.quote(self.args)
            paid = service.submit("demo", quote["quote_id"], "variant-meal")
            self.assertTrue(paid["paid_verified"])
            restarted = Service(Store(self.root))
            with patch.object(self.provider, "configured_cart_draft", side_effect=AssertionError("Do not rebuild")):
                repeated = restarted.add_configured_items(self.args, [self.selection], "variant-meal")
                self.assertFalse(repeated["add_attempted"])
            replay = restarted.submit("demo", quote["quote_id"], "variant-meal")
            self.assertEqual(replay["order_id"], paid["order_id"])
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute("SELECT stage,count(*) FROM writes GROUP BY stage").fetchall()),
                             {"cart": 1, "order": 1, "preorder": 1, "consume": 1})
        self.assertEqual(self.provider.wallet()["balance_cents"], 9000)

    def plan(self, **changes):
        prefs = {"store_code": "9000001", "pickup_time": "10:15", "max_amount_cny": "20",
                 "require_card_discount": False, "items": [{"name": "演示汉堡", "variant_of": "演示规格"}]}
        save_preferences(self.store, MealPreferences.model_validate(prefs | changes))
        with patch.object(Service, "provider", return_value=(self.store.account("demo"), self.provider)):
            return plan_meal(Service(self.store), "demo", self.when.date().isoformat())

    def test_preferences_resolve_names_to_fresh_parent_and_child_without_writes(self):
        result = self.plan()
        self.assertTrue(result["selection_ready"])
        self.assertEqual(result["items"][0]["variant_of"], "G900")
        self.assertEqual(result["estimated_amount_cny"], "10.00")
        self.assertFalse(result["cart_changed"])
        with self.store.connect() as db: self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 0)

    def test_variant_preferences_stop_for_budget_discount_or_coupon_conflicts(self):
        for change in ({"max_amount_cny": "5"}, {"require_card_discount": True},
                       {"items": [{"name": "演示汉堡", "variant_of": "演示规格", "coupon_name": "演示券"}]}):
            result = self.plan(**change)
            self.assertFalse(result["selection_ready"])
            self.assertEqual(result["items"], [])

    def test_variant_preferences_stop_for_unavailable_or_ambiguous_names(self):
        original = deepcopy(self.provider.menu_raw)
        for case in ("missing_parent", "ambiguous_parent", "ambiguous_child", "sold_out_child"):
            self.provider.menu_raw = deepcopy(original); self.provider.live = detail()
            rows = self.provider.menu_raw["menu"][0]["productList"]
            if case == "missing_parent": rows.clear()
            if case == "ambiguous_parent": rows.append(rows[0] | {"productCode": "G901"})
            if case == "ambiguous_child": self.provider.live["product"]["products"][1]["name"] = "演示汉堡"
            if case == "sold_out_child": self.provider.live["product"]["products"][0]["unsellable"] = True
            with self.subTest(case=case):
                self.assertFalse(self.plan()["selection_ready"])


if __name__ == "__main__":
    unittest.main()
