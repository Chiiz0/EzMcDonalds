"""Local, explicit SMS authentication; no checkout, payment, or automatic retries."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import time
import uuid

from .accounts import _enroll
from .errors import EzMcdError
from .models import fingerprint
from .protocol.login import encrypt_field, login_key
from .protocol.signing import BUILD, constants, sign_v4
from .storage import read_private, write_private
from .transport import API, Transport

SMS_SEND = "/bff/passport/verifyCode/sms/send"
MOBILE_LOGIN = "/bff/passport/login/mobile"
AUTH_LOCK = fingerprint({"scope": "local-sms-authentication"})
CLIENT_HEADERS = {"ct": "101", "v": "7.0.44.0", "language": "cn"}
SECURITY_RESTRICTION = "official_login_security_restriction_707053"
SECURITY_ERRORS = {SECURITY_RESTRICTION, "sms_rejected_707053", "login_rejected_707053"}


def normalize_phone(value):
    value = value.strip().replace(" ", "")
    if value.startswith("+86"):
        value = value[3:]
    if not re.fullmatch(r"1[3-9][0-9]{9}", value):
        raise EzMcdError("invalid_mainland_phone_number")
    return value


def device_context(store, binary, source_account=None):
    """Require the user's observed official device context before sending SMS.

    A locally generated installation ID alone failed real login acceptance.
    No SDK value is generated, and no prior account session is copied into login.
    """
    constants(binary)
    if not source_account:
        raise EzMcdError("official_login_device_context_required_use_device_from")
    record = store.account(source_account)
    if record.get("revoked") or record.get("provider") != "mcd-cn":
        raise EzMcdError("official_device_source_required")
    profile = record["profile"]
    device = {"build_sha256": BUILD, "source": "observed_official_device",
        "fields": {k: profile["fields"][k] for k in ("deviceToken", "riskDeviceId") if profile["fields"].get(k)},
        "client_headers": {k: profile["client_headers"][k] for k in CLIENT_HEADERS}}
    validate_device(device)
    return device


def validate_device(device):
    """Check required fields, without claiming to validate the SDK attestation."""
    fields = device.get("fields", {})
    if (device.get("build_sha256") != BUILD or
            not all(isinstance(fields.get(k), str) and fields[k] for k in ("deviceToken", "riskDeviceId")) or
            not 0 < len(fields["riskDeviceId"].encode("utf-8")) <= 4095):
        raise EzMcdError("official_login_device_context_incomplete")


class LoginTransport:
    def __init__(self, binary, device):
        validate_device(device)
        self.binary, self.device = binary, device

    def send(self, path, body):
        business = {SMS_SEND: ("1011", "500"), MOBILE_LOGIN: ("1004", "500")}
        if path not in business:
            raise EzMcdError("login_endpoint_not_supported")
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        now = time.time()
        # No sid/meddyId/routingId/tid from an existing account is sent here.
        headers = {k: self.device["client_headers"][k] for k in CLIENT_HEADERS} | {"sv": "v4", "x-mcd-gw-v": "1",
            "token": self.device["fields"]["deviceToken"], "User-Agent": "McdApp (iOS)",
            "Accept": "application/json", "Content-Type": "application/json; charset=utf-8",
            "st": f"{now:.0f}", "nonce": str(int(now * 1000)) + str(secrets.randbelow(1000000)),
            "biz_from": business[path][0], "biz_scenario": business[path][1]}
        if self.device["fields"].get("riskDeviceId"):
            headers["d"] = self.device["fields"]["riskDeviceId"]
        ak, secret, _, _ = constants(self.binary)
        headers.update(sign_v4(headers, ak, secret, datetime.fromtimestamp(now, timezone.utc), "POST", path, body=raw))
        return Transport._open("POST", API + path, headers, raw)


class VerificationRequired(EzMcdError):
    def __init__(self, context):
        super().__init__("official_login_verification_required")
        self.context = context


def response_data(result, stage):
    payload = result["response"]
    content = payload.get("data")
    # Observed server response: account security risk; not an SMS delivery delay.
    # The response does not identify whether account or device context caused it.
    if str(payload.get("code")) == "707053":
        raise EzMcdError(SECURITY_RESTRICTION)
    if (str(payload.get("code")) == "420" or isinstance(content, dict)
            and (content.get("securityContext") or content.get("needVerify"))):
        raise VerificationRequired(content)
    if result["http_status"] != 200 or payload.get("success") is not True:
        code = str(payload.get("code", "unknown"))
        code = code if re.fullmatch(r"[0-9]{1,9}", code) else "unknown"
        raise EzMcdError(stage + "_rejected_" + code)
    return content


def summary(attempt):
    result = {k: attempt.get(k) for k in ("attempt_id", "state", "phone_label", "expires_at", "error", "account")} | {
        "automatic_retry": False, "order_created": False, "payment_attempted": False}
    diagnostic = attempt.get("login_diagnostics") or attempt.get("sms_diagnostics")
    if diagnostic:
        result["diagnostics"] = {k: diagnostic.get(k) for k in ("http_status", "code", "success", "message_zh")}
    if attempt.get("error") in SECURITY_ERRORS:
        # Also explain attempts saved before this error was understood.
        result["state"] = "OFFICIAL_ACTION_REQUIRED"
        result["next_step"] = "check_login_in_official_app_and_complete_any_official_verification"
    return result


def diagnostics(result):
    """Keep error metadata, never arbitrary response values or echoed credentials.

    This endpoint reports Chinese errors. Retain only CJK text and punctuation
    from a failure message; digits, URLs and opaque ASCII tokens are omitted.
    """
    payload = result.get("response", {})
    content = payload.get("data")
    code = str(payload.get("code", ""))
    message = payload.get("message") or payload.get("msg") or ""
    message_zh = re.sub(r"[^\u3400-\u4dbf\u4e00-\u9fff，。！？；：、（）《》“”‘’\s]+", "…", str(message)[:500])
    known_fields = {"securityContext", "needVerify", "newUser", "sid", "meddyId", "tel", "refreshToken"}
    status = result.get("http_status")
    return {"http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "code": code if re.fullmatch(r"[0-9]{1,9}", code) else "unknown",
            "success": payload.get("success") is True,
            "data_fields": sorted(known_fields.intersection(content)) if isinstance(content, dict) else [],
            "message_zh": message_zh if payload.get("success") is not True else ""}


class SMSLogin:
    def __init__(self, store, binary, device, transport=None, key=None):
        validate_device(device)
        self.store, self.binary, self.device = store, str(Path(binary).expanduser().absolute()), device
        self.key = key if key is not None else login_key(self.binary)
        self.transport = transport or LoginTransport(self.binary, device)

    def _path(self, attempt_id):
        if not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
            raise EzMcdError("invalid_login_attempt_id")
        return self.store.root / "auth" / "attempts" / (attempt_id + ".json")

    def begin(self, phone):
        phone = normalize_phone(phone)
        cipher_phone = encrypt_field(phone, self.key)
        with self.store.lock(AUTH_LOCK):
            ledger_path = self.store.root / "auth" / "sms-limits.json"
            ledger = read_private(ledger_path) if ledger_path.exists() else {"salt": secrets.token_hex(32), "sends": {}}
            phone_key = hmac.new(bytes.fromhex(ledger["salt"]), phone.encode(), hashlib.sha256).hexdigest()
            now = time.time()
            recent = [t for t in ledger["sends"].get(phone_key, []) if now - t < 3600]
            if recent and now - max(recent) < 60:
                raise EzMcdError("sms_cooldown_wait_60_seconds_no_automatic_resend")
            if len(recent) >= 3:
                raise EzMcdError("local_sms_hourly_limit_reached")
            ledger["sends"][phone_key] = recent + [now]
            attempt = {"attempt_id": uuid.uuid4().hex, "state": "SMS_ATTEMPTED", "created_at": now,
                "expires_at": now + 300, "cipher_phone": cipher_phone, "phone_label": phone[:3] + "****" + phone[-4:],
                "device": self.device, "binary": self.binary}
            write_private(ledger_path, ledger)
            self._save(attempt)  # Persist claim before the request, including unknown outcomes.
            try:
                result = self.transport.send(SMS_SEND, {"tel": cipher_phone, "type": 1, "regionCode": "86"})
                attempt["sms_diagnostics"] = diagnostics(result)
                response_data(result, "sms")
                attempt["state"] = "SMS_SENT"
            except EzMcdError as error:
                attempt["state"] = "VERIFICATION_REQUIRED" if error.code == "official_login_verification_required" else "SMS_STOPPED"
                if error.code == SECURITY_RESTRICTION:
                    attempt["state"] = "OFFICIAL_ACTION_REQUIRED"
                if error.code == "transport_unknown_do_not_retry_writes":
                    attempt["state"] = "SMS_UNKNOWN"
                if isinstance(error, VerificationRequired):
                    attempt["verification"] = error.context
                attempt["error"] = error.code
            self._save(attempt)
        return summary(attempt)

    def _save(self, attempt):
        write_private(self._path(attempt["attempt_id"]), attempt)

    def verify(self, attempt_id, code):
        if not re.fullmatch(r"[0-9]{4,8}", code):
            raise EzMcdError("invalid_sms_code_format")
        with self.store.lock(AUTH_LOCK):
            attempt = read_private(self._path(attempt_id))
            if attempt["state"] not in ("SMS_SENT", "SMS_UNKNOWN"):
                raise EzMcdError("sms_attempt_not_ready_for_verification")
            if time.time() >= attempt["expires_at"]:
                raise EzMcdError("sms_attempt_expired")
            if attempt["device"] != self.device or attempt["binary"] != self.binary:
                raise EzMcdError("sms_attempt_device_changed")
            attempt["state"] = "LOGIN_ATTEMPTED"
            self._save(attempt)
            try:
                result = self.transport.send(MOBILE_LOGIN, {"tel": attempt["cipher_phone"], "code": encrypt_field(code, self.key),
                    "citicRegister": False, "regionCode": "86", "secondPhoneFlag": False})
                attempt["login_diagnostics"] = diagnostics(result)
                user = response_data(result, "login")
                if not isinstance(user, dict) or user.get("newUser"):
                    raise EzMcdError("existing_member_login_required")
                if not isinstance(user.get("tel"), str) or not hmac.compare_digest(user["tel"].lower(), attempt["cipher_phone"]):
                    raise EzMcdError("login_phone_binding_not_verified")
                if any(not isinstance(user.get(k), str) or not user[k] for k in ("sid", "meddyId")):
                    raise EzMcdError("login_session_fields_missing")
                fields = {k: user[k] for k in ("sid", "meddyId", "refreshToken", "routingId", "tid") if isinstance(user.get(k), str) and user[k]}
                fields.update(self.device["fields"])
                attempt["profile"] = {"schema_version": 2, "source": "direct_sms_login", "build_sha256": BUILD,
                    "authenticated_endpoint": MOBILE_LOGIN, "fields": fields,
                    "client_headers": self.device["client_headers"]}
                attempt["state"] = "SESSION_RECEIVED"
            except EzMcdError as error:
                attempt["state"] = "VERIFICATION_REQUIRED" if error.code == "official_login_verification_required" else "LOGIN_STOPPED"
                if error.code == SECURITY_RESTRICTION:
                    attempt["state"] = "OFFICIAL_ACTION_REQUIRED"
                if isinstance(error, VerificationRequired):
                    attempt["verification"] = error.context
                attempt["error"] = error.code
            self._save(attempt)
        return summary(attempt)


    def complete(self, attempt_id, alias):
        """Read-only check and local persistence; may resume after a lost balance reply."""
        from .provider import McdProvider
        with self.store.lock(AUTH_LOCK):
            attempt = read_private(self._path(attempt_id))
            if attempt["state"] == "CONNECTED":
                return summary(attempt)
            if attempt["state"] != "SESSION_RECEIVED":
                raise EzMcdError("no_verified_sms_session_to_connect")
            profile = attempt["profile"]
            McdProvider({"profile": profile, "binary": attempt["binary"]}).wallet()
            profile["verified_endpoint"] = "/bff/member/mwallet/balance"
            identity = fingerprint({"provider": "mcd-cn", "member": profile["fields"]["meddyId"]})
            existing = next((a for a in self.store.accounts() if a["identity"] == identity), None)
            # The same member keeps the existing alias, policy and entire journal.
            target = existing["alias"] if existing else alias
            _enroll(self.store, target, profile, attempt["binary"], None, None, update=bool(existing))
            def mark(record):
                record.update(auth_method="direct_sms", phone_ownership_verified_by_project=True,
                              phone_label=attempt["phone_label"])
                return record
            self.store.modify_account(target, mark)
            attempt.update(state="CONNECTED", account=target)
            attempt.pop("profile", None)
            attempt.pop("cipher_phone", None)
            self._save(attempt)
        return summary(attempt)


def interactive_login(store, alias, binary, source_account=None, resume=None):
    """Phone and OTP are entered on the user's terminal, never as argv or MCP."""
    import getpass
    import sys
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", alias):
        raise EzMcdError("invalid_account_alias")
    if resume:
        if not re.fullmatch(r"[0-9a-f]{32}", resume):
            raise EzMcdError("invalid_login_attempt_id")
        previous = read_private(store.root / "auth" / "attempts" / (resume + ".json"))
        device, binary = previous["device"], previous["binary"]
        result = summary(previous)
        flow = SMSLogin(store, binary, device) if result["state"] in ("SMS_SENT", "SMS_UNKNOWN", "SESSION_RECEIVED") else None
    else:
        if not sys.stdin.isatty():
            raise EzMcdError("sms_login_requires_local_interactive_terminal")
        device = device_context(store, binary, source_account)
        flow = SMSLogin(store, binary, device)
        print("仅登录已注册的本人账号；本次最多发送一次短信，不会下单或付款。", file=sys.stderr)
        phone = getpass.getpass("测试手机号（输入不回显）：")
        result = flow.begin(phone)
        phone = None
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    if result["state"] in ("SMS_SENT", "SMS_UNKNOWN"):
        if not sys.stdin.isatty():
            raise EzMcdError("sms_login_requires_local_interactive_terminal")
        if result["state"] == "SMS_UNKNOWN":
            print("发送结果未知；只有实际收到该手机号的验证码才继续输入。不会重新发送。", file=sys.stderr)
        code = getpass.getpass("短信验证码（输入不回显，勿粘贴到聊天）：")
        result = flow.verify(result["attempt_id"], code.strip())
        code = None
    if result["state"] == "SESSION_RECEIVED":
        result = flow.complete(result["attempt_id"], alias)
    if result["state"] == "VERIFICATION_REQUIRED":
        print("官方要求额外安全验证，本次已停止，没有自动重发。", file=sys.stderr)
    if result["state"] == "OFFICIAL_ACTION_REQUIRED":
        print("官方返回安全限制（707053），未确认短信发送成功。请在官方 App 核对登录并完成必要验证。", file=sys.stderr)
    return result
