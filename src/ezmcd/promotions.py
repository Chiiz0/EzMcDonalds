"""Distinguish an observed unavailable related offer from this item's pricing.

This does not redeem a promotion or infer eligibility. Only a different target
with the observed membership metadata and matching ordinary prices is allowed.
Self-targeted, eligible, discounted and unknown promotion shapes still stop.
"""


def ordinary_offer_with_unavailable_related_promotion(offer, detail):
    if (not isinstance(detail, dict) or any(offer.get(k) for k in
            ("cardId", "couponId", "couponCode", "promotionId", "_coupon_source"))):
        return False
    rows = offer.get("productPromotions")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return False
    row = rows[0]
    if set(row) != {"productCode", "promotionInfo"}:
        return False
    target = row["productCode"]
    if not isinstance(target, str) or not target or target == str(offer.get("productCode")):
        return False
    info = row["promotionInfo"]
    if not isinstance(info, dict) or set(info) != {
            "hasRights", "membershipCode", "pmtType", "promotionId", "promotionLimitNum", "specId", "withOrder"}:
        return False
    if (info["hasRights"] is not False or info["withOrder"] is not True
            or type(info["pmtType"]) is not int or info["pmtType"] != 3 or info["membershipCode"] != "106"
            or type(info["promotionLimitNum"]) is not int or info["promotionLimitNum"] <= 0
            or any(not isinstance(info[k], str) or not info[k] for k in ("promotionId", "specId"))):
        return False
    product = detail.get("product", {})
    prices = offer.get("priceInfo", {})
    if not isinstance(product, dict) or not isinstance(prices, dict):
        return False
    price = prices.get("eatInPrice")
    return bool(str(offer.get("productType")) == str(product.get("type")) == "7"
        and str(product.get("code")) == str(offer.get("productCode"))
        and type(price) is int and price > 0
        and type(product.get("price")) is int and product["price"] == price
        and type(product.get("startingPrice")) is int and product["startingPrice"] == price)
