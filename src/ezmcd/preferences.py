"""User-owned meal preferences and a read-only plan from live restaurant data."""
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import EzMcdError
from .models import CheckoutInput, ItemSelection, SHANGHAI, cents, fingerprint, yuan
from .storage import read_private, write_private

PREFERENCE_LOCK = fingerprint({"scope": "meal-preferences"})


class PreferredModifier(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=120)
    quantity: int = Field(ge=0, le=1)
    group_name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Choose an ingredient name from product_detail")
        return value


class PreferredComponentModifiers(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    component_name: str = Field(min_length=1, max_length=120)
    modifiers: list[PreferredModifier] = Field(min_length=1, max_length=20)

    @field_validator("modifiers")
    @classmethod
    def distinct_modifiers(cls, values):
        if len({(v.group_name, v.name) for v in values}) != len(values):
            raise ValueError("Choose each ingredient only once")
        return values


class PreferredItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=120)
    variant_of: str | None = Field(default=None, min_length=1, max_length=120)
    variant_specifications: dict[str, str] | None = Field(default=None, min_length=1, max_length=6,
                                                        exclude_if=lambda value: value is None)
    coupon_name: str | None = Field(default=None, min_length=1, max_length=120)
    quantity: int = Field(default=1, ge=1, le=20)
    component_names: list[str] = Field(default_factory=list, max_length=20)
    modifiers: list[PreferredModifier] = Field(default_factory=list, max_length=20)
    component_modifiers: list[PreferredComponentModifiers] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def variant_labels(self):
        if self.variant_specifications is not None and (not self.variant_of or any(
                not text.strip() or len(text) > 120 for pair in self.variant_specifications.items() for text in pair)):
            raise ValueError("Variant specification names require a parent and nonempty names from product_detail")
        return self

    @field_validator("modifiers")
    @classmethod
    def distinct_modifiers(cls, values):
        return PreferredComponentModifiers.distinct_modifiers(values)

    @field_validator("component_modifiers")
    @classmethod
    def distinct_targets(cls, values):
        if len({v.component_name for v in values}) != len(values) or any(not v.component_name.strip() for v in values):
            raise ValueError("Choose distinct, nonempty component names")
        return values

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
    eat_type: Literal["locker-in", "take-in-store"] = "locker-in"

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


def resolve_modifiers(public, preferences):
    """Resolve exact ingredient names against fresh choices, filling other defaults.

    Saved codes can change between restaurants and product revisions. A missing
    or ambiguous requested name blocks the plan instead of dropping that choice.
    """
    if not preferences:
        return None, []
    if not public or not public.get("supported"):
        return None, [{"reason": "preferred_ingredient_configuration_unsupported"}]
    groups = public["groups"]
    choices = [(i, j, c) for i, g in enumerate(groups) for j, c in enumerate(g["choices"])]
    quantities = {(i, j): c["default_quantity"] for i, j, c in choices}
    issues, changed_groups = [], set()
    for preference in preferences:
        matches = [(i, j, c) for i, j, c in choices if c["name"] == preference.name
                   and (preference.group_name is None or groups[i].get("name") == preference.group_name)]
        if len(matches) != 1:
            issues.append({"reason": "preferred_ingredient_missing_or_ambiguous", "name": preference.name})
            continue
        i, j, choice = matches[0]
        if groups[i].get("inactive") or (not choice.get("available", True)
                and not (groups[i].get("kind") == "optional_choice" and preference.quantity == 0)):
            issues.append({"reason": "preferred_ingredient_unavailable", "name": preference.name})
            continue
        if groups[i].get("kind") == "single_choice":
            if preference.quantity != 1 or i in changed_groups:
                issues.append({"reason": "select_one_preferred_option_per_group", "group": groups[i].get("name")})
                continue
            changed_groups.add(i)
            for k in range(len(groups[i]["choices"])):
                quantities[i, k] = 0
        quantities[i, j] = preference.quantity
    selected = []
    for i, group in enumerate(groups):
        if group.get("inactive"):
            continue
        if not group["min_quantity"] <= sum(quantities[i, j] for j in range(len(group["choices"]))) <= group["max_quantity"]:
            issues.append({"reason": "preferred_ingredients_outside_live_limits"})
        for j, choice in enumerate(group["choices"]):
            quantity = quantities[i, j]
            if quantity and (not choice.get("available", True) or not choice.get("selection_supported", True)
                    or cents(choice.get("extra_cny", "0.00")) != 0 and group.get("kind") != "optional_choice"):
                issues.append({"reason": "preferred_ingredient_unavailable_or_paid", "name": choice["name"]})
            if group.get("kind") == "single_choice":
                if quantity:
                    selected.append({"code": choice["code"], "quantity": 1, "key": choice["key"]})
            else:
                selected.append({"code": choice["code"], "quantity": quantity})
    return (selected if not issues else None), issues


def apply_preferred_modifiers(detail, preference, groups):
    problems = []
    if groups and preference.modifiers:
        modifiers = None
        problems.append({"reason": "use_component_modifiers_for_combo_ingredients"})
    else:
        modifiers, problems = resolve_modifiers(detail.get("modifiers"), preference.modifiers)
    chosen = [(selection, choice) for group in groups for selection in group["items"]
              for source in detail["groups"] if source["round"] == group["round"]
              for choice in source["choices"] if choice["product_code"] == selection["product_code"]]
    for target in preference.component_modifiers:
        matches = [(selection, choice) for selection, choice in chosen if choice["name"] == target.component_name]
        if len(matches) != 1:
            problems.append({"reason": "preferred_ingredient_component_not_selected_or_ambiguous", "name": target.component_name})
            continue
        selection, choice = matches[0]
        selection["modifiers"], issues = resolve_modifiers(choice.get("modifiers"), target.modifiers)
        problems.extend(issue | {"component": target.component_name} for issue in issues)
    return modifiers, problems


def selected_modifier_extra(public, selections):
    """Add only explicitly selected optional fees; defaults are validated free."""
    if not selections or not public:
        return 0
    chosen = {s["code"]: s["quantity"] for s in selections}
    return sum(cents(c["extra_cny"]) * chosen.get(c["code"], 0)
               for group in public.get("groups", []) if group.get("kind") == "optional_choice"
               for c in group["choices"])


def configuration_extra(detail, modifiers, groups):
    extra = selected_modifier_extra(detail.get("modifiers"), modifiers)
    for group in groups:
        for selection in group["items"]:
            choices = [c for source in detail["groups"] if source["round"] == group["round"]
                       for c in source["choices"] if c["product_code"] == selection["product_code"]]
            if len(choices) != 1:
                raise EzMcdError("component_not_available_in_selected_round")
            extra += selection["quantity"] * selected_modifier_extra(choices[0].get("modifiers"), selection.get("modifiers"))
    return extra


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
    record = service.store.account(alias)
    result = {"account": alias, "store_code": preferences.store_code, "simulation": record["provider"] == "demo",
              "cart_changed": False, "order_created": False, "payment_attempted": False,
              "selection_ready": False, "quote_required": True, "scheduling_enabled": False,
              "existing_purchase": False, "existing_orders": []}

    def existing_meal(pickup_at):
        orders = service.orders_at_pickup(alias, preferences.store_code, pickup_at)
        if not orders:
            return None
        return result | {"pickup_at": pickup_at, "items": [], "products": [], "quote_required": False,
            "existing_purchase": True, "existing_orders": orders, "live_status_checked": False,
            "source": "local_project_journal", "includes_official_app_orders": False,
            "issues": [{"reason": "existing_purchase_for_preferred_pickup"}],
            "next_step": "review_existing_purchase", "automatic_write_retry": False}

    # Recover the intended meal before any session/reservation request. An
    # explicit past date can find its purchase. Without a date, consider only
    # the next future preferred weekday/time, not a previous breakfast.
    now = datetime.now(SHANGHAI)
    if date is not None:
        candidate = datetime.fromisoformat(date + "T" + preferences.pickup_time + ":00+08:00")
    else:
        candidate = None
        for offset in range(8):
            day = now.date() + timedelta(days=offset)
            when = datetime.fromisoformat(day.isoformat() + "T" + preferences.pickup_time + ":00+08:00")
            if when > now and when.weekday() in preferences.weekdays:
                candidate = when
                break
    if candidate is not None and (existing := existing_meal(candidate.isoformat())):
        return existing

    _, provider = service.provider(alias)
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
    if not possible:
        return result | {"issues": [{"reason": "preferred_pickup_time_unavailable", "available_slots": slots}]}
    request = CheckoutInput(account=alias, store_code=preferences.store_code, pickup_at=min(possible),
                            max_amount_cny=preferences.max_amount_cny, eat_type=preferences.eat_type)
    # Live availability may choose a later day than the first preferred date.
    if existing := existing_meal(request.pickup_at):
        return existing
    menu = None
    selections, descriptions, issues, estimate = [], [], [], 0
    for preference in preferences.items:
        coupon_ref = None
        variant_of = None
        if preference.variant_of:
            if preference.coupon_name:
                issues.append({"reason": "preferred_variant_cannot_mix_coupon_source", "name": preference.name})
                continue
            if menu is None:
                menu = provider.menu(request)["products"]
            parents = {p["offer_id"]: p for p in menu if p["name"] == preference.variant_of
                       and str(p.get("product_type")) == "3"
                       and bool(p.get("card_name")) == preferences.require_card_discount}
            if len(parents) != 1:
                issues.append({"reason": "preferred_variant_parent_unavailable_or_ambiguous", "name": preference.variant_of})
                continue
            parent = next(iter(parents.values()))
            parent_detail = provider.product_detail(request, parent["product_code"], parent["offer_id"])
            variants = [v for v in parent_detail["variants"] if v["name"] == preference.name and v.get("parent_detail_selection")]
            if preference.variant_specifications:
                def matches(variant):
                    choices = variant.get("specification_choices", [])
                    names = {c["group_name"]: c["choice_name"] for c in choices}
                    return len(names) == len(choices) and all(names.get(k) == v for k, v in preference.variant_specifications.items())
                variants = [v for v in variants if matches(v)]
            if len(variants) != 1:
                issues.append({"reason": "preferred_variant_unavailable_or_unsupported", "name": preference.name,
                               "variants": parent_detail["variants"]})
                continue
            variant = variants[0]
            variant_of = variant["parent_detail_selection"]["variant_of"]
            offers = [variant | {"offer_id": variant["parent_detail_selection"]["offer_id"], "card_name": variant.get("card_name")}]
        elif preference.coupon_name:
            if preferences.require_card_discount:
                issues.append({"reason": "explicit_coupon_conflicts_with_required_card_discount", "name": preference.name})
                continue
            candidates = [c for c in provider.coupons(request)["coupons"] if c["name"] == preference.coupon_name]
            if len(candidates) != 1 or not candidates[0]["available_for_request"]:
                issues.append({"reason": "preferred_coupon_unavailable_or_ambiguous", "name": preference.coupon_name,
                               "coupons": candidates})
                continue
            if candidates[0]["configuration_path"] != "coupon_products":
                issues.append({"reason": "preferred_coupon_requires_menu_card_or_official_configuration", "name": preference.coupon_name})
                continue
            coupon_ref = candidates[0]["coupon_ref"]
            choices = provider.coupon_products(request, coupon_ref)["products"]
            offers = [p for p in choices if p["name"] == preference.name and str(p.get("product_type")) in ("1", "7")
                      and p["cart_configuration_supported"]]
        else:
            if menu is None:
                menu = provider.menu(request)["products"]
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
        detail = (provider.product_detail(request, offer["product_code"], offer["offer_id"], variant_of=variant_of) if variant_of else
                  provider.product_detail(request, offer["product_code"], offer["offer_id"], coupon_ref) if coupon_ref else
                  provider.product_detail(request, offer["product_code"], offer["offer_id"]))
        groups, extra, problems = choose_components(detail, preference.component_names)
        modifiers, modifier_problems = apply_preferred_modifiers(detail, preference, groups)
        problems.extend(modifier_problems)
        if detail.get("standard_configuration_supported") is False:
            problems.append({"reason": "product_default_configuration_unsupported"})
        if problems or type(detail.get("max_quantity")) is not int or preference.quantity > detail["max_quantity"]:
            issues.append({"reason": "product_configuration_required", "name": preference.name,
                           "details": problems, "max_quantity": detail.get("max_quantity")})
            continue
        selection = ItemSelection(product_code=offer["product_code"], offer_id=offer["offer_id"],
                                  quantity=preference.quantity, combo=groups, modifiers=modifiers, coupon_ref=coupon_ref,
                                  variant_of=variant_of)
        selections.append(selection.model_dump(exclude_none=True))
        descriptions.append({"name": offer["name"], "card_name": offer.get("card_name"), "coupon_name": preference.coupon_name,
                             "variant_of": preference.variant_of,
                             **({"variant_specifications": preference.variant_specifications} if preference.variant_specifications else {}),
                             "quantity": preference.quantity, "components": preference.component_names,
                             "modifiers": [m.model_dump(exclude_none=True) for m in preference.modifiers],
                             "component_modifiers": [m.model_dump(exclude_none=True) for m in preference.component_modifiers]})
        if estimate is not None:
            base = detail.get("configuration_base_cny", offer.get("display_price_cny"))
            fees = configuration_extra(detail, modifiers, groups)
            estimate = estimate + (cents(base) + extra + fees) * preference.quantity if base is not None and extra is not None else None
    if estimate is not None and estimate > cents(preferences.max_amount_cny):
        issues.append({"reason": "estimated_price_exceeds_preference_limit"})
    key = "meal-" + fingerprint({"account": alias, "store_code": request.store_code, "pickup_at": request.pickup_at})[:32]
    return result | {"request": request.model_dump(), "items": selections if not issues else [],
        "products": descriptions, "selection_ready": not issues and bool(selections), "issues": issues,
        "estimated_amount_cny": yuan(estimate) if estimate is not None and not issues else None,
        "estimated_price_is_final_quote": False, "idempotency_key": key,
        "next_step": "cart_get_then_cart_add_configured_if_empty_then_checkout_quote" if not issues else "resolve_choices_before_cart_changes"}
