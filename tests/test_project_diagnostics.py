import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ezmcd.diagnostics import doctor, credentials_guide, runtime_guidance
from ezmcd.errors import EzMcdError, error_result
from ezmcd.models import Policy, fingerprint
from ezmcd.service import Service
from ezmcd.storage import Store
from ezmcd.transport import data
from ezmcd.protocol.signing import BUILD
from ezmcd.runtime_use import binding
from ezmcd.runtime_recovery import device_context


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.record = {"alias": "test", "identity": fingerprint({"test": "doctor"}), "provider": "mcd-cn",
            "binary": str(self.store.root / "private-client-path"), "policy": Policy().model_dump(),
            "profile": {"fields": {"sid": "private-session", "refreshToken": "private-refresh",
                                    "deviceToken": "private-device", "riskDeviceId": "private-risk"}}}
        self.store.add_account(self.record)
        self.provider = Mock()
        self.service = Service(self.store, lambda _: self.provider)

    def test_default_diagnosis_never_calls_network_and_exposes_no_credentials_or_paths(self):
        result = doctor(self.service, binary="absent-client")
        self.provider.wallet.assert_not_called()
        self.assertEqual(result["accounts"][0]["client_build"], "not_found")
        self.assertTrue(result["accounts"][0]["session_refresh_supported"])
        text = json.dumps(result)
        self.assertNotIn("private-", text)
        self.assertNotIn(str(self.store.root), text)

    def test_live_expired_session_is_explained_without_automatic_refresh(self):
        self.provider.wallet.side_effect = EzMcdError("session_expired_reconnect_locally")
        result = doctor(self.service, "test", verify_session=True, binary="absent-client")
        self.assertEqual(result["accounts"][0]["session_status"], "expired_relogin_or_refresh")
        self.assertIn("account_refresh", result["accounts"][0]["session_check"]["next_action"])
        self.assertEqual([x[0] for x in self.provider.method_calls], ["wallet"])

    def test_server_401_is_distinct_from_generic_business_rejection(self):
        with self.assertRaisesRegex(EzMcdError, "session_expired"):
            data({"http_status": 200, "response": {"success": False, "code": 401, "message": "private-server-message"}})
        with self.assertRaisesRegex(EzMcdError, "upstream_rejected"):
            data({"http_status": 200, "response": {"success": False, "code": 123}})

    def test_incomplete_refresh_diagnosis_gives_specific_recovery_without_network(self):
        for state, action, code in (
                ("CANDIDATE", "resume_refresh_verification", "session_refresh_received_resume_verification"),
                ("CLAIMED", "relogin_or_reconnect_locally", "session_refresh_already_attempted_relogin_locally"),
                ("STOPPED", "relogin_or_reconnect_locally", "session_refresh_already_attempted_relogin_locally")):
            with self.subTest(state=state):
                self.record["session_refresh"] = {"state": state}
                self.store.update_account(self.record)
                result = doctor(self.service, "test", verify_session=True, binary="absent-client")["accounts"][0]
                self.assertEqual(result["next_actions"][0], action)
                self.assertNotIn("update_official_runtime_evidence_before_checkout", result["next_actions"])
                self.assertEqual(result["session_check"]["error"], code)
                self.assertFalse(result["readiness"]["can_browse"])
                self.assertNotIn("private-", json.dumps(result))
        self.provider.wallet.assert_not_called()

    def test_refresh_401_help_does_not_advise_repeating_rotation(self):
        result = error_result("session_refresh_rejected_401")
        self.assertIn("--update", result["next_action"])
        self.assertIn("不重复刷新", result["next_action"])
        self.assertFalse(result["automatic_write_retry"])

    def test_empty_install_has_concrete_local_setup_steps(self):
        empty = Store(self.store.root / "empty")
        result = doctor(Service(empty), binary="absent-client")
        self.assertEqual(result["next_step"], "connect_your_own_account")
        self.assertTrue(credentials_guide()["credentials_stay_on_your_device"])
        self.assertIn("--device-from", json.dumps(credentials_guide()))

    def test_help_preserves_machine_code_and_never_enables_retries(self):
        result = error_result("local_checkout_policy_required")
        self.assertEqual(result["error"], "local_checkout_policy_required")
        self.assertTrue(result["message"])
        self.assertFalse(result["automatic_write_retry"])

    def runtime_record(self, age_hours, now=1800000000):
        self.record["profile"].update(build_sha256=BUILD,
            client_headers={"ct": "101", "v": "synthetic", "language": "cn"})
        self.record["runtime"] = {"device_hex": "A" * 32, "environment_hex": "b" * 16,
                                  "observed_at": now-age_hours*3600}
        self.store.update_account(self.record)
        return now

    def grant_record(self, hours, now):
        self.record["runtime_use"] = {"schema_version": 1, "binding": binding(self.record),
            "granted_at": self.record["runtime"]["observed_at"]+1,
            "expires_at": self.record["runtime"]["observed_at"]+hours*3600,
            "maximum_observation_age_hours": hours}
        self.store.update_account(self.record)

    def test_ten_day_sample_explains_owner_choice_without_network_or_state_change(self):
        now = self.runtime_record(240)
        self.store.claim("existing-purchase", "order")
        before = self.store.account("test")
        with patch("ezmcd.diagnostics.time.time", return_value=now):
            account = doctor(self.service, "test", binary="absent-client")["accounts"][0]
        guidance = account["runtime_guidance"]
        self.assertEqual(guidance["state"], "explicit_reuse_choice_available")
        option = guidance["explicit_reuse_option"]
        self.assertEqual((option["minimum_max_age_hours"], option["maximum_max_age_hours"]), (241, 336))
        self.assertEqual(option["latest_possible_expiry"], now+96*3600)
        self.assertEqual(option["maximum_remaining_seconds"], 96*3600)
        self.assertEqual(option["cli_argv_template"][-1], "<chosen_hours>")
        self.assertIn("choose_explicit_runtime_reuse_or_update_evidence_locally", account["next_actions"])
        self.assertFalse(account["readiness"]["checkout_prerequisites_met"])
        self.assertFalse(guidance["permission_changed"])
        self.assertEqual(self.store.account("test"), before)
        self.assertEqual(self.provider.method_calls, [])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 1)
        self.assertNotIn("private-", json.dumps(guidance))
        self.assertNotIn(str(self.store.root), json.dumps(guidance))

    def test_active_grant_reports_original_expiry_and_expired_shorter_grant_never_renews(self):
        now = self.runtime_record(240)
        self.grant_record(336, now)
        original = self.store.account("test")
        first = runtime_guidance(original, now)
        later = runtime_guidance(original, now+3600)
        self.assertEqual(first["state"], "available_under_explicit_permission")
        self.assertEqual(first["usable_until"], later["usable_until"])
        self.assertEqual(first["remaining_seconds"]-later["remaining_seconds"], 3600)
        self.assertIsNone(first["explicit_reuse_option"])
        self.grant_record(168, now)
        expired = runtime_guidance(self.record, now)
        self.assertEqual(expired["state"], "explicit_reuse_choice_available")
        self.assertIsNone(expired["usable_until"])
        self.assertEqual(self.store.account("test")["runtime_use"]["maximum_observation_age_hours"], 168)

    def test_fourteen_day_boundary_requires_new_evidence_even_after_prior_grant(self):
        now = self.runtime_record(336)
        self.grant_record(336, now)
        result = runtime_guidance(self.record, now)
        self.assertEqual(result["state"], "new_official_evidence_required")
        self.assertIsNone(result["explicit_reuse_option"])
        self.assertIsNone(result["usable_until"])

    def test_missing_device_or_incomplete_login_never_offers_reuse(self):
        now = self.runtime_record(72)
        for state in ("CLAIMED", "CANDIDATE", "STOPPED"):
            self.record["session_refresh"] = {"state": state}
            result = runtime_guidance(self.record, now)
            self.assertEqual(result["state"], "complete_account_recovery_first")
            self.assertIsNone(result["explicit_reuse_option"])
        self.record.pop("session_refresh")
        self.record["revoked"] = True
        self.assertEqual(runtime_guidance(self.record, now)["state"], "complete_account_recovery_first")
        self.record.pop("revoked")
        self.record["profile"]["fields"].pop("riskDeviceId")
        result = runtime_guidance(self.record, now)
        self.assertEqual(result["state"], "connect_own_device_context_first")
        self.assertIsNone(result["explicit_reuse_option"])

    def test_live_expired_session_suppresses_local_reuse_option(self):
        now = self.runtime_record(72)
        self.provider.wallet.side_effect = EzMcdError("session_expired_reconnect_locally")
        with patch("ezmcd.diagnostics.time.time", return_value=now):
            result = doctor(self.service, "test", verify_session=True, binary="absent-client")["accounts"][0]
        self.assertEqual(result["runtime_guidance"]["state"], "complete_account_recovery_first")
        self.assertIsNone(result["runtime_guidance"]["explicit_reuse_option"])
        self.assertEqual([x[0] for x in self.provider.method_calls], ["wallet"])

    def test_recent_candidate_is_restore_option_without_new_sample_or_reuse_grant(self):
        now = self.runtime_record(4)
        runtime = self.record.pop("runtime")
        self.record["runtime_candidate"] = {"device_context": device_context(self.record), "evidence": runtime}
        result = runtime_guidance(self.record, now)
        self.assertEqual(result["state"], "restore_previous_own_runtime_readonly")
        self.assertIsNone(result["explicit_reuse_option"])
        self.assertIsNone(result["usable_until"])

    def test_default_runtime_expiry_and_invalid_evidence_are_distinct(self):
        now = self.runtime_record(4)
        result = runtime_guidance(self.record, now)
        self.assertEqual(result["state"], "available_under_default_limit")
        self.assertEqual(result["remaining_seconds"], 20*3600)
        self.record["runtime"]["observed_at"] = now+1
        self.assertEqual(runtime_guidance(self.record, now)["state"], "new_official_evidence_required")
        self.record.pop("runtime")
        self.assertEqual(runtime_guidance(self.record, now)["state"], "new_official_evidence_required")

    def test_expired_spending_policy_explains_recovery_without_enabling_or_paying(self):
        self.record["policy"] = Policy(allow_checkout=True, expires_at="2020-01-01T10:00:00+08:00").model_dump()
        self.store.update_account(self.record)
        before = self.store.account("test")
        result = doctor(self.service, "test", binary="absent-client")["accounts"][0]
        self.assertIn("checkout_policy_expired", result["readiness"]["checkout_blockers"])
        self.assertIn("review_existing_orders_before_new_local_spending_authorization", result["next_actions"])
        self.assertEqual(result["expired_policy_guidance"]["local_orders_tool"],
                         {"name": "orders_list", "arguments": {"account": "test"}})
        self.assertIn("不换键重购", result["expired_policy_guidance"]["next_action"])
        self.assertEqual(self.store.account("test"), before)
        self.assertEqual(self.provider.method_calls, [])


if __name__ == "__main__":
    unittest.main()
