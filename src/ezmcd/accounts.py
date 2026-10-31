"""Local-only account enrollment. A phone label is never proof of authentication."""
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hmac
import json
import re
import time

from .errors import EzMcdError
from .models import fingerprint, Policy
from .protocol.signing import BUILD, constants, sign_v4
from .protocol.v5 import decode_verified_sample
from .storage import read_private


def import_session(store, alias, profile_path, binary, runtime_sample=None, phone_label=None, *, update=False):
    profile = read_private(Path(profile_path).expanduser())
    return _enroll(store, alias, profile, binary, runtime_sample, phone_label, update=update)


def profile_from_balance_sample(sample, binary):
    """Validate a user-selected official request locally; never replay its signature."""
    request = sample["request"]
    if (request.get("method"), request.get("host"), request.get("path"), request.get("body", "")) != (
            "GET", "api.mcd.cn", "/bff/member/mwallet/balance", ""):
        raise EzMcdError("successful_balance_sample_required")
    response = sample["response"]
    payload = json.loads(response["body"]) if isinstance(response["body"], str) else response["body"]
    if response.get("status") != 200 or payload.get("success") is not True:
        raise EzMcdError("successful_balance_sample_required")
    headers = {k.lower(): v for k, v in request["headers"].items()}
    if (headers.get("ct") != "101" or headers.get("sv") != "v4" or
            any(not isinstance(headers.get(k), str) or not headers[k] for k in ("sid", "token", "meddyid", "v", "language"))):
        raise EzMcdError("official_session_headers_incomplete")
    parts = headers.get("authorization", "").split("#")
    if len(parts) != 6:
        raise EzMcdError("invalid_official_sample_signature")
    ak, secret, _, _ = constants(binary)
    expected = sign_v4(headers, ak, secret, parsedate_to_datetime(parts[4]), "GET", request["path"])
    if any(not hmac.compare_digest(value, headers.get(key, "")) for key, value in expected.items()):
        raise EzMcdError("invalid_official_sample_signature")
    mapping = {"sid": "sid", "token": "deviceToken", "meddyid": "meddyId",
               "routingid": "routingId", "tid": "tid", "d": "riskDeviceId"}
    return {"schema_version": 2, "imported_at": datetime.now(timezone.utc).isoformat(),
        "source": "user-selected signed balance sample", "build_sha256": BUILD,
        "verified_endpoint": request["path"], "fields": {v: headers[k] for k, v in mapping.items() if headers.get(k)},
        "client_headers": {k: headers[k] for k in ("ct", "v", "language")}}


def connect_samples(store, alias, balance_sample, binary, runtime_sample=None, phone_label=None, *, update=False):
    profile = profile_from_balance_sample(read_private(Path(balance_sample).expanduser()), binary)
    return _enroll(store, alias, profile, binary, runtime_sample, phone_label, update=update, check_live=True)


def connect_har(store, alias, har_path, binary, phone_label=None, *, update=False):
    from .capture import samples_from_har
    balance, runtime = samples_from_har(read_private(Path(har_path).expanduser()))
    profile = profile_from_balance_sample(balance, binary)
    return _enroll(store, alias, profile, binary, runtime, phone_label, update=update, check_live=True)


def _enroll(store, alias, profile, binary, runtime_sample, phone_label, *, update=False, check_live=False):
    fields = profile.get("fields", {})
    if (profile.get("build_sha256") != BUILD or profile.get("verified_endpoint") != "/bff/member/mwallet/balance"
            or not all(isinstance(fields.get(k), str) and fields[k] for k in ("sid", "deviceToken", "meddyId"))
            or not all(profile.get("client_headers", {}).get(k) for k in ("ct", "v", "language"))):
        raise EzMcdError("validated_official_session_required")
    native = constants(binary)
    runtime = None
    if runtime_sample:
        sample = runtime_sample if isinstance(runtime_sample, dict) else read_private(Path(runtime_sample).expanduser())
        request = sample.get("request", {})
        headers = request.get("headers", {})
        if (request.get("host") != "api.mcd.cn" or request.get("path") != "/bff/order/orders"
                or request.get("method") != "POST" or headers.get("sid") != fields["sid"]
                or headers.get("token") != fields["deviceToken"]):
            raise EzMcdError("runtime_sample_account_mismatch")
        observed, inputs = decode_verified_sample(request["path"], headers.get("x-mcd-sign"), native[2])
        if not 0 <= time.time() - inputs["milliseconds"] / 1000 <= 86400:
            raise EzMcdError("runtime_sample_expired")
        runtime = {"device_hex": observed.device_hex, "environment_hex": observed.environment_hex,
                   "observed_at": inputs["milliseconds"] / 1000}
    masked = None
    if phone_label:
        if not re.fullmatch(r"(?:\+?86)?1[3-9][0-9]{9}", phone_label):
            raise EzMcdError("invalid_mainland_phone_label")
        number = phone_label[-11:]
        masked = number[:3] + "****" + number[-4:]
    identity = fingerprint({"provider": "mcd-cn", "member": fields["meddyId"]})
    record = {"alias": alias, "identity": identity, "provider": "mcd-cn",
        "phone_label": masked, "phone_ownership_verified_by_project": False,
        "profile": profile, "binary": str(Path(binary).expanduser().absolute()), "runtime": runtime,
        "policy": Policy().model_dump(), "imported_at": time.time()}
    if check_live:
        from .provider import McdProvider
        McdProvider(record).wallet()
    if update:
        def replace(old):
            if old["identity"] != identity:
                raise EzMcdError("cannot_replace_alias_with_different_account")
            record["policy"] = old["policy"]
            record["phone_label"] = masked or old.get("phone_label")
            return record
        store.modify_account(alias, replace)
    else:
        store.add_account(record)
    result = public_account(record)
    if check_live:
        result["session_status"] = "verified_now"
    return result


def revoke_local(store, alias):
    def remove(record):
        record.pop("profile", None)
        record.pop("runtime", None)
        record["revoked"] = True
        record["policy"]["allow_checkout"] = False
        return record
    store.modify_account(alias, remove)
    return {"account": alias, "local_credentials_removed": True, "journal_preserved": True,
            "official_session_revoked": False, "reconnect": "Connect the same account with --update."}


def public_account(record):
    runtime = record.get("runtime")
    return {"account": record["alias"], "provider": record["provider"],
        "phone_label": record.get("phone_label"), "auth_method": "offline_demo" if record["provider"] == "demo" else "official_session_import",
        "session_status": "revoked_locally" if record.get("revoked") else "imported_verify_with_account_check", "sms_login_supported": False,
        "runtime_available": bool(runtime and 0 <= time.time() - runtime["observed_at"] <= 86400),
        "policy": record["policy"]}
