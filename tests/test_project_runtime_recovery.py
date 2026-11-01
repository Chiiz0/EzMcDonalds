from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ezmcd.accounts import _enroll, public_account, revoke_local
from ezmcd.diagnostics import doctor
from ezmcd.errors import EzMcdError
from ezmcd.models import Policy, fingerprint
from ezmcd.protocol.signing import BUILD
from ezmcd.runtime_recovery import device_context, fresh_evidence, restore_runtime, retain_candidate
from ezmcd.service import Service
from ezmcd.sessions import refresh_account
from ezmcd.storage import Store


class RuntimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.now = time.time()
        self.record = {"alias": "mine", "identity": fingerprint({"provider": "mcd-cn", "member": "synthetic-member"}),
            "provider": "mcd-cn", "binary": "unused", "policy": Policy().model_dump(),
            "runtime": {"observed_at": self.now - 100, "device_hex": "A" * 32, "environment_hex": "b" * 16},
            "profile": {"build_sha256": BUILD, "verified_endpoint": "/bff/member/mwallet/balance",
                "client_headers": {"ct": "101", "v": "synthetic-version", "language": "cn"},
                "fields": {"sid": "synthetic-old-session", "refreshToken": "synthetic-refresh",
                    "meddyId": "synthetic-member", "deviceToken": "synthetic-device", "riskDeviceId": "synthetic-risk"}}}
        self.store.add_account(self.record)
        for stage in ("order", "preorder", "consume"):
            self.store.claim("prior-purchase", stage)
        self.http = Mock()
        self.http.verify_runtime.return_value = {"http_status": 200, "response": {"success": True, "data": {"list": [{"orderId": "private-do-not-return"}]}}}
        self.factory = Mock(return_value=self.http)
        self.clock = Mock(return_value=self.now)

    def rotate(self, sid="synthetic-new-session", changes=None):
        profile = deepcopy(self.store.account("mine")["profile"])
        profile["fields"]["sid"] = sid
        profile["fields"].pop("refreshToken", None)
        if changes: changes(profile)
        with patch("ezmcd.accounts.constants"):
            return _enroll(self.store, "mine", profile, "unused", None, None, update=True)

    def restore(self):
        return restore_runtime(self.store, "mine", transport_factory=self.factory, clock=self.clock)

    def snapshot(self):
        with self.store.connect() as db:
            return {t: [tuple(r) for r in db.execute("SELECT * FROM " + t)]
                    for t in ("quotes", "attempts", "writes", "request_keys")}

    def test_session_change_keeps_inactive_evidence_until_fixed_read_verifies(self):
        before = self.snapshot()
        view = self.rotate()
        changed = self.store.account("mine")
        self.assertIsNone(changed["runtime"])
        self.assertTrue(view["runtime_recovery_available"])
        self.assertFalse(view["runtime_available"])
        self.assertIn("runtime_missing_or_stale", view["readiness"]["checkout_blockers"])
        self.assertEqual(changed["runtime_candidate"]["evidence"], self.record["runtime"])
        self.assertNotIn("synthetic-old-session", json.dumps(changed))
        result = self.restore()
        used = self.factory.call_args.args[0]
        self.assertEqual(used["profile"]["fields"]["sid"], "synthetic-new-session")
        self.http.verify_runtime.assert_called_once_with()
        self.http.send.assert_not_called()
        current = self.store.account("mine")
        self.assertEqual(current["runtime"]["observed_at"], self.record["runtime"]["observed_at"])
        self.assertNotIn("runtime_candidate", current)
        self.assertEqual(current["policy"], self.record["policy"])
        self.assertEqual(current["profile"], changed["profile"])
        self.assertEqual(before, self.snapshot())
        self.assertTrue(result["runtime_restored"])
        self.assertFalse(result["cross_day_validity_verified"])
        self.assertIn("checkout_disabled", result["readiness"]["checkout_blockers"])
        for private in ("synthetic-", "private-do-not-return", '"device_hex"', '"environment_hex"', '"runtime_candidate"'):
            self.assertNotIn(private, json.dumps(result))

    def test_restart_and_repeat_verify_same_evidence_without_renewing_it(self):
        self.rotate(); first = self.restore()
        before = self.store.account("mine")
        self.store = Store(self.store.root)
        self.clock.return_value += 100
        repeated = self.restore()
        self.assertTrue(repeated["repeated"])
        self.assertFalse(repeated["runtime_restored"])
        self.assertEqual(before, self.store.account("mine"))
        self.assertEqual(self.http.verify_runtime.call_count, 2)
        self.assertFalse(first["repeated"])
        self.clock.return_value += 86400
        with self.assertRaisesRegex(EzMcdError, "previous_runtime_unavailable"):
            self.restore()
        self.assertEqual(self.http.verify_runtime.call_count, 2)

    def test_failed_http_schema_or_network_never_activates_or_discards_candidate(self):
        self.rotate(); before = self.store.account("mine")
        for reply in ({"http_status": 401, "response": {"success": False}},
                      {"http_status": 200, "response": {"success": True, "data": {}}}):
            self.http.verify_runtime.return_value = reply
            with self.assertRaises(EzMcdError): self.restore()
            self.assertEqual(before, self.store.account("mine"))
        self.http.verify_runtime.side_effect = EzMcdError("transport_read_failed")
        with self.assertRaises(EzMcdError): self.restore()
        self.assertEqual(before, self.store.account("mine"))

    def test_original_age_boundary_and_expiry_during_get_do_not_extend_evidence(self):
        self.rotate(); before = self.store.account("mine")
        observation = self.record["runtime"]["observed_at"]
        self.clock.side_effect = [observation + 86399, observation + 86401]
        with self.assertRaisesRegex(EzMcdError, "previous_runtime_unavailable"):
            self.restore()
        self.assertEqual(before, self.store.account("mine"))
        self.clock.side_effect = None; self.clock.return_value = observation + 86401
        self.factory.reset_mock()
        with self.assertRaisesRegex(EzMcdError, "previous_runtime_unavailable"):
            self.restore()
        self.factory.assert_not_called()

    def test_device_risk_client_or_build_change_cannot_reuse_even_same_sid(self):
        for section, key in (("fields", "deviceToken"), ("fields", "riskDeviceId"),
                             ("client_headers", "v"), ("client_headers", "ct"), ("client_headers", "language")):
            self.store.update_account(deepcopy(self.record))
            result = self.rotate(sid="synthetic-old-session", changes=lambda p: p[section].update({key: "changed"}))
            self.assertFalse(result["runtime_available"])
            self.assertFalse(result["runtime_recovery_available"])
            self.assertNotIn("runtime_candidate", self.store.account("mine"))
        altered = deepcopy(self.record); altered["profile"]["build_sha256"] = "unsupported"
        self.assertIsNone(retain_candidate(self.record, altered, self.now))

    def test_candidate_modified_context_stops_before_network(self):
        self.rotate()
        self.store.modify_account("mine", lambda r: r | {"runtime_candidate": r["runtime_candidate"] | {"device_context": "not-the-device"}})
        with self.assertRaisesRegex(EzMcdError, "previous_runtime_unavailable"):
            self.restore()
        self.factory.assert_not_called()

    def test_successful_refresh_archives_runtime_but_still_requires_explicit_restore(self):
        transport = Mock()
        transport.send.return_value = {"http_status": 200, "response": {"success": True,
            "data": {"sid": "synthetic-rotated", "refreshToken": "synthetic-new-refresh"}}}
        provider = Mock(); provider.wallet.return_value = {"balance_cents": 100}
        result = refresh_account(self.store, "mine", provider_factory=lambda r: provider,
                                 transport_factory=lambda r: transport, clock=self.clock)
        self.assertTrue(result["runtime_recovery_available"])
        self.assertFalse(result["runtime_available"])
        self.assertIsNone(self.store.account("mine")["runtime"])
        transport.verify_runtime.assert_not_called()
        self.assertTrue(self.restore()["runtime_restored"])

    def test_refresh_incomplete_or_revoked_cannot_restore(self):
        self.rotate(); base = self.store.account("mine")
        for state in ("CLAIMED", "CANDIDATE", "STOPPED"):
            self.store.update_account(base | {"session_refresh": {"state": state}})
            self.assertFalse(public_account(self.store.account("mine"))["runtime_recovery_available"])
            with self.assertRaisesRegex(EzMcdError, "complete_session_refresh"):
                self.restore()
        self.store.update_account(base)
        revoke_local(self.store, "mine")
        self.assertNotIn("runtime_candidate", self.store.account("mine"))
        with self.assertRaisesRegex(EzMcdError, "active_official_account"):
            self.restore()
        self.factory.assert_not_called()

    def test_second_login_keeps_original_candidate_without_old_session_tokens(self):
        self.rotate(); first = self.store.account("mine")["runtime_candidate"]
        self.rotate(sid="synthetic-third-session")
        self.assertEqual(first, self.store.account("mine")["runtime_candidate"])
        self.assertTrue(self.restore()["runtime_restored"])
        self.assertEqual(self.factory.call_args.args[0]["profile"]["fields"]["sid"], "synthetic-third-session")

    def test_identity_mismatch_and_missing_device_context_never_retain(self):
        for changed in (self.record | {"identity": "another-member"}, self.record | {"profile": {}}):
            self.assertIsNone(retain_candidate(self.record, changed, self.now))
        self.assertIsNone(retain_candidate(self.record | {"revoked": True}, self.record, self.now))

    def test_invalid_or_expired_observation_is_not_archived(self):
        for age in (86401, -1):
            record = deepcopy(self.record); record["runtime"]["observed_at"] = self.now - age
            self.assertIsNone(retain_candidate(record, record, self.now))
        for change in ({"observed_at": float("nan")}, {"observed_at": True}, {"device_hex": "bad"}, {"environment_hex": "bad"}):
            self.assertFalse(fresh_evidence(self.record["runtime"] | change, self.now))

    def test_active_runtime_is_not_replaced_by_older_candidate(self):
        self.rotate()
        self.store.modify_account("mine", lambda r: r | {"runtime": self.record["runtime"] | {"observed_at": self.now - 1}})
        before = self.store.account("mine")
        with self.assertRaisesRegex(EzMcdError, "runtime_already_available"):
            self.restore()
        self.factory.assert_not_called()
        self.assertEqual(before, self.store.account("mine"))

    def test_restore_holds_identity_lock_and_failed_local_save_can_retry_get(self):
        self.rotate()
        def response():
            with self.assertRaisesRegex(EzMcdError, "account_busy"):
                with self.store.lock(self.record["identity"]): pass
            return {"http_status": 200, "response": {"success": True, "data": {"list": []}}}
        self.http.verify_runtime.side_effect = response
        before = self.store.account("mine")
        with patch("ezmcd.storage.write_private", side_effect=OSError("disk")):
            with self.assertRaises(OSError): self.restore()
        self.assertEqual(before, self.store.account("mine"))
        self.assertTrue(self.restore()["runtime_restored"])
        self.assertEqual(self.http.verify_runtime.call_count, 2)

    def test_diagnostics_recommend_restore_without_exposing_evidence(self):
        self.rotate()
        result = doctor(Service(self.store), "mine", verify_session=False, binary="unused")
        view = result["accounts"][0]
        self.assertIn("restore_previous_own_runtime_readonly", view["next_actions"])
        self.assertNotIn("update_official_runtime_evidence_before_checkout", view["next_actions"])
        self.assertNotIn("runtime_candidate", json.dumps(result))
