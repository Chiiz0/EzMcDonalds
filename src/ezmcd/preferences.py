"""User-owned meal preferences and a read-only plan from live restaurant data."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import EzMcdError
from .models import CheckoutInput, ItemSelection, SHANGHAI, cents, fingerprint, yuan
from .storage import read_private, write_private

PREFERENCE_LOCK = fingerprint({"scope": "meal-preferences"})


class PreferredItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=120)
    quantity: int = Field(default=1, ge=1, le=20)
    component_names: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("component_names")
    @classmethod
    def components(cls, values):
        if len(set(values)) != len(values) or any(not v.strip() or len(v) > 120 for v in values):
            raise ValueError("Choose distinct, nonempty component names")
        return values


class MealPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    account: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,40}$")
    store_code: str = Field(pattern=r"^[0-9]{7}$")
    store_name: str = Field(default="", max_length=120)
    items: list[PreferredItem] = Field(min_length=1, max_length=8)
    pickup_time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    max_amount_cny: str
    require_card_discount: bool = False
    eat_type: Literal["locker-in"] = "locker-in"

    @field_validator("weekdays")
    @classmethod
    def days(cls, values):
        if len(set(values)) != len(values) or any(type(v) is not int or not 0 <= v <= 6 for v in values):
            raise ValueError("Weekdays use distinct integers, Monday=0 through Sunday=6")
        return sorted(values)

    @field_validator("max_amount_cny")
    @classmethod
    def maximum(cls, value):
        if not 0 < cents(value) <= 100000:
            raise ValueError("Positive limit no greater than 1000 CNY required")
        return yuan(cents(value))


def get_preferences(store):
    path = store.root / "preferences.json"
    preferences = MealPreferences.model_validate(read_private(path)).model_dump() if path.exists() else None
    return {"preferences": preferences, "scheduling_enabled": False, "spending_authorized_by_preferences": False}


def save_preferences(store, preferences):
    if preferences.account:
        store.account(preferences.account)
    with store.lock(PREFERENCE_LOCK):
        write_private(store.root / "preferences.json", preferences.model_dump())
    return get_preferences(store)


def choose_components(detail, names):
    groups, used, issues, extra = [], set(), [], 0
    for group in detail["groups"]:
        all_choices = group["choices"]
        wanted = [c for c in all_choices if c["name"] in names]
        candidates = [c for c in all_choices if c["available"] and c["standard_configuration_supported"]]
        if wanted:
            chosen = wanted
            used.update(c["name"] for c in chosen)
        elif len(all_choices) == 1 and group["min_quantity"] == group["max_quantity"] == 1:
            chosen = all_choices
        elif group["min_quantity"] == 0:
            chosen = []
        else:
            issues.append({"reason": "component_choice_required", "round": group["round"],
                           "group": group["name"], "choices": all_choices})
            continue
        if (not group["min_quantity"] <= len(chosen) <= group["max_quantity"]
                or any(c not in candidates for c in chosen)
                or len({c["product_code"] for c in chosen}) != len(chosen)):
            issues.append({"reason": "preferred_component_unavailable_or_unsupported", "round": group["round"],
                           "choices": all_choices})
            continue
        groups.append({"round": group["round"], "items": [{"product_code": c["product_code"], "quantity": 1} for c in chosen]})
        if extra is not None:
            extra = extra + sum(cents(c["extra_cny"]) for c in chosen) if all(c["extra_cny"] is not None for c in chosen) else None
    missing = set(names) - used
    if missing:
        issues.append({"reason": "preferred_components_not_found", "names": sorted(missing)})
    return groups, extra, issues


def plan_meal(service, account=None, date=None):
    saved = get_preferences(service.store)["preferences"]
    if not saved:
        raise EzMcdError("meal_preferences_missing_save_preferences_first")
    preferences = MealPreferences.model_validate(saved)
    alias = account or preferences.account
    if not alias:
        raise EzMcdError("select_account_for_meal_plan")
    if date is not None:
        try:
            parsed = datetime.strptime(date, "%Y-%m-%d").date()
            if parsed.isoformat() != date:
                raise ValueError
        except ValueError:
            raise EzMcdError("meal_plan_date_requires_yyyy_mm_dd") from None
    record, provider = service.provider(alias)
    slots = provider.pickup_slots(preferences.store_code)
    possible = []
    for day in slots["dates"]:
        if date and day["date"] != date:
            continue
        for period in day["periods"]:
            for value in period["pickup_times"]:
                when = datetime.fromisoformat(value).astimezone(SHANGHAI)
                if when.strftime("%H:%M") == preferences.pickup_time and when.weekday() in preferences.weekdays:
                    possible.append(value)
    result = {"account": alias, "store_code": preferences.store_code, "simulation": record["provider"] == "demo",
              "cart_changed": False, "order_created": False, "payment_attempted": False,
              "selection_ready": False, "quote_required": True, "scheduling_enabled": False}
    if not possible:
        return result | {"issues": [{"reason": "preferred_pickup_time_unavailable", "available_slots": slots}]}
    request = CheckoutInput(account=alias, store_code=preferences.store_code, pickup_at=min(possible),
                            max_amount_cny=preferences.max_amount_cny, eat_type=preferences.eat_type)
    menu = provider.menu(request)["products"]
    selections, descriptions, issues, estimate = [], [], [], 0
    for preference in preferences.items:
        offers = [p for p in menu if p["name"] == preference.name and str(p.get("product_type")) in ("1", "7")
                  and (not preferences.require_card_discount or p.get("card_name"))]
        # Never choose between distinct products merely because their names match.
        if not offers or len({p["product_code"] for p in offers}) != 1:
            issues.append({"reason": "preferred_product_unavailable_or_ambiguous", "name": preference.name, "offers": offers})
            continue
        if len(offers) > 1 and any(p.get("display_price_cny") is None for p in offers):
            issues.append({"reason": "offer_price_unknown_choose_explicitly", "name": preference.name, "offers": offers})
            continue
        offer = min(offers, key=lambda p: (cents(p["display_price_cny"]) if p.get("display_price_cny") else 0, p["offer_id"]))
        detail = provider.product_detail(request, offer["product_code"], offer["offer_id"])
        groups, extra, problems = choose_components(detail, preference.component_names)
        if problems or type(detail.get("max_quantity")) is not int or preference.quantity > detail["max_quantity"]:
            issues.append({"reason": "product_configuration_required", "name": preference.name,
                           "details": problems, "max_quantity": detail.get("max_quantity")})
            continue
        selection = ItemSelection(product_code=offer["product_code"], offer_id=offer["offer_id"],
                                  quantity=preference.quantity, combo=groups)
        selections.append(selection.model_dump())
        descriptions.append({"name": offer["name"], "card_name": offer.get("card_name"),
                             "quantity": preference.quantity, "components": preference.component_names})
        if estimate is not None:
            estimate = estimate + (cents(offer["display_price_cny"]) + extra) * preference.quantity if offer.get("display_price_cny") is not None and extra is not None else None
    if estimate is not None and estimate > cents(preferences.max_amount_cny):
        issues.append({"reason": "estimated_price_exceeds_preference_limit"})
    key = "meal-" + fingerprint({"account": alias, "store_code": request.store_code, "pickup_at": request.pickup_at})[:32]
    return result | {"request": request.model_dump(), "items": selections if not issues else [],
        "products": descriptions, "selection_ready": not issues and bool(selections), "issues": issues,
        "estimated_amount_cny": yuan(estimate) if estimate is not None and not issues else None,
        "estimated_price_is_final_quote": False, "idempotency_key": key,
        "next_step": "cart_get_then_cart_add_configured_if_empty_then_checkout_quote" if not issues else "resolve_choices_before_cart_changes"}
