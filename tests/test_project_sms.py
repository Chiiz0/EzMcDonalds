import ctypes
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ezmcd.accounts import public_account
from ezmcd.errors import EzMcdError
from ezmcd.models import Policy, fingerprint
from ezmcd.protocol.login import encrypt_field
from ezmcd.protocol.signing import BUILD
from ezmcd.sms import CLIENT_HEADERS, LoginTransport, MOBILE_LOGIN, SMS_SEND, SMSLogin, device_context, diagnostics, interactive_login, normalize_phone
from ezmcd.storage import Store, read_private, write_private

KEY = b"0123456789abcdef"
PHONE = "13800000000"
DEVICE = {"build_sha256": BUILD, "fields": {"deviceToken": "a" * 40, "riskDeviceId": "synthetic-sdk-device"}, "client_headers": dict(CLIENT_HEADERS)}


def success(value=None):
    return {"http_status": 200, "response": {"success": True, "code": 200, "data": value}}


class SMSTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        self.http = Mock()
        self.flow = SMSLogin(self.store, "unused", DEVICE, transport=self.http, key=KEY)

    def received(self, **overrides):
        self.http.send.return_value = success()
        attempt = self.flow.begin(PHONE)
        user = {"sid": "new-synthetic-sid", "meddyId": "test-member", "refreshToken": "new-synthetic-refresh",
                "newUser": False, "tel": encrypt_field(PHONE, KEY)} | overrides
        self.http.send.return_value = success(user)
        return self.flow.verify(attempt["attempt_id"], "123456")

    def test_encrypted_requests_only_and_no_otp_in_persistent_state(self):
        result = self.received()
        self.assertEqual(result["state"], "SESSION_RECEIVED")
        sent = self.http.send.call_args_list
        self.assertEqual(sent[0].args, (SMS_SEND, {"tel": encrypt_field(PHONE, KEY), "type": 1, "regionCode": "86"}))
        self.assertEqual(sent[1].args, (MOBILE_LOGIN, {"tel": encrypt_field(PHONE, KEY), "code": encrypt_field("123456", KEY),
            "citicRegister": False, "regionCode": "86", "secondPhoneFlag": False}))
        for path in self.store.root.rglob("*.json"):
            self.assertNotIn(PHONE, path.read_text())
            self.assertNotIn('"123456"', path.read_text())
        self.assertNotIn("synthetic-sid", json.dumps(result))
        with self.assertRaisesRegex(EzMcdError, "not_ready"):
            self.flow.verify(result["attempt_id"], "123456")
        self.assertEqual(self.http.send.call_count, 2)

    def test_sms_unknown_is_not_resent_and_claim_survives_restart(self):
        self.http.send.side_effect = EzMcdError("transport_unknown_do_not_retry_writes")
        first = self.flow.begin(PHONE)
        self.assertEqual(first["state"], "SMS_UNKNOWN")
        restarted = SMSLogin(self.store, "unused", DEVICE, transport=self.http, key=KEY)
        with self.assertRaisesRegex(EzMcdError, "sms_cooldown"):
            restarted.begin(PHONE)
        self.assertEqual(self.http.send.call_count, 1)

    def test_challenge_never_advances_to_code_verification(self):
        self.http.send.return_value = {"http_status": 200, "response": {"success": False, "code": 420,
            "data": {"securityContext": {"securityToken": "private-challenge"}}}}
        result = self.flow.begin(PHONE)
        self.assertEqual(result["state"], "VERIFICATION_REQUIRED")
        self.assertNotIn("private-challenge", json.dumps(result))
        with self.assertRaisesRegex(EzMcdError, "not_ready"):
            self.flow.verify(result["attempt_id"], "123456")
        self.assertEqual(self.http.send.call_count, 1)

    def test_wrong_phone_and_new_member_cannot_be_enrolled(self):
        result = self.received(tel=encrypt_field("13900000000", KEY))
        self.assertEqual(result["error"], "login_phone_binding_not_verified")
        self.assertEqual(self.store.accounts(), [])
        with self.assertRaisesRegex(EzMcdError, "no_verified_sms_session"):
            self.flow.complete(result["attempt_id"], "test")

    def test_login_timeout_has_no_automatic_code_retry(self):
        self.http.send.return_value = success()
        result = self.flow.begin(PHONE)
        self.http.send.side_effect = EzMcdError("transport_unknown_do_not_retry_writes")
        result = self.flow.verify(result["attempt_id"], "123456")
        self.assertEqual(result["state"], "LOGIN_STOPPED")
        with self.assertRaisesRegex(EzMcdError, "not_ready"):
            self.flow.verify(result["attempt_id"], "123456")
        self.assertEqual(self.http.send.call_count, 2)

    def test_expired_attempt_stops_before_submitting_code(self):
        self.http.send.return_value = success()
        with patch("ezmcd.sms.time.time", return_value=1000):
            result = self.flow.begin(PHONE)
        with patch("ezmcd.sms.time.time", return_value=1301):
            with self.assertRaisesRegex(EzMcdError, "attempt_expired"):
                self.flow.verify(result["attempt_id"], "123456")
        self.assertEqual(self.http.send.call_count, 1)

    def test_hourly_send_limit_persists(self):
        self.http.send.return_value = success()
        for when in (1000, 1070, 1140):
            with patch("ezmcd.sms.time.time", return_value=when):
                self.flow.begin(PHONE)
        with patch("ezmcd.sms.time.time", return_value=1210):
            with self.assertRaisesRegex(EzMcdError, "hourly_limit"):
                self.flow.begin(PHONE)
        self.assertEqual(self.http.send.call_count, 3)

    def test_new_user_response_is_not_reported_as_existing_member_success(self):
        result = self.received(newUser=True)
        self.assertEqual(result["state"], "LOGIN_STOPPED")
        self.assertEqual(result["error"], "existing_member_login_required")
        self.assertEqual(self.store.accounts(), [])

    def test_complete_reuses_identity_preserves_policy_and_does_not_reuse_runtime(self):
        identity = fingerprint({"provider": "mcd-cn", "member": "test-member"})
        self.store.add_account({"alias": "existing", "identity": identity, "provider": "mcd-cn",
            "policy": Policy(max_order_cents=1200).model_dump(), "profile": {},
            "runtime": {"observed_at": 1, "device_hex": "do-not-copy"}})
        self.store.claim("previous-order", "consume")
        result = self.received()
        with patch("ezmcd.provider.McdProvider.wallet", return_value={"balance_cents": 1000}), \
                patch("ezmcd.accounts.constants", return_value=("a", "s", (), ())):
            connected = self.flow.complete(result["attempt_id"], "new-name")
            repeated = self.flow.complete(result["attempt_id"], "new-name")
        self.assertEqual(connected, repeated)
        self.assertEqual(connected["account"], "existing")
        account = self.store.account("existing")
        self.assertEqual(account["policy"]["max_order_cents"], 1200)
        self.assertIsNone(account["runtime"])
        self.assertEqual(public_account(account)["auth_method"], "direct_sms")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 1)
        self.assertNotIn("profile", read_private(self.flow._path(result["attempt_id"])))

    def test_read_only_check_failure_keeps_session_for_resume_without_new_sms(self):
        result = self.received()
        with patch("ezmcd.provider.McdProvider.wallet", side_effect=EzMcdError("temporary_query_failure")):
            with self.assertRaisesRegex(EzMcdError, "temporary_query_failure"):
                self.flow.complete(result["attempt_id"], "test")
        saved = read_private(self.flow._path(result["attempt_id"]))
        self.assertEqual(saved["state"], "SESSION_RECEIVED")
        self.assertNotIn("verified_endpoint", saved["profile"])
        self.assertEqual(self.http.send.call_count, 2)

    def test_diagnostics_do_not_keep_echoed_phone_code_or_session(self):
        payload = {"http_status": 200, "response": {"success": False, "code": 707053,
            "message": "请求失败，手机号13800000000，验证码123456，sid=synthetic-secret-session。",
            "data": {"synthetic-secret-key": "synthetic-secret-value", "securityContext": {"token": "secret"}}}}
        result = diagnostics(payload)
        self.assertEqual(result["code"], "707053")
        self.assertIn("请求失败", result["message_zh"])
        self.assertEqual(result["data_fields"], ["securityContext"])
        for secret in (PHONE, "123456", "synthetic-secret", "token"):
            self.assertNotIn(secret, json.dumps(result))

    def test_security_restricted_sms_requires_official_action_and_cannot_submit_otp(self):
        self.http.send.return_value = {"http_status": 200, "response": {"success": False, "code": 707053,
            "message": "请求失败"}}
        result = self.flow.begin(PHONE)
        self.assertEqual(result["state"], "OFFICIAL_ACTION_REQUIRED")
        self.assertEqual(result["error"], "official_login_security_restriction_707053")
        self.assertEqual(result["diagnostics"]["message_zh"], "请求失败")
        self.assertEqual(read_private(self.flow._path(result["attempt_id"]))["sms_diagnostics"]["message_zh"], "请求失败")
        with self.assertRaisesRegex(EzMcdError, "not_ready"):
            self.flow.verify(result["attempt_id"], "123456")
        self.assertEqual(self.http.send.call_count, 1)

    def test_security_restriction_during_login_cannot_create_session_or_retry(self):
        self.http.send.return_value = success()
        attempt = self.flow.begin(PHONE)
        self.http.send.return_value = {"http_status": 200, "response": {"success": False, "code": "707053"}}
        result = self.flow.verify(attempt["attempt_id"], "123456")
        self.assertEqual(result["state"], "OFFICIAL_ACTION_REQUIRED")
        self.assertNotIn("profile", read_private(self.flow._path(result["attempt_id"])))
        with self.assertRaisesRegex(EzMcdError, "not_ready"):
            self.flow.verify(result["attempt_id"], "123456")
        self.assertEqual(self.http.send.call_count, 2)

    def test_resume_legacy_security_rejection_does_not_prompt_read_key_or_send(self):
        attempt_id = "a" * 32
        write_private(self.flow._path(attempt_id), {"attempt_id": attempt_id, "state": "SMS_STOPPED",
            "error": "sms_rejected_707053", "device": DEVICE, "binary": "absent-app"})
        with patch("ezmcd.sms.SMSLogin") as factory, patch("getpass.getpass") as prompt, \
                contextlib.redirect_stderr(io.StringIO()):
            result = interactive_login(self.store, "test", "absent-app", resume=attempt_id)
        self.assertEqual(result["state"], "OFFICIAL_ACTION_REQUIRED")
        self.assertIn("official", result["next_step"])
        factory.assert_not_called()
        prompt.assert_not_called()

    def test_no_account_session_headers_and_no_nonlogin_endpoint(self):
        dirty = dict(DEVICE, client_headers=dict(CLIENT_HEADERS, sid="do-not-send", meddyid="do-not-send"))
        transport = LoginTransport("unused", dirty)
        with patch("ezmcd.sms.constants", return_value=("test-ak", "test-secret", (), ())), \
                patch("ezmcd.sms.Transport._open", return_value=success()) as send:
            transport.send(SMS_SEND, {"tel": "encrypted"})
            headers = send.call_args.args[2]
            self.assertEqual(headers["d"], DEVICE["fields"]["riskDeviceId"])
            for key in ("sid", "meddyid", "routingid", "tid", "X-Mcd-Sec-Verify"):
                self.assertNotIn(key, headers)
            with self.assertRaisesRegex(EzMcdError, "endpoint_not_supported"):
                transport.send("/bff/order/orders", {})
            self.assertEqual(send.call_count, 1)

    def test_missing_official_device_context_stops_before_any_sms(self):
        with patch("ezmcd.sms.constants"):
            with self.assertRaisesRegex(EzMcdError, "device_context_required"):
                device_context(self.store, "unused")
        with self.assertRaisesRegex(EzMcdError, "device_context_incomplete"):
            SMSLogin(self.store, "unused", dict(DEVICE, fields={"deviceToken": "a" * 40}), transport=self.http, key=KEY)
        self.http.send.assert_not_called()
        self.assertFalse((self.store.root / "auth/device.json").exists())

    def test_official_device_selection_never_copies_member_or_session(self):
        profile = {"fields": dict(DEVICE["fields"], sid="private-sid", meddyId="private-member", refreshToken="private-refresh"),
                   "client_headers": dict(CLIENT_HEADERS, sid="private-header-sid")}
        self.store.add_account({"alias": "source", "identity": "b" * 64, "provider": "mcd-cn", "profile": profile})
        with patch("ezmcd.sms.constants"):
            device = device_context(self.store, "unused", "source")
        self.assertEqual(device["source"], "observed_official_device")
        self.assertEqual(device["fields"], DEVICE["fields"])
        self.assertEqual(device["client_headers"], CLIENT_HEADERS)
        self.assertNotIn("private-", json.dumps(device))

    def test_phone_validation_does_not_accept_paths_or_foreign_numbers(self):
        self.assertEqual(normalize_phone("+86 138 0000 0000"), PHONE)
        for value in ("../13800000000", "+12125551234", "13800000000\n13900000000"):
            with self.assertRaises(EzMcdError):
                normalize_phone(value)

    @unittest.skipUnless(sys.platform == "darwin", "CommonCrypto comparison requires macOS")
    def test_wire_encryption_matches_commoncrypto_options_used_by_official_client(self):
        library = ctypes.CDLL("/usr/lib/system/libcommonCrypto.dylib")
        library.CCCrypt.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        library.CCCrypt.restype = ctypes.c_int
        for value in (PHONE, "123456", "abcdef" * 8):
            raw = value.encode(); output = ctypes.create_string_buffer(len(raw) + 16); written = ctypes.c_size_t()
            status = library.CCCrypt(0, 0, 3, KEY, 16, None, raw, len(raw), output, len(output), ctypes.byref(written))
            self.assertEqual(status, 0)
            self.assertEqual(encrypt_field(value, KEY), output.raw[:written.value].hex())
