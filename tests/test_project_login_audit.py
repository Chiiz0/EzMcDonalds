from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from ezmcd.errors import EzMcdError
from ezmcd.login_audit import inspect_login_exchange
from ezmcd.protocol.login import encrypt_field
from ezmcd.protocol.signing import sign_v4
from ezmcd.sms import CLIENT_HEADERS, MOBILE_LOGIN, SMS_SEND


class LoginAuditTests(unittest.TestCase):
    key = b"0123456789abcdef"

    def sample(self, login=False):
        phone = encrypt_field("13800000000", self.key)
        body = {"tel": phone, "regionCode": "86"}
        path = MOBILE_LOGIN if login else SMS_SEND
        body.update({"code": encrypt_field("123456", self.key), "citicRegister": False,
                     "secondPhoneFlag": False} if login else {"type": 1})
        raw = json.dumps(body, separators=(",", ":")).encode()
        headers = dict(CLIENT_HEADERS, sv="v4", token="private-device", d="private-sdk-result",
            biz_from="1004" if login else "1011", biz_scenario="500", **{"user-agent": "McdApp (iOS)"})
        headers.update(sign_v4(headers, "dummy-ak", "dummy-secret", datetime(2026, 1, 1, tzinfo=timezone.utc), "POST", path, body=raw))
        request = {"method": "POST", "scheme": "https", "host": "api.mcd.cn", "path": path, "headers": headers, "body": raw}
        response = {"status": 200, "body": {"success": True, "code": 200,
            "data": {"sid": "private-session", "refreshToken": "private-refresh", "tel": phone}}}
        return request, response

    def audit(self, request, response):
        with patch("ezmcd.login_audit.constants", return_value=("dummy-ak", "dummy-secret", (), ())), \
                patch("ezmcd.login_audit.login_key", return_value=self.key):
            return inspect_login_exchange(request, response, "unused", reference_device={"fields": {
                "deviceToken": "private-device", "riskDeviceId": "private-sdk-result"}},
                cipher_phone=encrypt_field("13800000000", self.key))

    def test_observed_request_is_verified_without_persisting_authentication_material(self):
        for login in (False, True):
            request, response = self.sample(login)
            result = self.audit(request, response)
            self.assertTrue(result["v4_signature_verified"])
            self.assertTrue(result["sdk_device_header_present"])
            self.assertTrue(result["matches_test_phone"])
            self.assertTrue(result["phone"]["plaintext_format_valid"])
            self.assertEqual(result["matches_imported_device"], {"token": True, "d": True})
            text = json.dumps(result)
            for secret in ("13800000000", "123456", "private-", "dummy-", encrypt_field("123456", self.key), encrypt_field("13800000000", self.key)):
                self.assertNotIn(secret, text)

    def test_wrong_origin_or_nonlogin_endpoint_is_not_captured(self):
        request, response = self.sample()
        for overrides in ({"host": "api.mcd.cn.example.org"}, {"scheme": "http"}, {"method": "GET"},
                          {"path": "/bff/order/orders"}, {"path": "https://example.org" + SMS_SEND}):
            with self.assertRaisesRegex(EzMcdError, "out_of_scope"):
                self.audit(request | overrides, response)

    def test_missing_device_header_and_tampered_body_are_visible(self):
        request, response = self.sample()
        request["headers"].pop("d")
        body = json.loads(request["body"])
        body["regionCode"] = "+86"
        body["private-unknown-field-name"] = "private-unknown-field-value"
        request["body"] = json.dumps(body).encode()
        result = self.audit(request, response)
        self.assertFalse(result["sdk_device_header_present"])
        self.assertFalse(result["region_code_matches_script"])
        self.assertFalse(result["v4_signature_verified"])
        self.assertEqual(result["extra_body_field_count"], 1)
        self.assertNotIn("private-", json.dumps(result))

    def test_malformed_payloads_fail_without_echoing_bodies(self):
        request, response = self.sample()
        for raw in (b"private-not-json", b"[]", b"a" * 65537):
            with self.assertRaises(EzMcdError) as error:
                self.audit(request | {"body": raw}, response)
            self.assertNotIn("private", str(error.exception))
