"""Finish an explicit local capture after cleanup, without replaying its writes.

Capture records are local recovery evidence, not cryptographic proof that other
clients made no purchases. Official signatures and current reads are verified
again; the account replacement and receipt are one atomic private-file write.
"""
from pathlib import Path
import re
import time

from .accounts import _enroll, profile_from_balance_sample, public_account, runtime_from_sample
from .errors import EzMcdError
from .models import fingerprint
from .provider import McdProvider
from .runtime_recovery import fresh_evidence
from .storage import read_private
from .transport import Transport
from .sampling_session import validate_session


def credential_fingerprint(record):
    return fingerprint({k: record.get(k) for k in ("identity", "binary", "profile", "runtime")})


def verify_capture_runtime(record):
    result = Transport(record).verify_runtime()
    payload = result.get("response", {})
    if result.get("http_status") != 200 or not isinstance(payload, dict) or payload.get("success") is not True:
        raise EzMcdError("runtime_readonly_verification_failed")
    if not isinstance(payload.get("data"), dict) or not isinstance(payload["data"].get("list"), list):
        raise EzMcdError("runtime_verification_response_unrecognized")
    if not fresh_evidence(record.get("runtime"), time.time()):
        raise EzMcdError("runtime_sample_expired")


def _capture_files(store, directory, *, connection=False, maintenance=False):
    directory = Path(directory).expanduser().absolute()
    parent = store.root / "captures"
    if (directory.parent != parent or not re.fullmatch(r"[0-9a-f]{32}", directory.name)
            or parent.is_symlink() or directory.is_symlink() or not directory.is_dir()
            or directory.stat().st_mode & 0o077):
        raise EzMcdError("sampling_capture_directory_invalid")
    files = {name: read_private(directory / (name + ".json"))
             for name in (("config", "audit", "cleanup", "balance") if connection else
                          ("config", "audit", "cleanup", "balance", "runtime"))}
    if not all(isinstance(value, dict) for value in files.values()):
        raise EzMcdError("sampling_capture_incomplete")
    config, audit, cleanup = (files[k] for k in ("config", "audit", "cleanup"))
    if connection:
        scope = (config.get("schema_version") == 4 and config.get("purpose") == "account_maintenance"
                 and all(isinstance(config.get(k), str) and re.fullmatch(r"[0-9a-f]{64}", config[k])
                         for k in ("identity", "account_fingerprint"))) if maintenance else (
                     config.get("schema_version") == 3 and config.get("purpose") == "account_connect"
                     and config.get("identity") is None)
        bound = (scope and config.get("refresh_session") is None
                 and isinstance(config.get("alias"), str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", config["alias"])
                 and isinstance(config.get("binary"), str) and Path(config["binary"]).is_absolute())
    else:
        bound = (config.get("schema_version") == 2 and isinstance(config.get("account_fingerprint"), str)
                 and re.fullmatch(r"[0-9a-f]{64}", config["account_fingerprint"]))
    if not bound or config.get("output") != str(directory):
        raise EzMcdError("sampling_capture_binding_missing")
    if (audit.get("balance_saved") is not True or audit.get("runtime_saved") is not (not connection)
            or type(audit.get("blocked_requests")) is not int or audit["blocked_requests"] < (1 if connection else 2)):
        raise EzMcdError("sampling_capture_incomplete")
    before, after = cleanup.get("proxy_before"), cleanup.get("proxy_after")
    if config.get("transport") is not None:
        from .sampling_local import TRANSPORT, LOCAL_MODE
        if (not connection or config["transport"] != TRANSPORT or config.get("local_mode") != LOCAL_MODE
                or cleanup.get("transport") != TRANSPORT
                or cleanup.get("system_proxy_modified") is not False
                or cleanup.get("system_proxy_unchanged") is not True
                or cleanup.get("capture_process_stopped") is not True
                or not isinstance(cleanup.get("effective_proxy_before"), dict)
                or cleanup["effective_proxy_before"] != cleanup.get("effective_proxy_after")):
            raise EzMcdError("sampling_cleanup_needs_attention")
    if (cleanup.get("official_app_closed") is not True or cleanup.get("cleanup_errors") != []
            or cleanup.get("app_launch_pending", False) is not False
            or not isinstance(before, dict) or before.get("Enabled") != "No" or before != after):
        raise EzMcdError("sampling_cleanup_needs_attention")
    session_path = directory / "session.json"
    if connection:
        if (audit.get("allowed_session_refreshes", 0) != 0 or audit.get("session_saved", False) is not False
                or any((directory / name).exists() or (directory / name).is_symlink() for name in ("session.json", "runtime.json"))):
            raise EzMcdError("sampling_connection_scope_invalid")
        return directory, files
    if session_path.exists() or session_path.is_symlink():
        files["session"] = read_private(session_path)
    elif audit.get("session_saved") is True:
        raise EzMcdError("sampling_session_evidence_invalid")
    return directory, files


def import_capture(store, alias, directory):
    directory, files = _capture_files(store, directory)
    config = files["config"]
    account = store.account(alias)
    if account.get("revoked") or account["provider"] != "mcd-cn":
        raise EzMcdError("active_official_account_required_for_runtime_update")
    if config.get("identity") != account["identity"] or config.get("binary") != account["binary"]:
        raise EzMcdError("sampling_member_mismatch")
    evidence = fingerprint({k: files[k] for k in ("balance", "runtime", "session") if k in files})
    receipt = {"capture_id": directory.name, "evidence_fingerprint": evidence}
    previous = account.get("sampling_import", {})
    repeated = (all(previous.get(k) == v for k, v in receipt.items())
                and previous.get("credential_fingerprint") == credential_fingerprint(account))
    if not repeated and fingerprint(account) != config["account_fingerprint"]:
        raise EzMcdError("sampling_account_changed_start_new_capture")
    # Validate both signed samples before any networking or credential update.
    profile = profile_from_balance_sample(files["balance"], account["binary"])
    if fingerprint({"provider": "mcd-cn", "member": profile["fields"]["meddyId"]}) != account["identity"]:
        raise EzMcdError("sampling_member_mismatch")
    runtime_from_sample(profile, files["runtime"], account["binary"])
    if "session" in files:
        profile["fields"]["refreshToken"] = validate_session(files["session"], profile, config, files["audit"])
    if repeated:
        with store.lock(account["identity"]):
            if fingerprint(store.account(alias)) != fingerprint(account):
                raise EzMcdError("sampling_account_changed_start_new_capture")
            McdProvider(account).wallet()
            verify_capture_runtime(account)
        result = public_account(account, session_verified=True)
    else:
        result = _enroll(store, alias, profile, account["binary"], files["runtime"], None,
            update=True, check_live=True, expected_account=config["account_fingerprint"],
            verify_runtime=True, capture_receipt=receipt)
    return result | {"account_updated": not repeated, "already_imported": repeated,
        "capture_directory": str(directory), "session_status": "verified_now",
        "verification": ["wallet_balance_get", "v5_order_list_get"],
        "observation_time_preserved": True, "policy_preserved": True, "journal_preserved": True,
        "order_created": False, "payment_attempted": False, "cart_changed": False,
        "order_post_acceptance_verified": False, "cross_day_validity_verified": False,
        "refresh_token_captured": "session" in files, "refresh_token_redemption_tested": False,
        "next_action": "凭据已核验。让 Agent 检查账号、原订单与购物车后继续选餐；本次未授权消费。"}
