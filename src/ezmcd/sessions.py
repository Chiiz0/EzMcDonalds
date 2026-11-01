"""Explicit session maintenance, with durable claims before token rotation."""
from copy import deepcopy
import time

from .accounts import public_account
from .errors import EzMcdError
from .models import fingerprint, yuan
from .provider import McdProvider
from .sms import diagnostics, response_data
from .storage import write_private
from .transport import Transport

REFRESH = "/bff/passport/login/session/refresh"
MIN_REFRESH_INTERVAL = 86400  # Local maintenance interval, not a server token TTL.


def session_fingerprint(fields):
    return fingerprint({k: fields.get(k) for k in ("sid", "refreshToken")})


def refresh_account(store, alias, *, provider_factory=McdProvider, transport_factory=Transport, clock=time.time):
    """Rotate once, or resume verification. Never retry an ambiguous POST.

    Intermediate claims and candidate credentials share the private account file.
    Store.modify_account holds the same identity lock used by checkout throughout.
    Its final atomic write commits both the new session and completed refresh state.
    """
    outcome = {}

    def update(record):
        if record.get("revoked"):
            raise EzMcdError("account_revoked_reconnect_locally")
        if record["provider"] != "mcd-cn":
            raise EzMcdError("session_refresh_requires_real_account")
        fields = record["profile"]["fields"]
        if not isinstance(fields.get("refreshToken"), str) or not fields["refreshToken"]:
            raise EzMcdError("refresh_token_missing_use_local_sms_login")
        current = session_fingerprint(fields)
        previous = record.get("session_refresh", {})
        now = clock()

        def persist():
            write_private(store.root / "accounts" / (record["identity"] + ".json"), record)

        if (previous.get("state") == "REFRESHED" and previous.get("result_fingerprint") == current
                and now - previous["completed_at"] < MIN_REFRESH_INTERVAL):
            # Check the current session without rotating it a second time.
            outcome.update(balance=provider_factory(record).wallet()["balance_cents"], rotated=False)
            return record

        if previous.get("source_fingerprint") == current and previous.get("state") != "REFRESHED":
            if previous.get("state") != "CANDIDATE":
                raise EzMcdError("session_refresh_already_attempted_relogin_locally")
            state = previous
        else:
            state = {"state": "CLAIMED", "attempted_at": now, "source_fingerprint": current}
            record["session_refresh"] = state
            persist()  # A crash after this point cannot resend the old refresh token.
            try:
                response = transport_factory(record).send("POST", REFRESH, body={"refreshToken": fields["refreshToken"]})
                state["diagnostics"] = diagnostics(response)
                content = response_data(response, "session_refresh")
                if (not isinstance(content, dict)
                        or any(not isinstance(content.get(k), str) or not 0 < len(content[k]) <= 16384
                               or "\r" in content[k] or "\n" in content[k] for k in ("sid", "refreshToken"))
                        or content.get("meddyId", fields["meddyId"]) != fields["meddyId"]):
                    raise EzMcdError("session_refresh_response_invalid_relogin_locally")
                state["candidate_fields"] = {k: content[k] for k in ("sid", "refreshToken")}
                state["state"] = "CANDIDATE"
                persist()  # Preserve rotated credentials before read-only verification.
            except EzMcdError as error:
                state["state"] = "STOPPED"
                state["error"] = error.code
                persist()
                raise

        candidate = deepcopy(record)
        candidate["profile"]["fields"].update(state["candidate_fields"])
        try:
            balance = provider_factory(candidate).wallet()["balance_cents"]
        except EzMcdError:
            raise EzMcdError("session_refresh_received_resume_verification") from None
        # Session-bound evidence is not renewed by an authentication response.
        sid_changed = fields["sid"] != state["candidate_fields"]["sid"]
        if sid_changed:
            candidate["runtime"] = None
            from .runtime_recovery import retain_candidate
            evidence = retain_candidate(record, candidate, clock())
            if evidence:
                candidate["runtime_candidate"] = evidence
            else:
                candidate.pop("runtime_candidate", None)
        candidate["session_refresh"] = {k: v for k, v in state.items() if k != "candidate_fields"} | {
            "state": "REFRESHED", "completed_at": clock(), "sid_changed": sid_changed,
            "result_fingerprint": session_fingerprint(candidate["profile"]["fields"])}
        outcome.update(balance=balance, rotated=True)
        return candidate

    account = store.modify_account(alias, update)
    return public_account(account, session_verified=True) | {
        "session_status": "verified_now", "wallet_balance_cny": yuan(outcome["balance"]),
        "session_refreshed": outcome["rotated"], "automatic_write_retry": False,
        "order_created": False, "payment_attempted": False}
