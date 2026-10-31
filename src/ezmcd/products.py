"""Validate explicit combo selections against fresh official product detail.

No client-provided price, coupon, customization blob or upstream identifier is
trusted. Advanced customization continues to use the configured official cart.
"""
from .errors import EzMcdError
from .models import yuan


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
        standard_customization(product)
        return True
    except EzMcdError:
        return False


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
                "standard_configuration_supported": configurable(p)} for p in group.get("comboProducts", [])]})
    return {"product_code": product["code"], "name": product["name"], "product_type": product["type"],
        "offer_id": offer["offer_id"], "card_name": offer.get("cardName"), "groups": groups,
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
    elif offer.get("couponId") or offer.get("productPromotions"):
        raise EzMcdError("promotion_configuration_requires_official_cart")
    groups = product.get("comboItems", [])
    selected = {s.round: s for s in selection.combo}
    if len(selected) != len(selection.combo) or set(selected) != {g["round"] for g in groups}:
        raise EzMcdError("select_each_combo_round_exactly_once")
    if kind == "1":
        if groups or selection.combo:
            raise EzMcdError("unexpected_combo_on_single_product")
        result["customization"] = standard_customization(product)
        return result
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
            values.append({"code": p["code"], "name": p["name"], "quantity": choice.quantity,
                           "customization": standard_customization(p)})
        result["comboItems"].append({k: group[k] for k in ("round", "isChoices", "choicesCode")} | {"comboProducts": values})
    return result
