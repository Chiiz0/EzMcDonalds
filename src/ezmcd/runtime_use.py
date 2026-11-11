"""Explicit, bounded use of an existing observation; never evidence renewal.

The local owner chooses the maximum age (up to fourteen days from observation).
An authorization is bound to the exact credentials and sample. Its initial GET
is not an order check: every historical order needs another, in-memory check.
"""
import math
import re
import time

from .errors import EzMcdError
from .models import fingerprint

DEFAULT_HOURS = 24
MAX_HOURS = 336  # Explicit local experiment limit, not an official credential lifetime.


def evidence_age(record, now):
    runtime = record.get("runtime")
    observed = runtime.get("observed_at") if isinstance(runtime, dict) else None
    if (type(observed) not in (int, float) or not math.isfinite(observed) or observed < 0
            or not 0 <= now - observed
            or not isinstance(runtime.get("device_hex"), str)
            or re.fullmatch(r"[0-9A-F]{32}", runtime["device_hex"]) is None
            or not isinstance(runtime.get("environment_hex"), str)
            or re.fullmatch(r"[0-9a-f]{16}", runtime["environment_hex"]) is None):
        return None
    return now - observed


def binding(record):
    return fingerprint({k: record.get(k) for k in ("identity", "binary", "profile", "runtime")})


def active(record, now):
    grant = record.get("runtime_use")
    age = evidence_age(record, now)
    if (not isinstance(grant, dict) or age is None or record.get("revoked")
            or record.get("provider") != "mcd-cn"
            or record.get("session_refresh", {}).get("state") in ("CLAIMED", "CANDIDATE", "STOPPED")):
        return False
    hours, granted, expires = (grant.get(k) for k in ("maximum_observation_age_hours", "granted_at", "expires_at"))
    return bool(type(grant.get("schema_version")) is int and grant["schema_version"] == 1
        and type(hours) is int and DEFAULT_HOURS < hours <= MAX_HOURS
        and type(granted) in (int, float) and math.isfinite(granted)
        and type(expires) in (int, float) and math.isfinite(expires)
        and record["runtime"]["observed_at"] <= granted <= now < expires
        and expires == record["runtime"]["observed_at"] + hours * 3600
        and grant.get("binding") == binding(record))


def available(record, now):
    age = evidence_age(record, now)
    return age is not None and (age <= DEFAULT_HOURS * 3600 or active(record, now))


def view(record, now):
    enabled = active(record, now)
    grant = record.get("runtime_use", {})
    return {"state": "active" if enabled else "expired_or_changed" if grant else "not_enabled",
        "maximum_observation_age_hours": grant["maximum_observation_age_hours"] if enabled else DEFAULT_HOURS,
        "expires_at": grant["expires_at"] if enabled else None,
        "historical_order_requires_new_readonly_check": enabled,
        "observation_time_changed": False, "spending_authorized": False,
        "order_post_acceptance_verified": False}


def require_accepted(response):
    payload = response.get("response") if isinstance(response, dict) else None
    if (not isinstance(payload, dict) or response.get("http_status") != 200
            or payload.get("success") is not True or not isinstance(payload.get("data"), dict)
            or not isinstance(payload["data"].get("list"), list)):
        raise EzMcdError("runtime_use_readonly_check_failed")


def configure(store, alias, maximum_hours=None, *, transport_factory=None, clock=time.time):
    """Local CLI only. None revokes without networking; a grant checks one GET."""
    if maximum_hours is not None and (type(maximum_hours) is not int or not DEFAULT_HOURS < maximum_hours <= MAX_HOURS):
        raise EzMcdError("runtime_use_age_limit_invalid")
    if transport_factory is None:
        from .transport import Transport
        transport_factory = Transport
    outcome = {}

    def update(record):
        if maximum_hours is None:
            outcome["changed"] = record.pop("runtime_use", None) is not None
            return record
        from .runtime_recovery import device_context
        now = clock()
        age = evidence_age(record, now)
        if (record.get("revoked") or record.get("provider") != "mcd-cn" or device_context(record) is None
                or record.get("session_refresh", {}).get("state") in ("CLAIMED", "CANDIDATE", "STOPPED")
                or not record.get("profile", {}).get("fields", {}).get("sid")):
            raise EzMcdError("runtime_use_requires_current_account_and_evidence")
        if age is None or age >= maximum_hours * 3600:
            raise EzMcdError("runtime_use_observation_outside_limit")
        require_accepted(transport_factory(record).verify_runtime(historical=True))
        verified = clock()
        if verified < now or verified >= record["runtime"]["observed_at"] + maximum_hours * 3600:
            raise EzMcdError("runtime_use_observation_outside_limit")
        repeated = active(record, verified) and record["runtime_use"]["maximum_observation_age_hours"] == maximum_hours
        if not repeated:
            record["runtime_use"] = {"schema_version": 1, "binding": binding(record),
                "granted_at": verified, "expires_at": record["runtime"]["observed_at"] + maximum_hours * 3600,
                "maximum_observation_age_hours": maximum_hours}
        outcome["changed"] = not repeated
        return record

    record = store.modify_account(alias, update)
    return {"account": alias, "runtime_use": view(record, clock()), **outcome,
        "verification": "v5_order_list_get" if maximum_hours is not None else None,
        "observation_time_changed": False, "policy_preserved": True, "journal_preserved": True,
        "order_created": False, "payment_attempted": False}
