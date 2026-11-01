"""Allowlisted views of the member's official order history; no payment data."""
from datetime import datetime
import re

from .errors import EzMcdError
from .models import SHANGHAI, cents, yuan


def order_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise EzMcdError("invalid_official_order_id")
    return value


def pagination(page, limit):
    if type(page) is not int or not 1 <= page <= 100 or type(limit) is not int or not 1 <= limit <= 20:
        raise EzMcdError("invalid_official_order_pagination")


def text(value, maximum=200):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError
    return value


def money(value):
    if value in (None, ""):
        return None
    result = cents(value)
    if not 0 <= result <= 100000000:
        raise ValueError
    return yuan(result)


def moment(value):
    if value in (None, ""):
        return None
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    if parsed.strftime("%Y-%m-%d %H:%M:%S") != value:
        raise ValueError
    return parsed.replace(tzinfo=SHANGHAI).isoformat()


def products(rows, depth=0):
    if depth > 3 or not isinstance(rows, list) or len(rows) > 100:
        raise ValueError
    result = []
    for row in rows:
        quantity = row["quantity"]
        if type(quantity) is not int or not 1 <= quantity <= 1000:
            raise ValueError
        result.append({"product_code": text(row.get("productCode"), 40),
            "name": text(row.get("productName") or row.get("name")), "quantity": quantity,
            "amount_cny": money(row.get("realSubtotal")),
            "components": products(row.get("comboItemList", []), depth + 1) if "comboItemList" in row else []})
    return result


def public_order(raw, *, detail=False):
    try:
        result = {"order_id": order_id(raw["orderId"]), "store_code": text(raw.get("storeCode"), 20),
            "store_name": text(raw.get("storeName")), "order_type": text(raw.get("orderTypeName")),
            "status_title": text(raw.get("orderStatusTitle") or raw.get("orderStatus")),
            "amount_cny": money(raw["realTotalAmount"]), "created_at": moment(raw.get("createTime")),
            "pickup_at": moment(raw.get("expectPickUpTime")), "items": products(raw["orderProductList"])}
        if result["amount_cny"] is None:
            raise ValueError
        if detail:
            coupons = raw.get("couponList", [])
            if not isinstance(coupons, list) or len(coupons) > 100:
                raise ValueError
            result.update(store_address=text(raw.get("storeAddress"), 500),
                pickup_code=text(raw.get("pickupCode"), 80), paid_amount_cny=money(raw.get("realPayAmount")),
                payment_channel=text(raw.get("paymentChannel"), 40), discount_cny=money(raw.get("totalDiscountAmount")),
                discount_names=[name for c in coupons if (name := text(c.get("couponName")))],
                eat_type=("locker-in" if raw.get("lockerOrder") is True else "take-in-store")
                    if str(raw.get("orderType")) == "1" and str(raw.get("beType")) == "1" and type(raw.get("eatType")) is int
                    and raw["eatType"] == 2 and type(raw.get("lockerOrder")) is bool and raw.get("dtStatus") in (None, "") else None)
        return result
    except (AttributeError, KeyError, TypeError, ValueError, RecursionError, EzMcdError):
        raise EzMcdError("official_order_response_schema_changed") from None


def public_orders(raw, page, limit):
    pagination(page, limit)
    rows = raw.get("list") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or len(rows) > limit:
        raise EzMcdError("official_order_list_schema_changed")
    orders = [public_order(row) for row in rows]
    if len({r["order_id"] for r in orders}) != len(orders):
        raise EzMcdError("official_order_list_identity_duplicated")
    return {"orders": orders, "page": page, "limit": limit,
        "next_page": page + 1 if len(rows) == limit and page < 100 else None,
        "page_may_have_more": len(rows) == limit, "complete_history": False,
        "pagination_limit_reached": page == 100 and len(rows) == limit}
