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


def iced_coffee():
    p = {"code": "200002", "name": "示例冰咖啡", "type": 1, "unsellable": False,
         "modification": {"items": [{"title": "冰(单选)", "minValues": 1, "maxValues": 1, "values": [
             {"code": "222", "name": name, "displayMode": 1, "minQuantity": 0, "maxQuantity": 1,
              "selectedQuantity": int(key == "0-1"), "selectedKey": key, "price": 0, "soldOut": False}
             for key, name in (("0-1", "标准"), ("0-0", "去冰"), ("0-2", "多冰"), ("1-1", "少冰"))]},
             {"title": "浓缩(单选)", "minValues": 1, "maxValues": 1, "values": [
                 {"code": "333", "name": name, "displayMode": 1, "minQuantity": 0, "maxQuantity": 1,
                  "selectedQuantity": 0, "selectedKey": key, "price": price, "soldOut": True}
                 for key, name, price in (("0-0", "不加", 0), ("0-1", "加", 500))]}]}}
    return p


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

    def test_single_choice_default_and_explicit_ice_keys_preserve_native_quantity(self):
        p = iced_coffee()
        self.assertEqual(cart_configuration(p), {"modification": {"values": [{"code": "222", "quantity": 1, "key": "0-1"}]}})
        for key in ("0-0", "0-2", "1-1"):
            result = selected_modification(p, [ModifierSelection(code="222", quantity=1, key=key)])
            self.assertEqual(result["modification"]["values"], [{"code": "222", "quantity": 1, "key": key}])
            verify_modification(result, result)
            with self.assertRaisesRegex(EzMcdError, "not_confirmed"):
                verify_modification(result, cart_configuration(p))

    def test_inactive_sold_out_group_is_visible_but_cannot_be_submitted(self):
        view = public_modifiers(iced_coffee())
        self.assertTrue(view["supported"])
        self.assertEqual(view["groups"][0]["kind"], "single_choice")
        self.assertTrue(view["groups"][1]["inactive"])
        self.assertEqual(view["groups"][1]["choices"][1]["extra_cny"], "5.00")
        with self.assertRaisesRegex(EzMcdError, "exactly_once"):
            selected_modification(iced_coffee(), [ModifierSelection(code="222", quantity=1, key="1-1"),
                                                ModifierSelection(code="333", quantity=1, key="0-0")])

    def test_single_choice_legacy_item_echo_is_checked_by_code_and_key(self):
        expected = selected_modification(iced_coffee(), [ModifierSelection(code="222", quantity=1, key="1-1")])
        actual = {"customization": {"options": [], "items": [{"code": "222", "name": "冰", "key": "1-1",
            "displayText": "冰(少冰)", "values": [{"code": "4", "name": "少冰", "price": 0, "quantity": 1}]}]}}
        verify_modification(expected, actual)
        verify_modification(expected, expected | actual)
        for change in ("wrong_key", "wrong_code", "paid", "two_choices", "unselected", "duplicate", "conflicting_modern"):
            wrong = deepcopy(actual)
            item = wrong["customization"]["items"][0]
            if change == "wrong_key": item["key"] = "0-1"
            if change == "wrong_code": item["code"] = "999"
            if change == "paid": item["values"][0]["price"] = 100
            if change == "two_choices": item["values"].append(deepcopy(item["values"][0]))
            if change == "unselected": item["values"][0]["quantity"] = 0
            if change == "duplicate": wrong["customization"]["options"] = [{"code": "222", "key": "1-1"}]
            if change == "conflicting_modern": wrong.update(selected_modification(iced_coffee()))
            with self.subTest(change=change), self.assertRaisesRegex(EzMcdError, "not_confirmed"):
                verify_modification(expected, wrong)

    def test_legacy_toggles_and_single_choice_items_are_verified_together(self):
        p = iced_coffee()
        p["modification"]["items"].extend(coffee()["modification"]["items"])
        expected = selected_modification(p)
        actual = {"customization": {"options": deepcopy(expected["modification"]["values"][1:]), "items": [
            {"code": "222", "key": "0-1", "values": [{"code": "1", "price": 0, "quantity": 1}]}]}}
        verify_modification(expected, actual)
        actual["customization"]["options"].pop()
        with self.assertRaisesRegex(EzMcdError, "not_confirmed"):
            verify_modification(expected, actual)

    def test_modern_echo_requires_selected_outer_quantity(self):
        expected = selected_modification(iced_coffee())
        for quantity in (0, 2, True, "1", None):
            wrong = deepcopy(expected)
            wrong["modification"]["values"][0]["quantity"] = quantity
            with self.subTest(quantity=quantity), self.assertRaisesRegex(EzMcdError, "not_confirmed"):
                verify_modification(expected, wrong)

    def test_single_choice_rejects_paid_sold_out_unknown_duplicate_or_zero_selections(self):
        for values in ([{"code": "222", "quantity": 1}], [{"code": "222", "quantity": 0, "key": "0-0"}],
                       [{"code": "222", "quantity": 1, "key": "9-9"}],
                       [{"code": "222", "quantity": 1, "key": k} for k in ("0-0", "1-1")]):
            with self.assertRaises(EzMcdError):
                selected_modification(iced_coffee(), [ModifierSelection(**v) for v in values])
        for change in ({"price": 100}, {"soldOut": True}):
            p = iced_coffee(); p["modification"]["items"][0]["values"][3].update(change)
            with self.assertRaisesRegex(EzMcdError, "available_free"):
                selected_modification(p, [ModifierSelection(code="222", quantity=1, key="1-1")])
            self.assertTrue(public_modifiers(p)["supported"])

    def test_no_default_missing_or_conflicting_code_group_is_not_guessed(self):
        for change in ("missing_default", "duplicate_default", "duplicate_key", "different_code"):
            p = iced_coffee(); values = p["modification"]["items"][0]["values"]
            if change == "missing_default": values[0]["selectedQuantity"] = 0
            if change == "duplicate_default": values[1]["selectedQuantity"] = 1
            if change == "duplicate_key": values[1]["selectedKey"] = "0-1"
            if change == "different_code": values[1]["code"] = "444"
            self.assertFalse(public_modifiers(p)["supported"])

    def test_single_choice_and_toggle_groups_can_be_combined_without_code_collisions(self):
        p = iced_coffee(); p["modification"]["items"].extend(coffee()["modification"]["items"])
        result = selected_modification(p, [ModifierSelection(code="222", quantity=1, key="1-1"),
            ModifierSelection(code="111", quantity=1), ModifierSelection(code="112", quantity=0)])
        self.assertEqual({v["code"]: v["key"] for v in result["modification"]["values"]}, {"222": "1-1", "111": "0-1", "112": "0-0"})
        with self.assertRaisesRegex(EzMcdError, "does_not_accept"):
            selected_modification(coffee(), [ModifierSelection(code="111", quantity=1, key="0-1"), ModifierSelection(code="112", quantity=0)])


if __name__ == "__main__":
    unittest.main()
