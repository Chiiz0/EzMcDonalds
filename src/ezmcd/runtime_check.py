"""Read-only evidence about an observed runtime; never a renewal mechanism."""
import math
import time

from .errors import EzMcdError
from .models import fingerprint
from .transport import Transport


def check_runtime(store, alias, *, transport_factory=Transport, clock=time.time):
    record = store.account(alias)
    if record.get("revoked") or record["provider"] != "mcd-cn":
        raise EzMcdError("runtime_check_requires_active_real_account")
    if record.get("session_refresh", {}).get("state") in ("CLAIMED", "CANDIDATE", "STOPPED"):
        raise EzMcdError("complete_session_refresh_or_login_before_runtime_check")
    runtime = record.get("runtime")
    observed = runtime.get("observed_at") if isinstance(runtime, dict) else None
    now = clock()
    if type(observed) not in (int, float) or not math.isfinite(observed) or observed > now:
        raise EzMcdError("runtime_evidence_missing_or_invalid")
    # No account-file mutation and no GET response bodies/order history in output.
    # The identity lock prevents credential rotation or checkout in this project
    # during the probe. It does not stop another official client from logging in.
    with store.lock(record["identity"]):
        current = store.account(alias)
        if fingerprint(current) != fingerprint(record):
            raise EzMcdError("account_changed_before_runtime_check")
        response = transport_factory(record).verify_runtime(historical=True)
    payload = response["response"]
    accepted = (response["http_status"] == 200 and payload.get("success") is True
                and isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("list"), list))
    return {"account": alias, "checked_at": clock(), "observation_age_hours": round((now-observed)/3600, 3),
        "http_status": response["http_status"], "read_only_v5_verified": accepted,
        "verification_scope": "GET /bff/order/orders (first page, one item)",
        "observation_expired_by_project_policy": now-observed > 86400,
        "observation_time_changed": False, "account_changed": False,
        "checkout_authorized_by_check": False, "order_post_acceptance_verified": False,
        "next_action": ("runtime_observation_update_still_required_before_checkout" if now-observed > 86400
                        else "continue_normal_quote_and_spending_checks") if accepted else "check_session_and_official_runtime_locally",
        "order_created": False, "payment_attempted": False}
