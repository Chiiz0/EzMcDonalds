from copy import deepcopy
import json
import unittest

from ezmcd.catalog import cart_items
from ezmcd.errors import EzMcdError
from ezmcd.models import ItemSelection, ModifierSelection
from ezmcd.modifiers import public_modifiers, selected_modification, verify_modification
from ezmcd.products import cart_configuration, configured_product, public_detail, verify_configured_cart


def coffee():
    return {"code": "200001", "name": "示例咖啡", "type": 1, "unsellable": False, "hasCustomization": 1,
        "modification": {"items": [{"minValues": 0, "maxValues": 2, "values": [
            {"code": code, "name": name, "displayMode": 2, "minQuantity": 0, "maxQuantity": 1,
             "selectedQuantity": 1, "selectedKey": "0-1", "unselectedKey": "0-0", "price": 0, "soldOut": False}
            for code, name in (("111", "奶油球"), ("112", "白砂糖"))]}]}}


class ModifierTests(unittest.TestCase):
    def test_default_two_free_ingredients_use_official_modification_values(self):
        self.assertEqual(cart_configuration(coffee()), {"modification": {"values": [
            {"code": "111", "key": "0-1", "quantity": 1}, {"code": "112", "key": "0-1", "quantity": 1}]}})
        view = public_modifiers(coffee())
        self.assertTrue(view["supported"])
        self.assertEqual(view["groups"][0]["choices"][1]["name"], "白砂糖")

    def test_explicit_remove_sugar_keeps_requested_cream(self):
        choices = [ModifierSelection(code="111", quantity=1), ModifierSelection(code="112", quantity=0)]
        detail = {"maxPurchaseQuantity": 5, "product": coffee()}
        offer = {"productCode": "200001", "productType": 1, "offer_id": "synthetic"}
        selection = ItemSelection(product_code="200001", modifiers=choices)
        result = configured_product(selection, offer, detail, 1)
        self.assertNotIn("customization", result)
        self.assertEqual(result["modification"]["values"][1], {"code": "112", "quantity": 1, "key": "0-0"})
        self.assertEqual(detail["product"]["modification"]["items"][0]["values"][1]["selectedQuantity"], 1)

    def test_partial_unknown_and_duplicate_choices_are_rejected(self):
        for choices in ([], [("111", 1)], [("111", 1), ("113", 0)], [("111", 0), ("111", 1), ("112", 0)]):
            with self.subTest(choices=choices), self.assertRaisesRegex(EzMcdError, "exactly_once"):
                selected_modification(coffee(), [ModifierSelection(code=c, quantity=q) for c, q in choices])

    def test_live_minimum_is_enforced(self):
        product = coffee(); product["modification"]["items"][0]["minValues"] = 1
        with self.assertRaisesRegex(EzMcdError, "outside_live_limits"):
            selected_modification(product, [ModifierSelection(code=c, quantity=0) for c in ("111", "112")])

    def test_paid_sold_out_nonbinary_or_unknown_shapes_are_not_guessed(self):
        for key, value in (("price", 100), ("soldOut", True), ("displayMode", 3), ("maxQuantity", 2),
                           ("selectedQuantity", True), ("selectedKey", "1-2"), ("unselectedKey", "")):
            p = coffee(); p["modification"]["items"][0]["values"][0][key] = value
            self.assertFalse(public_modifiers(p)["supported"])
            with self.assertRaises(EzMcdError):
                cart_configuration(p)

    def test_cart_echo_must_confirm_both_included_and_removed_choices(self):
        expected = selected_modification(coffee(), [ModifierSelection(code=c, quantity=0) for c in ("111", "112")])
        verify_modification(expected, deepcopy(expected))
        verify_modification(expected, {"customization": {"options": expected["modification"]["values"]}})
        for actual in ({}, {"modification": None}, selected_modification(coffee()),
                       {"modification": {"values": expected["modification"]["values"][:1]}}):
            with self.assertRaisesRegex(EzMcdError, "not_confirmed"):
                verify_modification(expected, actual)

    def test_modifier_checks_bind_to_product_round_and_quantity(self):
        selected = selected_modification(coffee())
        draft = {"products": [{"code": "900001", "quantity": 1, "comboItems": [
            {"round": 2, "comboProducts": [{"code": "200001", "quantity": 1} | selected]}]}]}
        cart = {"items": [{"product_code": "900001", "quantity": 1, "components": [
            {"round": 2, "comboProducts": [{"code": "200001", "quantity": 1} | selected]}]}]}
        verify_configured_cart(draft, cart)
        for field, value in (("round", 3),):
            wrong = deepcopy(cart); wrong["items"][0]["components"][0][field] = value
            with self.assertRaises(EzMcdError): verify_configured_cart(draft, wrong)
        wrong = deepcopy(cart); wrong["items"][0]["components"][0]["comboProducts"][0]["quantity"] = 2
        with self.assertRaises(EzMcdError): verify_configured_cart(draft, wrong)

    def test_single_product_configuration_is_visible_to_quote_identity(self):
        product = {"code": "200001", "name": "咖啡", "type": 1, "quantity": 1, "realSubTotalPrice": 1000}
        before = cart_items({"products": [product | selected_modification(coffee())]})
        after = cart_items({"products": [product | selected_modification(coffee(), [
            ModifierSelection(code="111", quantity=0), ModifierSelection(code="112", quantity=0)])]})
        self.assertNotEqual(before, after)
        self.assertIn('"0-0"', json.dumps(after))

    def test_group_detail_displays_variants_without_reusing_private_discount_ids(self):
        p = {"code": "G900001", "name": "示例组合", "type": 3, "products": [
            {"code": "900001", "name": "示例组合", "type": 7, "matchedSpecsCode": "2-1", "price": 2100, "unsellable": False}],
            "specTypes": [{"code": "2", "name": "饮品规格", "specs": [{"code": "1", "name": "配咖啡"}]}]}
        result = public_detail({"product": p}, {"productCode": "G900001", "offer_id": "synthetic"})
        self.assertFalse(result["standard_configuration_supported"])
        self.assertEqual(result["variants"][0]["product_code"], "900001")
        self.assertEqual(result["specifications"][0]["choices"][0]["name"], "配咖啡")


if __name__ == "__main__":
    unittest.main()
