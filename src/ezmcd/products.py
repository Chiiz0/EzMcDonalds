"""Validate explicit combo selections against fresh official product detail.

No client-provided price, coupon, customization blob or upstream identifier is
trusted. Advanced customization continues to use the configured official cart.
"""
from .errors import EzMcdError
from .models import yuan
from .modifiers import public_modifiers, selected_modification, verify_modification
from .coupons import coupon_fields, verify_coupon_cart


def choose_offer(menu, product_code, offer_id=None):
    matches = {p["offer_id"]: p for p in menu if str(p.get("productCode")) == product_code
               and (offer_id is None or p["offer_id"] == offer_id)}
    if len(matches) != 1:
        raise EzMcdError("select_exact_live_menu_offer")
    return next(iter(matches.values()))


def standard_customization(product):
    """Only the observed, singleton default-condiment shape is serialized.

    Do not guess the encoding for ice, milk, size, or multi-choice modifiers.
    Products with no modifiers have an explicitly empty customization.
    """
    if product.get("grills") or product.get("biasChoices"):
        raise EzMcdError("advanced_customization_requires_official_cart")
    modifications = product.get("modification") or {}
    if set(modifications) - {"items"}:
        raise EzMcdError("advanced_customization_requires_official_cart")
    groups = modifications.get("items", [])
    if product.get("hasCustomization") and not groups:
        raise EzMcdError("advanced_customization_requires_official_cart")
    options = []
    for group in groups:
        values = group.get("values", [])
        if (len(values) != 1 or group.get("maxValues") != 1 or group.get("minValues") != 0):
            raise EzMcdError("advanced_customization_requires_official_cart")
        item = values[0]
        if (item.get("displayMode") != 2 or item.get("selectedQuantity") != 1
                or item.get("selectedKey") != "0-1" or item.get("price") != 0
                or item.get("soldOut") is not False):
            raise EzMcdError("advanced_customization_requires_official_cart")
        options.append({"code": item["code"], "name": item["name"], "key": "0-1",
            "checked": 1, "price": 0, "quantity": 1, "checkedCode": "1", "displayText": ""})
    return {"options": options, "items": []}


def configurable(product):
    try:
        cart_configuration(product)
        return True
    except EzMcdError:
        return False


def cart_configuration(product, modifiers=None):
    if modifiers is None:
        try:
            return {"customization": standard_customization(product)}
        except EzMcdError:
            pass
    return selected_modification(product, modifiers)


def verify_configured_cart(draft, cart):
    """Verify the new modifier path after the one cart write; never retry it."""
    for expected in draft.get("products", []):
        verify_coupon_cart(expected, cart.get("items", []))
        checks = [expected] + [p for g in expected.get("comboItems", []) for p in g.get("comboProducts", [])]
        if not any("modification" in p for p in checks):
            continue
        rows = [p for p in cart.get("items", []) if p["product_code"] == expected["code"]]
        if len(rows) != 1 or rows[0]["quantity"] != expected["quantity"]:
            raise EzMcdError("cart_modifier_selection_not_confirmed_review_cart")
        row = rows[0]
        verify_modification(expected, row.get("configuration", {}))
        for group in expected.get("comboItems", []):
            actual_groups = [g for g in row["components"] if g.get("round") == group["round"]]
            for part in group.get("comboProducts", []):
                if "modification" not in part:
                    continue
                actual = [p for g in actual_groups for p in g.get("comboProducts", []) if p.get("code") == part["code"]]
                if len(actual_groups) != 1 or len(actual) != 1 or actual[0].get("quantity") != part["quantity"]:
                    raise EzMcdError("cart_modifier_selection_not_confirmed_review_cart")
                verify_modification(part, actual[0])


def public_detail(detail, offer):
    product = detail["product"]
    if str(product.get("code")) != str(offer["productCode"]):
        raise EzMcdError("product_detail_identity_changed")
    groups = []
    for group in product.get("comboItems", []):
        groups.append({"round": group["round"], "name": group.get("name", ""),
            "min_quantity": group["minQuantity"], "max_quantity": group["maxQuantity"],
            "choices": [{"product_code": p["code"], "name": p["name"],
                "available": p.get("unsellable") is False,
                "current_quantity": p.get("quantity", 0),
                "extra_cny": yuan(p["diffPrice"]) if type(p.get("diffPrice")) is int and p["diffPrice"] >= 0 else None,
                "standard_configuration_supported": configurable(p), "modifiers": public_modifiers(p)} for p in group.get("comboProducts", [])]})
    return {"product_code": product["code"], "name": product["name"], "product_type": product["type"],
        "offer_id": offer["offer_id"], "card_name": offer.get("cardName"), "groups": groups,
        "modifiers": public_modifiers(product), "standard_configuration_supported": str(product.get("type")) in ("1", "7") and configurable(product),
        "variants": [{"product_code": p.get("code"), "name": p.get("name"), "product_type": p.get("type"),
            "specification_code": p.get("matchedSpecsCode"), "available": p.get("unsellable") is False,
            "display_price_cny": yuan(p["price"]) if type(p.get("price")) is int and p["price"] >= 0 else None}
            for p in product.get("products", [])] if str(product.get("type")) == "3" else [],
        "specifications": [{"code": g.get("code"), "name": g.get("name"),
            "choices": [{"code": p.get("code"), "name": p.get("name"), "price_note": p.get("spread")} for p in g.get("specs", [])]}
            for g in product.get("specTypes", [])] if str(product.get("type")) == "3" else [],
        "max_quantity": detail.get("maxPurchaseQuantity"), "display_price_is_final_quote": False,
        "advanced_customization": "Configure in official App, then use cart_get and checkout_quote."}


def configured_product(selection, offer, detail, sequence):
    product = detail["product"]
    if str(product.get("code")) != selection.product_code or str(product.get("type")) != str(offer.get("productType")):
        raise EzMcdError("product_detail_identity_changed")
    kind = str(product["type"])
    if kind not in ("1", "7") or product.get("unsellable") or offer.get("unsellable"):
        raise EzMcdError("product_type_or_availability_not_supported")
    limit = detail.get("maxPurchaseQuantity")
    if type(limit) is not int or limit < selection.quantity:
        raise EzMcdError("product_quantity_exceeds_live_limit")
    limited = product.get("limitedQuantity", 0)
    if limited and selection.quantity > limited:
        raise EzMcdError("product_quantity_exceeds_live_limit")
    result = {"code": selection.product_code, "type": kind, "quantity": selection.quantity,
              "sequence": sequence, "operationType": 0, "useRightCard": bool(offer.get("cardId"))}
    if offer.get("cardId"):
        for field in ("cardId", "cardType", "couponId", "couponCode", "promotionId"):
            if offer.get(field) in (None, ""):
                raise EzMcdError("live_card_offer_incomplete")
            result[field] = offer[field]
    elif offer.get("_coupon_source"):
        result.update(coupon_fields(offer, selection.quantity))
    elif offer.get("couponId") or offer.get("productPromotions"):
        raise EzMcdError("promotion_configuration_requires_official_cart")
    groups = product.get("comboItems", [])
    selected = {s.round: s for s in selection.combo}
    if len(selected) != len(selection.combo) or set(selected) != {g["round"] for g in groups}:
        raise EzMcdError("select_each_combo_round_exactly_once")
    if kind == "1":
        if groups or selection.combo:
            raise EzMcdError("unexpected_combo_on_single_product")
        result.update(cart_configuration(product, selection.modifiers))
        return result
    if selection.modifiers is not None:
        raise EzMcdError("select_modifiers_on_combo_components")
    if not groups or len({g["round"] for g in groups}) != len(groups):
        raise EzMcdError("unsupported_combo_group_schema")
    result["comboItems"] = []
    for group in groups:
        choices = selected[group["round"]].items
        if len({s.product_code for s in choices}) != len(choices):
            raise EzMcdError("duplicate_component_combine_quantity")
        total = sum(s.quantity for s in choices)
        if not group["minQuantity"] <= total <= group["maxQuantity"]:
            raise EzMcdError("combo_round_quantity_outside_live_limits")
        values = []
        for choice in choices:
            options = [p for p in group["comboProducts"] if p["code"] == choice.product_code]
            if len(options) != 1 or options[0].get("unsellable") is not False:
                raise EzMcdError("component_not_available_in_selected_round")
            p = options[0]
            maximum = p.get("comboMaxQuantity", -1)
            if maximum != -1 and choice.quantity > maximum:
                raise EzMcdError("component_quantity_exceeds_live_limit")
            values.append({"code": p["code"], "name": p["name"], "quantity": choice.quantity}
                          | cart_configuration(p, choice.modifiers))
        result["comboItems"].append({k: group[k] for k in ("round", "isChoices", "choicesCode")} | {"comboProducts": values})
    return result
