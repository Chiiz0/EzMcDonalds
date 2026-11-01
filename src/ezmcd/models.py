from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import EzMcdError

SHANGHAI = ZoneInfo("Asia/Shanghai")


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def cents(value) -> int:
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
            raise ValueError
        return int(amount)
    except (ValueError, InvalidOperation):
        raise EzMcdError("invalid_money_amount") from None


def yuan(value: int) -> str:
    if type(value) is not int or value < 0:
        raise EzMcdError("invalid_cent_amount")
    return f"{value // 100}.{value % 100:02}"


def pickup_time(value: str) -> datetime:
    try:
        when = datetime.fromisoformat(value)
        if when.tzinfo is None or when.second or when.microsecond:
            raise ValueError
        return when.astimezone(SHANGHAI)
    except ValueError:
        raise EzMcdError("pickup_requires_iso8601_with_timezone_and_minute_precision") from None


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    allow_checkout: bool = False
    max_order_cents: int = Field(default=2000, ge=1, le=100000)
    daily_budget_cents: int = Field(default=2000, ge=1, le=1000000)
    max_orders_per_day: int = Field(default=1, ge=1, le=20)
    allowed_stores: list[str] = Field(default_factory=list)
    expires_at: str | None = None

    @field_validator("allowed_stores")
    @classmethod
    def stores(cls, values):
        if any(len(x) != 7 or not x.isascii() or not x.isdigit() for x in values):
            raise ValueError("Store codes must contain seven digits")
        return values

    @field_validator("expires_at")
    @classmethod
    def expiry(cls, value):
        if value is not None:
            pickup_time(value)
        return value


class ComponentSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product_code: str = Field(pattern=r"^[0-9]{1,20}$")
    quantity: int = Field(default=1, ge=1, le=20)


class ComboSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    round: int = Field(ge=0, le=100)
    items: list[ComponentSelection] = Field(min_length=1, max_length=20)


class ItemSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product_code: str = Field(pattern=r"^[A-Za-z0-9]{1,20}$")
    quantity: int = Field(default=1, ge=1, le=20)
    offer_id: str | None = None
    combo: list[ComboSelection] = Field(default_factory=list, max_length=20)


class CheckoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    store_code: str = Field(pattern=r"^[0-9]{7}$")
    pickup_at: str
    max_amount_cny: str
    eat_type: str = "locker-in"

    @field_validator("pickup_at")
    @classmethod
    def pickup(cls, value):
        return pickup_time(value).isoformat()

    @field_validator("max_amount_cny")
    @classmethod
    def maximum(cls, value):
        if not 0 < cents(value) <= 100000:
            raise ValueError("Positive limit no greater than 1000 CNY required")
        return yuan(cents(value))

    @field_validator("eat_type")
    @classmethod
    def eat(cls, value):
        # Other codes require additional fulfilment fields and acceptance tests.
        if value != "locker-in":
            raise ValueError("This adapter currently supports in-store pickup lockers")
        return value


class StoreSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    city: str | None = Field(default=None, min_length=1, max_length=60)
    keyword: str = Field(default="", max_length=80)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    page: int = Field(default=1, ge=1, le=100)
    limit: int = Field(default=10, ge=1, le=30)

    @field_validator("city", "keyword")
    @classmethod
    def text(cls, value):
        if value is not None and (any(ord(c) < 32 for c in value) or not value.strip() and value):
            raise ValueError("Use a city name/code and a plain search term")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def location(self):
        coordinates = self.latitude is not None or self.longitude is not None
        if bool(self.city) == coordinates or coordinates and (self.latitude is None or self.longitude is None):
            raise ValueError("Provide either city name/code or both latitude and longitude")
        return self
