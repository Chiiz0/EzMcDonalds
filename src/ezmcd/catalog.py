"""Normalize live menus and validate checkout without a fixed store or meal."""
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit

from .errors import EzMcdError
from .models import cents, fingerprint, pickup_time, yuan


def menu_products(menu):
    result = []
    def walk(categories, parents=()):
        for category in categories:
            names = (*parents, category.get("categoryName", ""))
            for product in category.get("productList", []):
                item = deepcopy(product)
                item["offer_id"] = fingerprint({k: item.get(k) for k in
                    ("productCode", "cardId", "couponId", "promotionId", "productType")})[:24]
                item["category_names"] = list(names)
                result.append(item)
            walk(category.get("categories", []), names)
    walk(menu.get("menu", []))
    return result


def public_menu(menu, keyword=""):
    result, seen = [], set()
    for item in menu_products(menu):
        if item["offer_id"] in seen or keyword.casefold() not in item.get("productName", "").casefold():
            continue
        seen.add(item["offer_id"])
        price = item.get("priceInfo", {}).get("eatInPrice")
        picture = item.get("productImage", "")
        parsed = urlsplit(picture) if isinstance(picture, str) else None
        safe_picture = picture if parsed and parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".mcd.cn") else None
        result.append({"product_code": item["productCode"], "name": item.get("productName"),
            "offer_id": item["offer_id"], "product_type": item.get("productType"),
            "categories": item["category_names"], "description": item.get("memo", ""),
            "display_price_cny": yuan(price) if type(price) is int else None,
            "image_url": safe_picture, "max_quantity": item.get("maxPurchaseQuantity"),
            "availability_requires_detail_check": True,
            "card_name": item.get("cardName"), "requires_configuration": bool(item.get("isChoices") or item.get("needDetail") or str(item.get("productType")) != "1"),
            "display_price_is_final_quote": False})
    return result


def menu_categories(menu):
    result = []
    def walk(rows, parents=()):
        for row in rows:
            names = (*parents, row.get("categoryName", ""))
            result.append({"category_code": str(row.get("categoryCode", "")), "names": list(names),
                           "product_count": len(row.get("productList", []))})
            walk(row.get("categories", []), names)
    walk(menu.get("menu", []))
    return result


def membership_assets(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("cards"), list):
        raise EzMcdError("membership_assets_schema_changed")
    # Never return card numbers, credential-bearing jump URLs, or opaque coupon IDs.
    return {"cards": [{"name": c.get("title"), "validity": c.get("validPeriodText"),
        "valid_from": c.get("redeemStartTime"), "valid_until": c.get("redeemEndTime"),
        "days_left": c.get("leftDays"), "reported_count": c.get("count")} for c in raw["cards"]],
        "coupon_categories": [{"name": c.get("name"), "count": c.get("count"), "description": c.get("text")}
                              for c in raw.get("couponCategories", [])],
        "applicability_requires_live_menu_and_quote": True}


def reservation_daypart(reservation, pickup):
    interval = reservation.get("timeInterval")
    if type(interval) is not int or interval <= 0:
        raise EzMcdError("invalid_reservation_interval")
    for day in reservation.get("reservationOptions", []):
        if day.get("date") != pickup.date().isoformat():
            continue
        for slot in day.get("timeOptions", []):
            start = datetime.strptime(slot["timeStart"], "%H:%M").time()
            end = datetime.strptime(slot["timeEnd"], "%H:%M").time()
            selected = pickup.time().replace(tzinfo=None)
            minutes = selected.hour * 60 + selected.minute - start.hour * 60 - start.minute
            if start <= selected <= end and minutes % interval == 0:
                return str(slot["dayPartCode"])
    raise EzMcdError("pickup_slot_unavailable")


def component_spec(product):
    return [{k: deepcopy(group[k]) for k in ("round", "isChoices", "choicesCode") if k in group}
        | {"comboProducts": [{k: deepcopy(item[k]) for k in ("code", "name", "quantity", "customization",
            "modification", "grills", "biasChoices") if k in item} for item in group.get("comboProducts", [])]}
        for group in product.get("comboItems", [])]


def cart_items(cart):
    return [{"product_code": str(p["code"]), "name": p["name"], "quantity": p["quantity"],
             "product_type": str(p["type"]), "components": component_spec(p),
             "configuration": {k: deepcopy(p[k]) for k in ("customization", "modification", "grills", "biasChoices") if k in p},
             "amount_cents": p["realSubTotalPrice"]} for p in cart.get("products", [])]


def checkout(args, cart, confirmation, daypart):
    pickup = pickup_time(args.pickup_at)
    if (str(cart.get("storeCode")) != args.store_code or cart.get("beType") != 1
            or cart.get("orderType") != 1 or cart.get("submit") != 1
            or str(cart.get("daypartCode")) != daypart):
        raise EzMcdError("cart_store_fulfillment_or_daypart_mismatch")
    products = cart.get("products", [])
    if not 1 <= len(products) <= 20 or any(p.get("saleStatus") != 1 for p in products):
        raise EzMcdError("cart_empty_or_unavailable")
    if any(type(p.get("quantity")) is not int or not 1 <= p["quantity"] <= 20 or str(p.get("type")) not in ("1", "7") for p in products):
        raise EzMcdError("product_type_or_quantity_not_supported")
    if (confirmation.get("submit") != 1 or str(confirmation.get("orderType")) != "1"
            or str(confirmation.get("storeInfo", {}).get("storeCode")) != args.store_code
            or str(confirmation.get("dayPart", {}).get("dayPartCode")) != daypart):
        raise EzMcdError("confirmation_store_or_fulfillment_mismatch")
    selected = confirmation.get("storePickUpInfo", {})
    choices = [x for x in selected.get("eatTypeOptions", []) if x.get("selected") is True]
    if (selected.get("pickupExpectTimeValue") != pickup.strftime("%Y-%m-%d %H:%M")
            or len(choices) != 1 or choices[0].get("code") != args.eat_type):
        raise EzMcdError("confirmation_pickup_time_or_method_mismatch")
    price = confirmation["productPriceInfo"]
    amount = cents(price["realTotalAmount"])
    regular, discount = cents(price["totalAmount"]), cents(price["discountAmount"])
    if (not 0 < amount <= cents(args.max_amount_cny) or regular - discount != amount
            or cart.get("realTotalPrice") != amount or cart.get("totalPrice") != regular
            or cart.get("discountAmount") != discount or cart.get("realDeliveryPrice", 0) != 0):
        raise EzMcdError("price_changed_or_exceeds_limit")
    rows = price.get("cartProductList", [])
    by_sequence = {str(p["sequence"]): p for p in products}
    if len(rows) != len(products) or len(by_sequence) != len(products):
        raise EzMcdError("confirmation_cart_size_mismatch")
    order_rows, labels = [], []
    seen = set()
    for row in rows:
        seq = str(row["sequence"])
        p = by_sequence.get(seq)
        if (seq in seen or not p or str(row.get("productCode")) != str(p["code"])
                or row.get("quantity") != p["quantity"] or row.get("productName") != p["name"]
                or str(row.get("productType")) != str(p["type"]) or row.get("saleStatus") != 1
                or not row.get("uniqueKey") or cents(row.get("realSubtotal")) != p["realSubTotalPrice"]
                or cents(row.get("subtotal")) != p["subTotalPrice"]):
            raise EzMcdError("confirmation_product_mismatch")
        seen.add(seq)
        expected_components = sorted((str(i["code"]), i["quantity"]) for g in p.get("comboItems", []) for i in g.get("comboProducts", []))
        actual_components = sorted((str(i["productCode"]), i["quantity"]) for i in row.get("comboItemList", []))
        if expected_components != actual_components:
            raise EzMcdError("confirmation_components_mismatch")
        coupons = row.get("couponList", [])
        if p.get("coupon"):
            coupon = p["coupon"]
            if len(coupons) != 1 or any(coupons[0].get(a) != coupon.get(b) for a, b in (
                    ("couponId", "id"), ("couponCode", "code"), ("promotionId", "promotionId"), ("cardId", "rightCardId"))):
                raise EzMcdError("confirmation_coupon_mismatch")
        elif coupons:
            raise EzMcdError("unexpected_coupon_requires_review")
        labels.extend(c.get("couponName", "") for c in coupons)
        output = {k: deepcopy(row[k]) for k in ("productImage", "realSubtotal", "productCode", "uniqueKey", "productType", "quantity", "productName", "sequence")}
        output.update(productPrice=row["price"], changeQuantity=0, status=0,
            couponList=[{k: deepcopy(c[k]) for k in ("couponCode", "couponName", "couponId", "image", "promotionId", "showPmtType") if k in c} for c in coupons])
        order_rows.append(output)
    if sum(p["realSubTotalPrice"] for p in products) != amount:
        raise EzMcdError("unmodeled_order_level_discount_or_fee")
    tableware = [x for x in confirmation.get("tablewareInfo", {}).get("options", []) if x.get("selected") is True]
    if len(tableware) != 1:
        raise EzMcdError("tableware_selection_required")
    body = {"realTotalAmount": price["realTotalAmount"], "uniCode": "", "vehicleTypeCode": "",
        "eatTypeCode": args.eat_type, "date": pickup.date().isoformat(), "beCode": "", "skipPriceChange": False,
        "storeCode": args.store_code, "cartItems": order_rows, "dayPartCode": daypart, "licensePlate": "",
        "tablewareCode": tableware[0]["code"], "beType": "1", "driveDuration": -1, "time": pickup.strftime("%H:%M"),
        "orderType": "1", "vehicleColorCode": "", "autoMatch": True, "addressId": ""}
    items = cart_items(cart)
    intent = fingerprint({"store": args.store_code, "pickup": args.pickup_at, "eat_type": args.eat_type,
        "items": [{k: v for k, v in item.items() if k != "amount_cents"} for item in items]})
    return {"store_code": args.store_code, "store_name": confirmation["storeInfo"].get("storeName"),
        "pickup_at": args.pickup_at, "eat_type": args.eat_type, "items": items,
        "amount_cents": amount, "regular_cents": regular, "discount_cents": discount, "discount_names": labels,
        "body": body, "intent": intent, "checkout_digest": fingerprint(body), "daypart": daypart}
