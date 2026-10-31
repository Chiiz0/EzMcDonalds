from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ezmcd.accounts import public_account, revoke_local
from ezmcd.errors import EzMcdError
from ezmcd.models import Policy, fingerprint
from ezmcd.service import Service
from ezmcd.sessions import REFRESH, refresh_account
from ezmcd.storage import Store
from ezmcd.transport import API, Transport


def success(**fields):
    return {"http_status": 200, "response": {"success": True, "data": {
        "sid": "synthetic-new-sid", "refreshToken": "synthetic-new-refresh"} | fields}}


class SessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.record = {"alias": "test", "identity": fingerprint({"member": "synthetic-member"}),
            "provider": "mcd-cn", "binary": "unused", "phone_label": "138****0000",
            "auth_method": "direct_sms", "policy": Policy().model_dump(),
            "runtime": {"observed_at": time.time(), "device_hex": "A" * 32, "environment_hex": "b" * 16},
            "profile": {"fields": {"sid": "synthetic-old-sid", "refreshToken": "synthetic-old-refresh",
                "meddyId": "synthetic-member", "deviceToken": "synthetic-device", "riskDeviceId": "synthetic-risk"}}}
        self.store.add_account(self.record)
        self.transport = Mock()
        self.transport.send.return_value = success()
        self.transport_factory = Mock(return_value=self.transport)
        self.provider = Mock()
        self.provider.wallet.return_value = {"balance_cents": 1234}
        self.provider_factory = Mock(return_value=self.provider)
        self.clock = Mock(return_value=time.time())

    def refresh(self):
        return refresh_account(self.store, "test", transport_factory=self.transport_factory,
                               provider_factory=self.provider_factory, clock=self.clock)

    def test_rotation_claim_precedes_network_and_preserves_identity_policy_journal(self):
        def respond(*args, **kwargs):
            self.assertEqual(self.store.account("test")["session_refresh"]["state"], "CLAIMED")
            with self.assertRaisesRegex(EzMcdError, "account_busy"):
                with self.store.lock(self.record["identity"]):
                    pass
            return success()
        self.transport.send.side_effect = respond
        result = self.refresh()
        current = self.store.account("test")
        self.assertEqual(current["profile"]["fields"]["sid"], "synthetic-new-sid")
        for key in ("identity", "alias", "phone_label", "policy", "auth_method"):
            self.assertEqual(current[key], self.record[key])
        self.assertIsNone(current["runtime"])
        self.assertNotIn("candidate_fields", current["session_refresh"])
        self.assertEqual(result["wallet_balance_cny"], "12.34")
        self.assertTrue(result["session_refreshed"])
        self.assertFalse(result["order_created"])
        self.assertNotIn("synthetic-", json.dumps(result))
        self.transport.send.assert_called_once_with("POST", REFRESH, body={"refreshToken": "synthetic-old-refresh"})
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 0)

    def test_repeat_within_interval_only_verifies_and_later_rotation_is_allowed(self):
        self.refresh()
        result = self.refresh()
        self.assertFalse(result["session_refreshed"])
        self.assertEqual(self.transport.send.call_count, 1)
        self.assertEqual(self.provider.wallet.call_count, 2)
        self.clock.return_value += 86401
        self.refresh()
        self.assertEqual(self.transport.send.call_count, 2)
        self.assertEqual(self.transport.send.call_args.kwargs["body"], {"refreshToken": "synthetic-new-refresh"})

    def test_lost_response_never_repeats_refresh_after_restart(self):
        self.transport.send.side_effect = EzMcdError("transport_unknown_do_not_retry_writes")
        with self.assertRaises(EzMcdError):
            self.refresh()
        self.store = Store(self.store.root)
        with self.assertRaisesRegex(EzMcdError, "already_attempted"):
            self.refresh()
        self.assertEqual(self.transport.send.call_count, 1)
        self.provider.wallet.assert_not_called()
        with self.assertRaisesRegex(EzMcdError, "session_refresh_incomplete"):
            Service(self.store, self.provider_factory).provider("test")

    def test_process_interrupt_keeps_claim_and_does_not_resend(self):
        self.transport.send.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.refresh()
        self.assertEqual(self.store.account("test")["session_refresh"]["state"], "CLAIMED")
        with self.assertRaisesRegex(EzMcdError, "already_attempted"):
            self.refresh()
        self.assertEqual(self.transport.send.call_count, 1)

    def test_verification_failure_preserves_candidate_and_resume_is_read_only(self):
        self.provider.wallet.side_effect = EzMcdError("transport_unknown_do_not_retry_writes")
        with self.assertRaisesRegex(EzMcdError, "resume_verification"):
            self.refresh()
        current = self.store.account("test")
        self.assertEqual(current["profile"]["fields"]["sid"], "synthetic-old-sid")
        self.assertEqual(current["session_refresh"]["candidate_fields"]["sid"], "synthetic-new-sid")
        self.provider.wallet.side_effect = None
        self.assertTrue(self.refresh()["session_refreshed"])
        self.assertEqual(self.transport.send.call_count, 1)
        self.assertEqual(self.store.account("test")["session_refresh"]["state"], "REFRESHED")

    def test_failed_final_commit_resumes_without_rotating_twice(self):
        with patch("ezmcd.storage.write_private", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.refresh()
        self.assertEqual(self.store.account("test")["session_refresh"]["state"], "CANDIDATE")
        self.refresh()
        self.assertEqual(self.transport.send.call_count, 1)

    def test_invalid_response_or_changed_member_never_commits_or_retries(self):
        for override in ({"sid": "bad\r\nheader"}, {"refreshToken": None}, {"meddyId": "other-member"}):
            with self.subTest(override=override):
                self.store.update_account(deepcopy(self.record))
                self.transport.send.return_value = success(**override)
                count = self.transport.send.call_count
                with self.assertRaisesRegex(EzMcdError, "response_invalid"):
                    self.refresh()
                with self.assertRaisesRegex(EzMcdError, "already_attempted"):
                    self.refresh()
                self.assertEqual(self.transport.send.call_count, count + 1)
                self.assertEqual(self.store.account("test")["profile"], self.record["profile"])
        self.provider.wallet.assert_not_called()

    def test_challenge_stops_without_saving_challenge_credentials(self):
        self.transport.send.return_value = {"http_status": 200, "response": {"code": 420,
            "data": {"securityContext": {"token": "synthetic-challenge-secret"}}}}
        with self.assertRaisesRegex(EzMcdError, "verification_required"):
            self.refresh()
        self.assertNotIn("synthetic-challenge-secret", json.dumps(self.store.account("test")))
        self.provider.wallet.assert_not_called()

    def test_unchanged_sid_preserves_original_runtime_timestamp(self):
        self.transport.send.return_value = success(sid="synthetic-old-sid")
        self.refresh()
        self.assertEqual(self.store.account("test")["runtime"], self.record["runtime"])

    def test_new_login_clears_old_refresh_block_with_new_credentials(self):
        self.transport.send.side_effect = EzMcdError("transport_unknown_do_not_retry_writes")
        with self.assertRaises(EzMcdError):
            self.refresh()
        updated = self.store.account("test")
        updated["profile"]["fields"]["sid"] = "synthetic-relogin-sid"
        updated["profile"]["fields"]["refreshToken"] = "synthetic-relogin-refresh"
        self.store.update_account(updated)
        self.transport.send.side_effect = None
        self.refresh()
        self.assertEqual(self.transport.send.call_count, 2)

    def test_revoke_removes_candidate_credentials_and_prevents_rotation(self):
        self.provider.wallet.side_effect = EzMcdError("transport_unknown_do_not_retry_writes")
        with self.assertRaises(EzMcdError):
            self.refresh()
        revoke_local(self.store, "test")
        self.assertNotIn("synthetic-new-refresh", json.dumps(self.store.account("test")))
        with self.assertRaisesRegex(EzMcdError, "revoked"):
            self.refresh()
        self.assertEqual(self.transport.send.call_count, 1)

    def test_missing_refresh_token_stops_before_network(self):
        self.record["profile"]["fields"].pop("refreshToken")
        self.store.update_account(self.record)
        with self.assertRaisesRegex(EzMcdError, "refresh_token_missing"):
            self.refresh()
        self.transport_factory.assert_not_called()

    def test_readiness_separates_session_runtime_and_policy(self):
        result = public_account(self.record)
        self.assertFalse(result["readiness"]["can_browse"])
        self.assertTrue(result["device_context_available"])
        self.assertTrue(result["session_refresh_supported"])
        result = public_account(self.record, session_verified=True)
        self.assertTrue(result["readiness"]["can_browse"])
        self.assertEqual(result["readiness"]["checkout_blockers"], ["checkout_disabled"])
        self.record["policy"]["allow_checkout"] = True
        self.assertTrue(public_account(self.record, session_verified=True)["readiness"]["checkout_prerequisites_met"])
        self.record["policy"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.assertIn("checkout_policy_expired", public_account(self.record)["readiness"]["checkout_blockers"])

    def test_refresh_transport_is_fixed_origin_signed_v4(self):
        transport = Transport(self.record)
        transport.signer = Mock()
        transport.signer.headers.return_value = {"synthetic": "header"}
        with patch.object(Transport, "_open", return_value=success()) as open_request:
            transport.send("POST", REFRESH, body={"refreshToken": "synthetic-old-refresh"})
        self.assertEqual(open_request.call_args.args[1], API + REFRESH)
        self.assertEqual(transport.signer.headers.call_args.args[4], ("1006", "500"))
        self.assertFalse(transport.signer.headers.call_args.kwargs["v5"])


if __name__ == "__main__":
    unittest.main()
