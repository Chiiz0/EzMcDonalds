"""Recover a recent observation after the same member changes login session.

This is not device initialization or a new observation. The old evidence keeps
its original age and is inactive until a fixed v5 GET accepts the new session.
"""
from copy import deepcopy
import math
import re
import time

from .errors import EzMcdError
from .models import fingerprint
from .protocol.signing import BUILD
from .transport import Transport

SOURCE = "previous_own_device_readonly_verified"


def device_context(record):
    profile = record.get("profile", {})
    headers, fields = profile.get("client_headers", {}), profile.get("fields", {})
    if profile.get("build_sha256") != BUILD:
        return None
    values = {k: headers.get(k) for k in ("ct", "v", "language")}
    values.update({k: fields.get(k) for k in ("deviceToken", "riskDeviceId")})
    if any(not isinstance(v, str) or not v for v in values.values()):
        return None
    return fingerprint({"build": BUILD, "values": values})


def fresh_evidence(runtime, now):
    if not isinstance(runtime, dict):
        return False
    observed = runtime.get("observed_at")
    return (type(observed) in (int, float) and math.isfinite(observed) and 0 <= now - observed <= 86400
        and isinstance(runtime.get("device_hex"), str) and re.fullmatch(r"[0-9A-F]{32}", runtime["device_hex"]) is not None
        and isinstance(runtime.get("environment_hex"), str) and re.fullmatch(r"[0-9a-f]{16}", runtime["environment_hex"]) is not None)


def retain_candidate(old, new, now):
    context = device_context(new)
    if (old.get("revoked") or old.get("identity") != new.get("identity") or context is None
            or context != device_context(old)):
        return None
    prior = old.get("runtime_candidate", {})
    evidence = old.get("runtime") or (prior.get("evidence") if prior.get("device_context") == context else None)
    if not fresh_evidence(evidence, now):
        return None
    return {"device_context": context, "evidence": deepcopy(evidence)}


def recovery_available(record, now):
    candidate = record.get("runtime_candidate")
    return bool(not record.get("revoked") and record.get("provider") == "mcd-cn"
        and record.get("session_refresh", {}).get("state") not in ("CLAIMED", "CANDIDATE", "STOPPED")
        and isinstance(candidate, dict) and candidate.get("device_context") is not None
        and candidate.get("device_context") == device_context(record) and fresh_evidence(candidate.get("evidence"), now))


def restore_runtime(store, alias, *, transport_factory=Transport, clock=time.time):
    outcome = {}
    def update(record):
        if record.get("revoked") or record.get("provider") != "mcd-cn":
            raise EzMcdError("active_official_account_required_for_runtime_update")
        if record.get("session_refresh", {}).get("state") in ("CLAIMED", "CANDIDATE", "STOPPED"):
            raise EzMcdError("complete_session_refresh_or_login_before_runtime_update")
        now = clock()
        active = record.get("runtime")
        session = fingerprint({"sid": record["profile"]["fields"]["sid"]})
        repeated = bool(isinstance(active, dict) and active.get("source") == SOURCE
                        and active.get("verified_session_fingerprint") == session
                        and device_context(record) is not None
                        and active.get("verified_device_context") == device_context(record))
        if repeated:
            evidence = deepcopy(active)
        else:
            if fresh_evidence(active, now):
                raise EzMcdError("runtime_already_available_use_account_check")
            if not recovery_available(record, now):
                raise EzMcdError("previous_runtime_unavailable_update_official_evidence")
            evidence = deepcopy(record["runtime_candidate"]["evidence"])
        if not fresh_evidence(evidence, now):
            raise EzMcdError("previous_runtime_unavailable_update_official_evidence")
        candidate = deepcopy(record)
        candidate["runtime"] = evidence
        response = transport_factory(candidate).verify_runtime()
        payload = response.get("response", {})
        if response.get("http_status") != 200 or payload.get("success") is not True:
            raise EzMcdError("runtime_readonly_verification_failed")
        if not isinstance(payload.get("data"), dict) or not isinstance(payload["data"].get("list"), list):
            raise EzMcdError("runtime_verification_response_unrecognized")
        verified = clock()
        if not fresh_evidence(evidence, verified):
            raise EzMcdError("previous_runtime_unavailable_update_official_evidence")
        if not repeated:
            evidence.update(source=SOURCE, verified_at=verified, verified_session_fingerprint=session,
                            verified_device_context=device_context(record))
            record["runtime"] = evidence
            record.pop("runtime_candidate", None)
        outcome.update(runtime_restored=not repeated, repeated=repeated)
        return record
    updated = store.modify_account(alias, update)
    from .accounts import public_account
    return public_account(updated, session_verified=True) | outcome | {
        "observation_time_preserved": True, "verification": "v5_order_list_get",
        "order_post_acceptance_verified": False, "cross_day_validity_verified": False,
        "order_created": False, "payment_attempted": False}
