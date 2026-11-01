"""The pinned client's toggles and single choices, never arbitrary blobs.

The native cartModificationItemsFromDetail:oldItems: emits code, quantity=1
and selectedKey/unselectedKey inside modification.values. Those keys describe
the ingredient quantity; the outer quantity is always one for this shape.
"""
import re

from .errors import EzMcdError


def modifier_groups(product):
    if product.get("grills") or product.get("biasChoices"):
        raise EzMcdError("advanced_customization_requires_official_cart")
    modification = product.get("modification") or {}
    if not isinstance(modification, dict) or set(modification) - {"items"}:
        raise EzMcdError("advanced_customization_requires_official_cart")
    groups = modification.get("items", [])
    if not isinstance(groups, list) or len(groups) > 20 or product.get("hasCustomization") and not groups:
        raise EzMcdError("advanced_customization_requires_official_cart")
    seen, result = set(), []
    for group in groups:
        values = group.get("values", [])
        low, high = group.get("minValues"), group.get("maxValues")
        if (not isinstance(values, list) or not values or type(low) is not int or type(high) is not int
                or not 0 <= low <= high <= len(values)):
            raise EzMcdError("advanced_customization_requires_official_cart")
        modes = {v.get("displayMode") for v in values}
        if modes not in ({1}, {2}):
            raise EzMcdError("advanced_customization_requires_official_cart")
        single = modes == {1}
        optional = single and low == 0 and high == 1 and len(values) == 1
        codes, keys = set(), set()
        if single and not optional and (low != 1 or high != 1 or len(values) > 20):
            raise EzMcdError("advanced_customization_requires_official_cart")
        for item in values:
            code = item.get("code")
            if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{1,20}", code) or code in seen
                    or any(type(item.get(k)) is not int for k in ("displayMode", "minQuantity", "maxQuantity", "price"))
                    or item.get("minQuantity") != 0
                    or item.get("maxQuantity") != 1 or type(item.get("selectedQuantity")) is not int
                    or item["selectedQuantity"] not in (0, 1) or type(item.get("soldOut")) is not bool):
                raise EzMcdError("advanced_customization_requires_official_cart")
            if optional:
                # Observed optional milk replacement: its unselected default
                # costs nothing; the user must explicitly choose the surcharge.
                if (item.get("selectedKey") != "0-1" or item.get("unselectedKey") != "0-0"
                        or item["selectedQuantity"] != 0 or not 0 <= item["price"] <= 100000):
                    raise EzMcdError("advanced_customization_requires_official_cart")
            elif single:
                key = item.get("selectedKey")
                if (not isinstance(key, str) or not re.fullmatch(r"[0-9]{1,4}-[0-9]{1,4}", key)
                        or key in keys or item.get("unselectedKey") or item["price"] < 0):
                    raise EzMcdError("advanced_customization_requires_official_cart")
                keys.add(key)
            elif (code in codes or item.get("selectedKey") != "0-1" or item.get("unselectedKey") != "0-0"
                    or item["price"] != 0 or item["soldOut"]):
                raise EzMcdError("advanced_customization_requires_official_cart")
            codes.add(code)
        if single and len(codes) != 1:
            raise EzMcdError("advanced_customization_requires_official_cart")
        seen.update(codes)
        inactive = single and not optional and all(v["soldOut"] and v["selectedQuantity"] == 0 for v in values)
        result.append(group | {"_kind": "optional_choice" if optional else "single_choice" if single else "toggle", "_inactive": inactive})
    if len(seen) > 20:
        raise EzMcdError("advanced_customization_requires_official_cart")
    return result


def selected_modification(product, selections=None):
    groups = [g for g in modifier_groups(product) if not g["_inactive"]]
    choices = {s.code: s for s in selections} if selections is not None else None
    if choices is not None:
        if len(choices) != len(selections) or set(choices) != {v["code"] for g in groups for v in g["values"]}:
            raise EzMcdError("select_every_modifier_exactly_once")
    values, prices = [], {}
    for group in groups:
        options = group["values"]
        if group["_kind"] == "optional_choice":
            item = options[0]
            quantity = choices[item["code"]].quantity if choices is not None else 0
            if choices is not None and choices[item["code"]].key is not None:
                raise EzMcdError("toggle_modifier_does_not_accept_choice_key")
            if quantity and item["soldOut"]:
                raise EzMcdError("modifier_choice_unavailable")
            values.append({"code": item["code"], "quantity": 1,
                           "key": item["selectedKey"] if quantity else item["unselectedKey"]})
            if quantity and item["price"]:
                prices[item["code"]] = item["price"]
        elif group["_kind"] == "single_choice":
            if choices is None:
                selected = [v for v in options if v["selectedQuantity"] == 1]
            else:
                choice = choices[options[0]["code"]]
                selected = [v for v in options if v["selectedKey"] == choice.key and choice.quantity == 1]
            if len(selected) != 1 or selected[0]["soldOut"] or selected[0]["price"] != 0:
                raise EzMcdError("select_one_available_free_modifier_choice")
            values.append({"code": selected[0]["code"], "quantity": 1, "key": selected[0]["selectedKey"]})
        else:
            quantities = {v["code"]: choices[v["code"]].quantity if choices is not None else v["selectedQuantity"] for v in options}
            if choices is not None and any(choices[v["code"]].key is not None for v in options):
                raise EzMcdError("toggle_modifier_does_not_accept_choice_key")
            if not group["minValues"] <= sum(quantities.values()) <= group["maxValues"]:
                raise EzMcdError("modifier_quantity_outside_live_limits")
            values.extend({"code": v["code"], "quantity": 1,
                "key": v["selectedKey"] if quantities[v["code"]] else v["unselectedKey"]} for v in options)
    if not values:
        return {"customization": {"options": [], "items": []}}
    return {"modification": {"values": values}} | ({"_ezmcd_modifier_prices": prices} if prices else {})


def public_modifiers(product):
    try:
        groups = modifier_groups(product)
        selected_modification(product)
    except (EzMcdError, TypeError, KeyError, AttributeError):
        return {"supported": False, "groups": [], "reason": "advanced_customization_requires_official_cart"}
    from .models import yuan
    return {"supported": True, "selection_rule": "Specify every active code once: toggle and optional_choice use quantity 0/1 without key; single_choice uses quantity=1 and the selected listed key. Inactive groups are omitted. Optional choices may cost extra; always obtain a fresh quote.",
        "groups": [{"index": i, "name": g.get("title", ""), "kind": g["_kind"], "inactive": g["_inactive"],
            "min_quantity": g["minValues"], "max_quantity": g["maxValues"],
            "choices": [{"code": v["code"], "name": v.get("name", ""), "default_quantity": v["selectedQuantity"],
                         "available": not v["soldOut"], "extra_cny": yuan(v["price"]),
                         "selection_supported": g["_kind"] != "single_choice" or v["price"] == 0,
                         **({"key": v["selectedKey"]} if g["_kind"] == "single_choice" else {})}
                        for v in g["values"]]} for i, g in enumerate(groups)]}


def configuration_keys(actual, *, allow_paid=False):
    """Normalize observed choices; paid echoes require explicit caller support."""
    try:
        if actual.get("grills") or actual.get("biasChoices"):
            raise ValueError
        representations = []
        modification = actual.get("modification") or {}
        if not isinstance(modification, dict) or set(modification) - {"values"}:
            raise ValueError
        modern = modification.get("values")
        if modern is not None:
            if not isinstance(modern, list) or any(type(v.get("quantity")) is not int or v["quantity"] != 1 for v in modern):
                raise ValueError
            representations.append(modern)
        legacy = actual.get("customization") or {}
        if not isinstance(legacy, dict) or set(legacy) - {"options", "items"}:
            raise ValueError
        options, items = legacy.get("options", []), legacy.get("items", [])
        if not isinstance(options, list) or not isinstance(items, list):
            raise ValueError
        if any("price" in v and (type(v["price"]) is not int or v["price"] != 0) for v in options):
            raise ValueError
        # Group code/key identifies the choice. Paid optional replacements use
        # key 0-1 and one selected value; their exact price is compared separately.
        for item in items:
            values = item["values"]
            if (not isinstance(values, list) or len(values) != 1
                    or type(values[0].get("quantity")) is not int or values[0]["quantity"] not in (0, 1)
                    or type(values[0].get("price")) is not int or not 0 <= values[0]["price"] <= 100000
                    or values[0]["quantity"] == 0 and item.get("key") != "0-0"
                    or values[0]["quantity"] == 1 and values[0]["price"] != 0 and (not allow_paid or item.get("key") != "0-1")):
                raise ValueError
        if options or items:
            representations.append(options + items)
        if not representations:
            return {}
        choices = None
        for received in representations:
            got = {v["code"]: v["key"] for v in received}
            if (len(got) != len(received) or any(not isinstance(code, str) or not re.fullmatch(r"[0-9]{1,20}", code)
                    or not isinstance(key, str) or not re.fullmatch(r"[0-9]{1,4}-[0-9]{1,4}", key) for code, key in got.items())
                    or choices is not None and choices != got):
                raise ValueError
            choices = got
        return choices
    except (AttributeError, TypeError, ValueError, KeyError):
        raise EzMcdError("cart_modifier_selection_not_confirmed_review_cart") from None


def configuration_signature(actual, *, requested=False):
    """Bind a paid echo to the exact surcharge saved before the cart write.

    Local price evidence is never sent upstream or accepted from cart responses.
    Free choices retain their original comparison, including both echo formats.
    """
    keys = configuration_keys(actual, allow_paid=not requested)
    if requested:
        prices = actual.get("_ezmcd_modifier_prices", {})
        if (not isinstance(prices, dict) or any(code not in keys or keys[code] != "0-1"
                or type(price) is not int or not 0 < price <= 100000 for code, price in prices.items())):
            raise EzMcdError("cart_modifier_selection_not_confirmed_review_cart")
    else:
        prices = {item["code"]: item["values"][0]["price"]
                  for item in (actual.get("customization") or {}).get("items", [])
                  if item["values"][0]["quantity"] == 1 and item["values"][0]["price"]}
    return {"keys": keys, "paid": prices}


def modifier_extra_cents(actual):
    return sum(configuration_signature(actual)["paid"].values())


def cart_wire_body(draft):
    """Remove only our local price evidence from the outgoing native shape."""
    from copy import deepcopy
    body = deepcopy(draft)
    for product in body.get("products", []):
        product.pop("_ezmcd_modifier_prices", None)
        for group in product.get("comboItems", []):
            for part in group.get("comboProducts", []):
                part.pop("_ezmcd_modifier_prices", None)
    return body


def verify_modification(expected, actual):
    """Stop when the server changes an explicitly submitted ingredient."""
    wanted = expected.get("modification", {}).get("values")
    if wanted is not None and configuration_signature(actual) != configuration_signature(expected, requested=True):
        raise EzMcdError("cart_modifier_selection_not_confirmed_review_cart")
