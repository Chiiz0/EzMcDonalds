"""Resolve an explicitly selected ordinary single or combo variant from its parent.

The parent must be a regular menu offer. Its card/coupon identifiers are never
borrowed by a child. Related offers shown in detail are not selected implicitly.
"""
from copy import deepcopy
import re

from .errors import EzMcdError


def combo_base(product):
    """Validate the observed additive combo shape before using its base price.

The displayed variant price already includes default component differences.
Never add those differences twice, or infer a base from display text.
"""
    base = product.get("startingPrice")
    groups = product.get("comboItems")
    try:
        if (product.get("priceFormula") is not True or type(base) is not int or not 0 < base <= 100000
                or not isinstance(groups, list) or not 1 <= len(groups) <= 20
                or any(product.get(k) for k in ("grills", "biasChoices", "modification", "customization", "hasCustomization"))):
            raise ValueError
        rounds, extra = set(), 0
        for group in groups:
            number, low, high = (group[k] for k in ("round", "minQuantity", "maxQuantity"))
            choices = group["comboProducts"]
            if (type(number) is not int or not 1 <= number <= 100 or number in rounds
                    or type(low) is not int or type(high) is not int or not 0 <= low <= high <= 20
                    or type(group.get("isChoices")) is not int or group["isChoices"] not in (0, 1)
                    or not isinstance(choices, list) or not 1 <= len(choices) <= 100
                    or not isinstance(group.get("choicesCode"), str)
                    or (group["isChoices"] == 1 and not re.fullmatch(r"[0-9]{1,20}", group["choicesCode"]))
                    or (group["isChoices"] == 0 and group["choicesCode"] != "")):
                raise ValueError
            rounds.add(number)
            codes, selected = set(), 0
            for part in choices:
                code, quantity, difference = (part[k] for k in ("code", "quantity", "diffPrice"))
                maximum = part.get("comboMaxQuantity", -1)
                if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{1,20}", code) or code in codes
                        or not isinstance(part.get("name"), str) or not part["name"]
                        or type(quantity) is not int or not 0 <= quantity <= high
                        or type(difference) is not int or not 0 <= difference <= 100000
                        or type(maximum) is not int or not -1 <= maximum <= 1000
                        or (maximum != -1 and quantity > maximum)
                        or type(part.get("unsellable")) is not bool or part.get("products") or part.get("comboItems")
                        or any(part.get(k) for k in ("cardId", "couponId", "couponCode", "promotionId", "productPromotions"))):
                    raise ValueError
                codes.add(code)
                selected += quantity
                extra += quantity * difference
            if not low <= selected <= high:
                raise ValueError
        if product["price"] != base + extra:
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError):
        raise EzMcdError("variant_combo_schema_or_price_changed") from None
    return base


def named_specifications(parent, product):
    selected = dict(pair.split("-") for pair in product["matchedSpecsCode"].split(","))
    return [{"group_code": group["code"], "group_name": group.get("name"),
             "choice_code": choice["code"], "choice_name": choice.get("name")}
            for group in parent["specTypes"] for choice in group["specs"]
            if choice["code"] == selected[group["code"]]]


def resolve_variant(detail, offer, code):
    if not isinstance(detail, dict) or not isinstance(detail.get("product"), dict) or not isinstance(offer, dict):
        raise EzMcdError("variant_specification_schema_changed")
    parent = detail["product"]
    if (str(offer.get("productType")) != "3" or str(parent.get("type")) != "3"
            or parent.get("code") != offer.get("productCode") or parent.get("unsellable") or offer.get("unsellable")):
        raise EzMcdError("variant_parent_identity_or_availability_changed")
    if offer.get("_coupon_source") or any(offer.get(k) for k in
            ("cardId", "cardName", "couponId", "couponCode", "promotionId", "productPromotions")):
        raise EzMcdError("variant_discount_requires_own_menu_offer")
    variants, specs = parent.get("products"), parent.get("specTypes")
    try:
        if not isinstance(variants, list) or not variants or not isinstance(specs, list) or not specs:
            raise ValueError
        dimensions = {}
        for group in specs:
            key = group["code"]
            choices = [v["code"] for v in group["specs"]]
            if (not isinstance(key, str) or not re.fullmatch(r"[0-9]{1,8}", key) or key in dimensions
                    or not choices or len(set(choices)) != len(choices)
                    or any(not isinstance(v, str) or not re.fullmatch(r"[0-9]{1,8}", v) for v in choices)):
                raise ValueError
            dimensions[key] = set(choices)
        codes, combinations = set(), set()
        for variant in variants:
            if variant["code"] in codes:
                raise ValueError
            codes.add(variant["code"])
            pairs = [v.split("-") for v in variant["matchedSpecsCode"].split(",")]
            if any(len(pair) != 2 for pair in pairs):
                raise ValueError
            selected = dict(pairs)
            combination = tuple(sorted(selected.items()))
            if (len(selected) != len(pairs) or set(selected) != set(dimensions) or combination in combinations
                    or any(value not in dimensions[key] for key, value in selected.items())):
                raise ValueError
            combinations.add(combination)
        found = [p for p in variants if p["code"] == code]
        if len(found) != 1:
            raise EzMcdError("variant_not_in_current_parent_detail")
        product = deepcopy(found[0])
        if any(product.get(k) for k in ("cardId", "cardName", "couponId", "couponCode", "promotionId", "productPromotions")):
            raise EzMcdError("variant_discount_requires_own_menu_offer")
        kind = str(product.get("type"))
        if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{1,20}", code) or kind not in ("1", "7")
                or product.get("unsellable") is not False or type(product.get("price")) is not int or product["price"] <= 0
                or not isinstance(product.get("name"), str) or not product["name"]
                or (kind == "1" and product.get("comboItems")) or product.get("products")):
            raise EzMcdError("variant_product_requires_official_configuration")
        base = combo_base(product) if kind == "7" else product["price"]
        limit, parent_limit = detail["maxPurchaseQuantity"], parent.get("limitedQuantity", 0)
        if type(limit) is not int or limit < 1 or type(parent_limit) is not int or parent_limit < 0:
            raise ValueError
        if parent_limit:
            limit = min(limit, parent_limit)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise EzMcdError("variant_specification_schema_changed") from None
    # The detail may advertise second-cup offers. A regular single variant does
    # not select associationInfo products or promotionInfos; checkout must still
    # disclose and validate the actual resulting price and discount.
    resolved = {"productCode": code, "productType": product["type"], "productName": product["name"],
        "offer_id": offer["offer_id"], "priceInfo": {"eatInPrice": product["price"]},
        "_variant_of": parent["code"], "_variant_specs": product["matchedSpecsCode"],
        "_variant_specifications": named_specifications(parent, product), "_configuration_base_cents": base,
        "_optional_promotions": bool(product.get("promotionInfos") or product.get("associationInfo"))}
    return resolved, deepcopy(detail) | {"product": product, "maxPurchaseQuantity": limit}
