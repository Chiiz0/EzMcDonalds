"""Resolve user-owned coupons from fresh store/time queries, without exposing IDs."""
from copy import deepcopy
import re

from .errors import EzMcdError
from .models import fingerprint, yuan


def coupon_signature(coupon):
    """Private identity comparison also used for a cart's coupon echo."""
    return fingerprint({k: str(coupon.get(k) or "") for k in ("id", "code", "promotionId", "rightCardId")})


def coupon_rows(raw, scope):
    try:
        tabs = raw["couponTabList"]
        if not isinstance(tabs, list) or len(tabs) > 30:
            raise ValueError
        rows, evidence = {}, {}
        for tab in tabs:
            if not isinstance(tab["coupons"], list):
                raise ValueError
            for group in tab["coupons"]:
                if not isinstance(group["coupons"], list):
                    raise ValueError
                for source in group["coupons"]:
                    if not all(isinstance(source.get(k), str) and 0 < len(source[k]) <= 200 for k in ("id", "code", "promotionId", "title")):
                        raise ValueError
                    ref = "coupon-" + fingerprint({"scope": scope,
                        "identity": coupon_signature(source | {"rightCardId": source.get("cardId")})})[:24]
                    if ref not in rows:
                        rows[ref] = deepcopy(source) | {"coupon_ref": ref, "categories": []}
                        evidence[ref] = fingerprint(source)
                    elif evidence[ref] != fingerprint(source):
                        raise ValueError
                    if tab["tabName"] not in rows[ref]["categories"]:
                        rows[ref]["categories"].append(tab["tabName"])
                    if len(rows) > 500:
                        raise ValueError
        return list(rows.values())
    except (AttributeError, KeyError, TypeError, ValueError):
        raise EzMcdError("coupon_catalog_schema_changed") from None


def availability_issues(coupon):
    issues = []
    if type(coupon.get("enable")) is not int or coupon["enable"] != 1:
        issues.append("coupon_not_enabled_for_request")
    if type(coupon.get("useStatus")) is not int or coupon["useStatus"] != 1:
        issues.append("coupon_not_usable_for_request")
    if coupon.get("currentChannelCanRedeem") is not True or coupon.get("onlyPosCanRedeem") is not False:
        issues.append("coupon_channel_not_eligible")
    for field in ("currentDayAvailableCount", "totalAvailableCount"):
        if type(coupon.get(field)) is not int or coupon[field] <= 0:
            issues.append("coupon_remaining_uses_unavailable")
            break
    return issues


def configuration_issues(coupon):
    if coupon.get("cardId") or coupon.get("couponCardType") == 2:
        return ["use_live_menu_card_offer"]
    if (type(coupon.get("couponCardType")) is not int or coupon["couponCardType"] != 0
            or type(coupon.get("couponPromotionType")) is not int or coupon["couponPromotionType"] != 1
            or type(coupon.get("isPrepaid")) is not int or coupon["isPrepaid"] != 0
            or type(coupon.get("paymentType")) is not int or coupon["paymentType"] != 0
            or coupon.get("multipleProducts") is not False or coupon.get("conditionalProductCodes") != []):
        return ["coupon_requires_official_configuration"]
    return []


def public_coupon(coupon):
    unavailable = availability_issues(coupon)
    configuration = configuration_issues(coupon)
    discount = coupon.get("discountInfo") or {}
    instructions = coupon.get("instructions") or {}
    return {"coupon_ref": coupon["coupon_ref"], "name": coupon["title"], "subtitle": coupon.get("subtitle", ""),
        "categories": coupon["categories"], "validity_text": coupon.get("datetimeText"),
        "discount_display": {k: discount[k] for k in ("discountDesc", "discountTypeText", "discountValue", "separatePriceText") if k in discount},
        "remaining_today": coupon.get("currentDayAvailableCount"), "remaining_total": coupon.get("totalAvailableCount"),
        "available_for_request": not unavailable, "availability_issues": unavailable,
        "configuration_path": "menu_card_offer" if configuration == ["use_live_menu_card_offer"] else "official_app" if configuration else "coupon_products",
        "configuration_issues": configuration, "reservation_rule_requires_quote": True,
        "rules": [r["text"] for key in ("availableTime", "labels") for r in instructions.get(key, [])
                  if isinstance(r, dict) and isinstance(r.get("text"), str)],
        "final_eligibility_requires_quote": True}


def choose_coupon(rows, ref):
    if not isinstance(ref, str) or not re.fullmatch(r"coupon-[0-9a-f]{24}", ref):
        raise EzMcdError("select_coupon_from_current_store_time_list")
    matches = [c for c in rows if c["coupon_ref"] == ref]
    if len(matches) != 1:
        raise EzMcdError("coupon_not_found_for_account_store_time")
    if availability_issues(matches[0]):
        raise EzMcdError("coupon_unavailable_for_selected_pickup")
    return matches[0]


def coupon_offers(coupon, raw):
    """Only the observed one-product coupon shape can enter configured cart flow."""
    if not isinstance(raw, dict) or not isinstance(raw.get("productList"), list) or len(raw["productList"]) > 200:
        raise EzMcdError("coupon_products_schema_changed")
    issues = configuration_issues(coupon)
    if (raw.get("multipleProducts") is not False or type(raw.get("isPrepaid")) is not int or raw["isPrepaid"] != 0
            or type(raw.get("maxPurchaseQuantity")) is not int or raw["maxPurchaseQuantity"] <= 0):
        issues = issues + ["coupon_product_bundle_requires_official_configuration"]
    offers, seen = [], set()
    for source in raw["productList"]:
        code = source.get("productCode")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{1,20}", code) or code in seen:
            raise EzMcdError("coupon_products_schema_changed")
        seen.add(code)
        problems = list(issues)
        if type(source.get("count")) is not int or source["count"] != 1 or str(source.get("productType")) not in ("1", "7"):
            problems.append("coupon_product_configuration_not_supported")
        if type(source.get("saleStatus")) is not int or source["saleStatus"] != 1:
            problems.append("coupon_product_not_available")
        amount = source.get("priceInfo", {}).get("discountPrice")
        if type(amount) is not int or amount <= 0:
            problems.append("coupon_product_price_unknown_or_zero")
        # A zero per-product maximum is returned by the official coupon query.
        # Do not treat it as a usable limit: live product detail is checked again.
        limit = min(coupon["currentDayAvailableCount"], coupon["totalAvailableCount"], raw["maxPurchaseQuantity"]) if not issues else 0
        if type(source.get("maxPurchaseQuantity")) is int and source["maxPurchaseQuantity"] > 0:
            limit = min(limit, source["maxPurchaseQuantity"])
        offer = deepcopy(source) | {"offer_id": fingerprint({"coupon": coupon["coupon_ref"], "product": code})[:24],
            "coupon_ref": coupon["coupon_ref"], "couponId": coupon["id"], "couponCode": coupon["code"],
            "promotionId": coupon["promotionId"], "coupon_name": coupon["title"], "_coupon_source": True,
            "_coupon_max_quantity": limit, "_coupon_configuration_issues": problems}
        offers.append(offer)
    return offers


def public_coupon_products(coupon, offers):
    return {"coupon": public_coupon(coupon), "products": [{
        "product_code": p["productCode"], "name": p.get("productName"), "product_type": p.get("productType"),
        "coupon_ref": p["coupon_ref"], "offer_id": p["offer_id"],
        "display_price_cny": yuan(p["priceInfo"]["discountPrice"]) if type(p.get("priceInfo", {}).get("discountPrice")) is int and p["priceInfo"]["discountPrice"] >= 0 else None,
        "cart_configuration_supported": not p["_coupon_configuration_issues"],
        "requires_product_detail": True,
        "configuration_issues": p["_coupon_configuration_issues"], "max_quantity_before_detail_check": p["_coupon_max_quantity"],
        "next_step": "product_detail_with_coupon_ref" if not p["_coupon_configuration_issues"] else "use_live_menu_card_offer_or_official_app"
    } for p in offers], "display_price_is_final_quote": False, "cart_changed": False, "order_created": False}


def coupon_fields(offer, quantity):
    if offer.get("_coupon_configuration_issues") or not 1 <= quantity <= offer.get("_coupon_max_quantity", 0):
        raise EzMcdError("coupon_configuration_or_quantity_requires_review")
    return {k: offer[k] for k in ("couponId", "couponCode", "promotionId")}


def verify_coupon_cart(expected, rows):
    if not expected.get("couponId") or expected.get("useRightCard"):
        return
    wanted = coupon_signature({"id": expected["couponId"], "code": expected["couponCode"], "promotionId": expected["promotionId"]})
    matches = [r for r in rows if r["product_code"] == expected["code"] and r.get("coupon_fingerprint") == wanted]
    if len(matches) != 1 or matches[0]["quantity"] != expected["quantity"]:
        raise EzMcdError("cart_coupon_not_confirmed_review_cart")
