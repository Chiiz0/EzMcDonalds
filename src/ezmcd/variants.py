"""Resolve an explicitly selected ordinary variant from its live parent detail.

The parent must be a regular menu offer. Its card/coupon identifiers are never
borrowed by a child. Related offers shown in detail are not selected implicitly.
"""
from copy import deepcopy
import re

from .errors import EzMcdError


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
        if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{1,20}", code) or str(product.get("type")) != "1"
                or product.get("unsellable") is not False or type(product.get("price")) is not int or product["price"] <= 0
                or not isinstance(product.get("name"), str) or not product["name"]
                or product.get("comboItems") or product.get("products")):
            raise EzMcdError("variant_product_requires_official_configuration")
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
        "_optional_promotions": bool(product.get("promotionInfos") or product.get("associationInfo"))}
    return resolved, deepcopy(detail) | {"product": product, "maxPurchaseQuantity": limit}
