"""Bind a parent-page card variant to fresh, explicit member eligibility.

The observed coffee-card list includes medium sizes but excludes the large
variant on the same parent page. Parent membership alone is never sufficient.
"""
import re

from .coupons import availability_issues
from .errors import EzMcdError
from .variants import resolve_variant

CARD_FIELDS = ("cardId", "cardType", "couponId", "couponCode", "promotionId")
CARD_BINDINGS = (("cardId", "cardId"), ("couponId", "id"),
                 ("couponCode", "code"), ("promotionId", "promotionId"))


def matching_card(rows, offer):
    if (not isinstance(offer, dict) or type(offer.get("cardType")) is not int or offer["cardType"] != 2
            or any(not isinstance(offer.get(k), str) or not 0 < len(offer[k]) <= 200
                   for k, _ in CARD_BINDINGS) or offer.get("_coupon_source")):
        raise EzMcdError("variant_card_offer_not_supported")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise EzMcdError("variant_card_not_available_for_request")
    matches = [row for row in rows if all(offer[k] == row.get(source) for k, source in CARD_BINDINGS)]
    if len(matches) != 1:
        raise EzMcdError("variant_card_not_available_for_request")
    card = matches[0]
    if availability_issues(card):
        raise EzMcdError("variant_card_not_available_for_request")
    if (any(type(card.get(k)) is not int or card[k] != expected for k, expected in (
            ("couponCardType", 2), ("couponPromotionType", 1), ("isPrepaid", 0), ("paymentType", 0)))
            or card.get("multipleProducts") is not False or not isinstance(card.get("title"), str)
            or not card["title"]):
        raise EzMcdError("variant_card_offer_not_supported")
    return card


def _promotion_limit(offer, code, eligible_codes):
    """The parent also declares which children use this exact card promotion."""
    try:
        rows = offer["productPromotions"]
        if not isinstance(rows, list) or not rows or len(rows) > 200:
            raise ValueError
        limits = {}
        for row in rows:
            product_code, info = row["productCode"], row["promotionInfo"]
            if (product_code in limits or product_code not in eligible_codes
                    or info["cardId"] != offer["cardId"] or info["promotionId"] != offer["promotionId"]
                    or type(info.get("cardType")) is not int or info["cardType"] != 2
                    or type(info.get("pmtType")) is not int or info["pmtType"] != 1
                    or info.get("hasRights") is not True or info.get("withOrder") is not False
                    or type(info.get("promotionLimitNum")) is not int or info["promotionLimitNum"] <= 0):
                raise ValueError
            limits[product_code] = info["promotionLimitNum"]
        if code not in limits:
            raise EzMcdError("variant_not_eligible_for_selected_card")
        return limits[code]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise EzMcdError("variant_card_promotion_binding_changed") from None


def resolve_card_variant(detail, offer, code, evidence):
    card, raw = evidence
    matching_card([card], offer)
    try:
        products = raw["productList"]
        if (raw.get("multipleProducts") is not True or type(raw.get("isPrepaid")) is not int or raw["isPrepaid"] != 0
                or type(raw.get("maxPurchaseQuantity")) is not int or raw["maxPurchaseQuantity"] <= 0
                or not isinstance(products, list) or not 1 <= len(products) <= 200):
            raise ValueError
        codes = [p["productCode"] for p in products]
        if (len(set(codes)) != len(codes) or any(not isinstance(c, str) or not re.fullmatch(r"[0-9]{1,20}", c) for c in codes)
                or any(type(p.get("count")) is not int or p["count"] != 1 or str(p.get("productType")) != "1"
                       or p.get("groupList") or p.get("isChoices") or p.get("supportChoice") for p in products)):
            raise ValueError
        # These three observed lists must agree; do not reinterpret a condition
        # list or a mixed bundle as permission to discount every parent variant.
        for name in ("conditionalProductCodes", "specialProductCodes"):
            listed = card[name]
            if not isinstance(listed, list) or len(listed) != len(set(listed)) or set(listed) != set(codes):
                raise ValueError
        found = [p for p in products if p["productCode"] == code]
        if len(found) != 1:
            raise EzMcdError("variant_not_eligible_for_selected_card")
        entry = found[0]
        if type(entry.get("saleStatus")) is not int or entry["saleStatus"] != 1:
            raise EzMcdError("variant_not_eligible_for_selected_card")
        regular, discounted = (entry["priceInfo"][k] for k in ("eatInPrice", "discountPrice"))
        if any(type(v) is not int for v in (regular, discounted)) or not 0 < discounted <= regular <= 100000:
            raise ValueError
        per_product = entry.get("maxPurchaseQuantity", 0)
        if type(per_product) is not int or per_product < 0:
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError):
        raise EzMcdError("variant_card_product_list_changed") from None
    promotion_limit = _promotion_limit(offer, code, set(codes))
    # Reuse ordinary structural/specification validation only after separately
    # establishing this exact card/member/product binding. No discount fields
    # enter that structural resolver, and no unrelated promotion is transferred.
    plain = {k: offer[k] for k in ("productCode", "productType", "offer_id", "unsellable") if k in offer}
    resolved, selected = resolve_variant(detail, plain, code)
    if str(selected["product"]["type"]) != "1" or selected["product"]["price"] != regular:
        raise EzMcdError("variant_card_regular_price_mismatch")
    limit = min(selected["maxPurchaseQuantity"], raw["maxPurchaseQuantity"],
                card["currentDayAvailableCount"], card["totalAvailableCount"], promotion_limit)
    if per_product > 0:
        limit = min(limit, per_product)
    selected["maxPurchaseQuantity"] = limit
    resolved.update({k: offer[k] for k in CARD_FIELDS})
    resolved.update(cardName=card["title"], priceInfo={"eatInPrice": discounted, "separatePrice": regular},
        _configuration_base_cents=discounted, _regular_price_cents=regular, _verified_card_variant=True)
    return resolved, selected
