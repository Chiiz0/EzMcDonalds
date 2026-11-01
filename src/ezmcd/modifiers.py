"""The pinned client's free binary ingredient choices, never arbitrary blobs.

The native cartModificationItemsFromDetail:oldItems: emits code, quantity=1
and selectedKey/unselectedKey inside modification.values. Those keys describe
the ingredient quantity; the outer quantity is always one for this shape.
"""
import re

from .errors import EzMcdError


def binary_groups(product):
    if product.get("grills") or product.get("biasChoices"):
        raise EzMcdError("advanced_customization_requires_official_cart")
    modification = product.get("modification") or {}
    if not isinstance(modification, dict) or set(modification) - {"items"}:
        raise EzMcdError("advanced_customization_requires_official_cart")
    groups = modification.get("items", [])
    if not isinstance(groups, list) or len(groups) > 20 or product.get("hasCustomization") and not groups:
        raise EzMcdError("advanced_customization_requires_official_cart")
    seen = set()
    for group in groups:
        values = group.get("values", [])
        low, high = group.get("minValues"), group.get("maxValues")
        if (not isinstance(values, list) or not values or type(low) is not int or type(high) is not int
                or not 0 <= low <= high <= len(values)):
            raise EzMcdError("advanced_customization_requires_official_cart")
        for item in values:
            code = item.get("code")
            if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{1,20}", code) or code in seen
                    or any(type(item.get(k)) is not int for k in ("displayMode", "minQuantity", "maxQuantity", "price"))
                    or item.get("displayMode") != 2 or item.get("minQuantity") != 0
                    or item.get("maxQuantity") != 1 or type(item.get("selectedQuantity")) is not int
                    or item["selectedQuantity"] not in (0, 1) or item.get("selectedKey") != "0-1"
                    or item.get("unselectedKey") != "0-0" or item.get("price") != 0
                    or item.get("soldOut") is not False):
                raise EzMcdError("advanced_customization_requires_official_cart")
            seen.add(code)
    if len(seen) > 20:
        raise EzMcdError("advanced_customization_requires_official_cart")
    return groups


def selected_modification(product, selections=None):
    groups = binary_groups(product)
    values = [v for g in groups for v in g["values"]]
    if selections is None:
        quantities = {v["code"]: v["selectedQuantity"] for v in values}
    else:
        quantities = {s.code: s.quantity for s in selections}
        if len(quantities) != len(selections) or set(quantities) != {v["code"] for v in values}:
            raise EzMcdError("select_every_modifier_exactly_once")
    for group in groups:
        if not group["minValues"] <= sum(quantities[v["code"]] for v in group["values"]) <= group["maxValues"]:
            raise EzMcdError("modifier_quantity_outside_live_limits")
    if not values:
        return {"customization": {"options": [], "items": []}}
    return {"modification": {"values": [{"code": v["code"], "quantity": 1,
        "key": v["selectedKey"] if quantities[v["code"]] else v["unselectedKey"]} for v in values]}}


def public_modifiers(product):
    try:
        groups = binary_groups(product)
        selected_modification(product)
    except (EzMcdError, TypeError, KeyError, AttributeError):
        return {"supported": False, "groups": [], "reason": "advanced_customization_requires_official_cart"}
    return {"supported": True, "selection_rule": "If modifiers are supplied, specify every listed code exactly once; 0 removes and 1 includes.",
        "groups": [{"index": i, "min_quantity": g["minValues"], "max_quantity": g["maxValues"],
            "choices": [{"code": v["code"], "name": v.get("name", ""), "default_quantity": v["selectedQuantity"],
                         "extra_cny": "0.00"} for v in g["values"]]} for i, g in enumerate(groups)]}


def verify_modification(expected, actual):
    """Stop if a server cart omitted/changed an explicitly submitted ingredient.

    Modern carts may echo modification.values or normalize them to legacy
    customization.options. Compare their code/key pairs, never the display text.
    """
    wanted = expected.get("modification", {}).get("values")
    if wanted is None:
        return
    try:
        received = (actual.get("modification") or {}).get("values")
        if received is None:
            received = (actual.get("customization") or {}).get("options", [])
        target = {v["code"]: v["key"] for v in wanted}
        got = {v["code"]: v["key"] for v in received}
        if len(got) != len(received) or target != got:
            raise ValueError
    except (AttributeError, TypeError, ValueError, KeyError):
        raise EzMcdError("cart_modifier_selection_not_confirmed_review_cart") from None
