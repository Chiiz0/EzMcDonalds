"""Small, explicit views of live city, restaurant and pickup data."""
from datetime import datetime, timedelta
import re

from .errors import EzMcdError
from .models import SHANGHAI


def cities(raw, keyword=""):
    if not isinstance(raw, dict) or not isinstance(raw.get("groups"), list):
        raise EzMcdError("city_list_schema_changed")
    found = {}
    for group in raw["groups"]:
        for city in group.get("cities", []):
            code, name = str(city.get("code", "")), city.get("name")
            if re.fullmatch(r"[0-9]{6}", code) and isinstance(name, str):
                if keyword.casefold() in name.casefold() or keyword == code:
                    found[code] = {"city_code": code, "name": name}
    return list(found.values())


def resolve_city(raw, value):
    matches = cities(raw, value)
    exact = [c for c in matches if c["name"] == value or c["city_code"] == value]
    matches = exact or matches
    if len(matches) != 1:
        raise EzMcdError("city_not_unique_use_cities_list_and_select_code")
    return matches[0]["city_code"]


def restaurant(raw):
    code = str(raw.get("code", raw.get("storeCode", "")))
    name = raw.get("name", raw.get("storeName"))
    if not re.fullmatch(r"[0-9]{7}", code) or not isinstance(name, str):
        raise EzMcdError("store_list_schema_changed")
    status = raw.get("storeStatus") or {}
    can_order = status.get("canOrder", raw.get("onlineBusinessStatus"))
    distance = raw.get("distance")
    return {"store_code": code, "name": name, "address": raw.get("address", raw.get("storeAddress")),
        "city_code": raw.get("cityCode"), "city_name": raw.get("cityName"),
        "latitude": raw.get("latitude"), "longitude": raw.get("longitude"),
        "distance_meters": distance if type(distance) in (int, float) and distance >= 0 else None,
        "opening_time": raw.get("startTime"), "closing_time": raw.get("endTime"),
        "can_order_now": can_order if type(can_order) is bool else None,
        "reservation_supported": raw.get("isReservation") in (1, True),
        "services": [v for v in raw.get("hotTagCodes", []) if isinstance(v, str)],
        "pickup_slots_must_be_checked": True}


def store_results(raw, request, *, nearby=False):
    rows = raw.get("stores")
    if not isinstance(rows, list):
        raise EzMcdError("store_list_schema_changed")
    if nearby:
        if request.keyword:
            rows = [r for r in rows if request.keyword.casefold() in
                    (str(r.get("name", "")) + " " + str(r.get("address", ""))).casefold()]
        total = len(rows)
        start = (request.page - 1) * request.limit
        rows = rows[start:start + request.limit]
    else:
        total = raw.get("totalSize")
        if type(total) is not int or total < 0 or len(rows) > request.limit:
            raise EzMcdError("store_pagination_schema_changed")
    return {"stores": [restaurant(r) for r in rows], "page": request.page, "limit": request.limit,
        "total": total, "has_more": request.page * request.limit < total,
        "scope": "server_nearby_results" if nearby else "city_search",
        "fulfillment": "pickup", "server_time": raw.get("datetime")}


def pickup_choices(raw, store_code, *, now=None):
    interval = raw.get("timeInterval")
    if type(interval) is not int or not 1 <= interval <= 240 or not isinstance(raw.get("reservationOptions"), list):
        raise EzMcdError("pickup_slots_schema_changed")
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    dates, total = [], 0
    try:
        for day in raw["reservationOptions"]:
            periods = []
            for period in day.get("timeOptions", []):
                start = datetime.strptime(day["date"] + " " + period["timeStart"], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
                end = datetime.strptime(day["date"] + " " + period["timeEnd"], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
                if end < start:
                    raise ValueError("Unsupported overnight period")
                options = []
                point = start
                while point <= end:
                    total += 1
                    if total > 10000:
                        raise ValueError("Too many pickup slots")
                    if point > now:
                        options.append(point.isoformat())
                    point += timedelta(minutes=interval)
                if options:
                    periods.append({"daypart_code": str(period["dayPartCode"]), "name": period.get("dayPartName", ""),
                                    "pickup_times": options})
            if periods:
                dates.append({"date": day["date"], "label": day.get("dateText", day["date"]), "periods": periods})
    except (ValueError, TypeError, KeyError):
        raise EzMcdError("pickup_slots_schema_changed") from None
    return {"store_code": store_code, "timezone": "Asia/Shanghai", "interval_minutes": interval,
            "dates": dates, "availability_rechecked_at_checkout": True}
