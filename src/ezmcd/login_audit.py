"""Passive login-request comparison; outputs structure and booleans, no credentials.

The caller owns any live capture. This module neither sends nor changes requests,
and never returns phone/code ciphertext, SDK identifiers, signatures or sessions.
"""
from email.utils import parsedate_to_datetime
import hmac
import json
import re
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .errors import EzMcdError
from .protocol.login import login_key
from .protocol.signing import constants, sign_v4
from .sms import CLIENT_HEADERS, MOBILE_LOGIN, SMS_SEND, diagnostics

ROUTES = {SMS_SEND: {"tel", "type", "regionCode"},
          MOBILE_LOGIN: {"tel", "code", "citicRegister", "regionCode", "secondPhoneFlag"}}
HEADER_NAMES = frozenset(("user-agent", "content-type", "accept", "ct", "v", "language", "sv", "token", "d",
    "sid", "meddyid", "routingid", "tid", "st", "nonce", "authorization", "x-hmac-digest", "x-mcd-gw-v",
    "x-mcd-sign", "x-mcd-sec-verify", "biz_from", "biz_scenario", "p", "x-b3-traceid", "x-b3-spanid"))


def login_route(method, scheme, host, path):
    parsed = urlsplit(path)
    return (method == "POST" and scheme == "https" and host == "api.mcd.cn"
            and not parsed.scheme and not parsed.netloc and parsed.path in ROUTES)


def _decoded_shape(value, key, phone=False):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{32}){1,8}", value):
        return {"cipher_format_valid": False, "plaintext_format_valid": False}
    try:
        cipher = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = cipher.update(bytes.fromhex(value)) + cipher.finalize()
        unpad = padding.PKCS7(128).unpadder()
        plain = (unpad.update(padded) + unpad.finalize()).decode("utf-8")
        valid = bool(re.fullmatch(r"1[3-9][0-9]{9}" if phone else r"[0-9]{4,8}", plain))
    except (ValueError, UnicodeError):
        valid = False
    return {"cipher_format_valid": True, "plaintext_format_valid": valid}


def inspect_login_exchange(request, response, binary, *, reference_device=None, cipher_phone=None):
    if not login_route(request.get("method"), request.get("scheme"), request.get("host"), request.get("path", "")):
        raise EzMcdError("login_audit_out_of_scope")
    path = urlsplit(request["path"])
    raw = request.get("body", b"")
    raw = raw.encode("utf-8") if isinstance(raw, str) else raw
    response_raw = response.get("body", b"")
    if len(raw) > 65536 or not isinstance(response_raw, dict) and len(response_raw) > 524288:
        raise EzMcdError("login_audit_body_too_large")
    try:
        body = json.loads(raw)
        payload = response_raw if isinstance(response_raw, dict) else json.loads(response_raw)
    except (ValueError, UnicodeError):
        raise EzMcdError("login_audit_json_required") from None
    if not isinstance(body, dict) or not isinstance(payload, dict):
        raise EzMcdError("login_audit_json_object_required")
    headers = {k.lower(): v for k, v in request.get("headers", {}).items()}
    if any(not isinstance(v, str) for v in headers.values()):
        raise EzMcdError("login_audit_invalid_headers")
    ak, secret, _, _ = constants(binary)
    key = login_key(binary)
    signed = False
    if headers.get("sv") == "v4":
        parts = headers.get("authorization", "").split("#")
        if len(parts) == 6:
            try:
                expected = sign_v4(headers, ak, secret, parsedate_to_datetime(parts[4]), "POST", path.path, path.query, raw)
                signed = all(hmac.compare_digest(v, headers.get(k, "")) for k, v in expected.items())
            except (ValueError, TypeError, OverflowError):
                pass
    expected_fields = ROUTES[path.path]
    result = {"path": path.path, "method": "POST", "query_present": bool(path.query),
        "headers_present": sorted(HEADER_NAMES.intersection(headers)),
        "unrecognized_header_count": len(set(headers) - HEADER_NAMES),
        "client_headers_match_script": {k: headers.get(k) == v for k, v in CLIENT_HEADERS.items()},
        "user_agent_matches_script": headers.get("user-agent") == "McdApp (iOS)",
        "signature_version": headers.get("sv") if headers.get("sv") in ("v4", "v5") else "other",
        "v4_signature_verified": signed, "sdk_device_header_present": bool(headers.get("d")),
        "device_token_present": bool(headers.get("token")),
        "security_verification_header_present": bool(headers.get("x-mcd-sec-verify")),
        "body_fields_present": sorted(expected_fields.intersection(body)),
        "extra_body_field_count": len(set(body) - expected_fields),
        "region_code_matches_script": body.get("regionCode") == "86",
        "phone": _decoded_shape(body.get("tel"), key, phone=True),
        "response": diagnostics({"http_status": response.get("status"), "response": payload})}
    business = ("1011", "500") if path.path == SMS_SEND else ("1004", "500")
    result["business_headers_match_script"] = headers.get("biz_from") == business[0] and headers.get("biz_scenario") == business[1]
    if path.path == SMS_SEND:
        result["sms_type_matches_script"] = type(body.get("type")) is int and body["type"] == 1
    else:
        result["code"] = _decoded_shape(body.get("code"), key)
        result["login_flags_match_script"] = body.get("citicRegister") is False and body.get("secondPhoneFlag") is False
    if cipher_phone is not None:
        actual = body.get("tel")
        result["matches_test_phone"] = isinstance(actual, str) and hmac.compare_digest(actual, cipher_phone)
    if reference_device:
        fields = reference_device.get("fields", {})
        result["matches_imported_device"] = {header: bool(fields.get(field) and headers.get(header)
            and hmac.compare_digest(fields[field], headers[header]))
            for field, header in (("deviceToken", "token"), ("riskDeviceId", "d"))}
    return result
