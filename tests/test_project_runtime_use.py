from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ezmcd.accounts import public_account, revoke_local
from ezmcd.errors import EzMcdError
from ezmcd.models import Policy, fingerprint
from ezmcd.protocol.signing import BUILD, Signer
from ezmcd.provider import McdProvider
from ezmcd.runtime_use import active, available, configure, view
from ezmcd.service import Service
from ezmcd.storage import Store
from ezmcd.transport import Transport

NOW = 1800000000
ACCEPTED = {"http_status": 200, "response": {"success": True, "data": {"list": []}}}
CONSTANTS = ("test-ak", "test-secret", tuple(range(64)), ())


class RuntimeUseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name))
        self.record = {"alias": "mine", "provider": "mcd-cn", "identity": fingerprint({"member": "synthetic"}),
            "policy": Policy().model_dump(), "binary": "synthetic-binary", "profile": {
                "build_sha256": BUILD, "fields": {"sid": "private-session", "deviceToken": "private-token",
                    "riskDeviceId": "private-risk", "meddyId": "private-member"},
                "client_headers": {"ct": "101", "v": "synthetic", "language": "cn"}},
            "runtime": {"device_hex": "A" * 32, "environment_hex": "b" * 16, "observed_at": NOW-72*3600}}
        self.store.add_account(self.record)
        self.store.claim("old-cart", "cart")
        self.http = Mock(); self.http.verify_runtime.return_value = ACCEPTED
        self.factory = Mock(return_value=self.http)
        self.now = NOW
        self.addCleanup(patch.stopall)
        patch("ezmcd.protocol.signing.time.time", side_effect=lambda: self.now).start()
        patch("ezmcd.protocol.signing.constants", return_value=CONSTANTS).start()
        self.body = {"synthetic": "not-an-official-order"}

    def grant(self, hours=168):
        return configure(self.store, "mine", hours, transport_factory=self.factory, clock=lambda: self.now)

    def transport(self):
        t = Transport(self.store.account("mine"))
        t._open = Mock(return_value=ACCEPTED)
        return t

    def test_explicit_grant_is_bounded_from_original_observation_and_does_not_grant_spending(self):
        result = self.grant()
        updated = self.store.account("mine")
        self.assertEqual(updated["runtime"], self.record["runtime"])
        self.assertEqual(updated["profile"], self.record["profile"])
        self.assertEqual(updated["policy"], self.record["policy"])
        self.assertEqual(updated["runtime_use"]["expires_at"], NOW+96*3600)
        self.http.verify_runtime.assert_called_once_with(historical=True)
        self.assertNotIn("private-", json.dumps(result))
        self.assertTrue(public_account(updated, session_verified=True)["runtime_available"])
        self.assertEqual(public_account(updated, session_verified=True)["readiness"]["checkout_blockers"], ["checkout_disabled"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)

    def test_repeat_checks_again_without_extending_authorization_or_observation(self):
        self.grant(); before = self.store.account("mine")
        self.now += 3600
        self.assertFalse(self.grant()["changed"])
        self.assertEqual(self.store.account("mine"), before)
        self.assertEqual(self.http.verify_runtime.call_count, 2)

    def test_fourteen_day_permission_keeps_ten_day_observation_and_only_four_days_remaining(self):
        self.record["runtime"]["observed_at"] = NOW-240*3600
        self.store.modify_account("mine", lambda _: deepcopy(self.record))
        with self.assertRaisesRegex(EzMcdError, "outside_limit"):
            self.grant(168)
        self.factory.assert_not_called()
        self.grant(336)
        updated = self.store.account("mine")
        self.assertEqual(updated["runtime"], self.record["runtime"])
        self.assertEqual(updated["policy"], self.record["policy"])
        self.assertEqual(updated["runtime_use"]["expires_at"], NOW+96*3600)
        self.assertTrue(available(updated, self.now))
        self.now += 3600
        self.assertFalse(self.grant(336)["changed"])
        self.assertEqual(self.store.account("mine"), updated)
        self.assertEqual(self.http.verify_runtime.call_count, 2)

    def test_fourteen_day_boundary_and_slow_get_cannot_extend_observation(self):
        self.record["runtime"]["observed_at"] = NOW-336*3600+1
        self.store.modify_account("mine", lambda _: deepcopy(self.record))
        self.grant(336)
        before = self.store.account("mine")
        self.now += 1
        self.assertFalse(available(before, self.now))
        self.factory.reset_mock()
        with self.assertRaisesRegex(EzMcdError, "outside_limit"):
            self.grant(336)
        self.factory.assert_not_called()
        self.assertEqual(self.store.account("mine"), before)
        self.now -= 1
        self.store.modify_account("mine", lambda _: deepcopy(self.record))
        def expire(**kwargs):
            self.now += 1
            return ACCEPTED
        self.http.verify_runtime.side_effect = expire
        with self.assertRaisesRegex(EzMcdError, "outside_limit"):
            self.grant(336)
        self.assertEqual(self.store.account("mine"), self.record)

    def test_higher_ceiling_does_not_extend_an_existing_seven_day_grant(self):
        from ezmcd.runtime_check import check_runtime
        self.grant(168)
        before = self.store.account("mine")
        self.now = before["runtime_use"]["expires_at"]+1
        self.assertFalse(available(before, self.now))
        result = check_runtime(self.store, "mine", transport_factory=self.factory, clock=lambda: self.now)
        self.assertTrue(result["read_only_v5_verified"])
        self.assertEqual(result["runtime_use"]["state"], "expired_or_changed")
        self.assertEqual(self.store.account("mine"), before)

    def test_ten_day_evidence_still_needs_a_single_use_order_check(self):
        self.record["runtime"]["observed_at"] = NOW-240*3600
        self.store.modify_account("mine", lambda _: deepcopy(self.record))
        self.grant(336)
        transport = self.transport()
        with self.assertRaisesRegex(EzMcdError, "new_order_check"):
            transport.send("POST", "/bff/order/orders", body=self.body)
        transport._open.assert_not_called()
        self.assertTrue(transport.prepare_order_runtime(self.body))
        transport.send("POST", "/bff/order/orders", body=self.body)
        self.assertEqual(transport._open.call_count, 2)
        with self.assertRaisesRegex(EzMcdError, "new_order_check"):
            transport.send("POST", "/bff/order/orders", body=self.body)
        self.assertEqual(transport._open.call_count, 2)
        self.assertFalse(self.store.account("mine")["policy"]["allow_checkout"])

    def test_revoke_is_local_and_keeps_evidence_policy_and_journal(self):
        self.grant(); self.factory.reset_mock()
        result = configure(self.store, "mine", transport_factory=self.factory, clock=lambda: self.now)
        self.factory.assert_not_called()
        self.assertEqual(result["runtime_use"]["state"], "not_enabled")
        self.assertEqual(self.store.account("mine"), self.record)
        self.assertFalse(available(self.store.account("mine"), self.now))

    def test_invalid_limits_and_ineligible_accounts_stop_before_network_or_changes(self):
        for hours in (True, 24, 337, 0, -1, 48.5, "168"):
            with self.subTest(hours=hours), self.assertRaises(EzMcdError): self.grant(hours)
        variants = [{"runtime": None}, {"revoked": True}, {"provider": "demo"}]
        variants += [{"session_refresh": {"state": s}} for s in ("CLAIMED", "CANDIDATE", "STOPPED")]
        variants += [{"runtime": self.record["runtime"] | {"observed_at": v}}
                     for v in (True, float("inf"), -1, NOW+1, NOW-168*3600)]
        for change in variants:
            record = deepcopy(self.record) | change
            # Invalid NaN/Inf cannot be persisted by the private store either.
            if isinstance(change.get("runtime"), dict) and change["runtime"].get("observed_at") == float("inf"):
                self.assertFalse(available(record, self.now)); continue
            self.store.modify_account("mine", lambda _: record)
            with self.subTest(change=change), self.assertRaises(EzMcdError): self.grant()
            self.assertEqual(self.store.account("mine"), record)
        self.factory.assert_not_called()

    def test_rejected_or_malformed_get_never_grants_permission(self):
        for response in ({"http_status": 401, "response": {"success": False}},
                         {"http_status": 200, "response": {"success": True, "data": {}}}, {}, None):
            self.http.verify_runtime.return_value = response
            with self.assertRaisesRegex(EzMcdError, "readonly_check_failed"): self.grant()
            self.assertEqual(self.store.account("mine"), self.record)

    def test_expiry_during_get_cannot_save_a_grant(self):
        def expire(**kwargs):
            self.now = self.record["runtime"]["observed_at"]+168*3600
            return ACCEPTED
        self.http.verify_runtime.side_effect = expire
        with self.assertRaisesRegex(EzMcdError, "outside_limit"): self.grant()
        self.assertEqual(self.store.account("mine"), self.record)

    def test_diagnostic_uses_post_response_expiry_and_cannot_renew_the_grant(self):
        from ezmcd.runtime_check import check_runtime
        self.grant(); record = self.store.account("mine")
        def expire(**kwargs):
            self.now = record["runtime_use"]["expires_at"]
            return ACCEPTED
        self.http.verify_runtime.side_effect = expire
        result = check_runtime(self.store, "mine", transport_factory=self.factory, clock=lambda: self.now)
        self.assertTrue(result["read_only_v5_verified"])
        self.assertTrue(result["observation_expired_by_project_policy"])
        self.assertEqual(result["runtime_use"]["state"], "expired_or_changed")
        self.assertEqual(self.store.account("mine"), record)

    def test_binding_changes_and_expiry_invalidate_without_transferring_permission(self):
        self.grant(); original = self.store.account("mine")
        for section, key, value in (("profile", "sid", "rotated"), ("profile", "riskDeviceId", "new-risk"),
                                    ("profile", "deviceToken", "new-device"), ("profile", "meddyId", "other-member"),
                                    ("runtime", "device_hex", "C"*32), ("runtime", "observed_at", NOW-3600)):
            record = deepcopy(original)
            target = record[section]["fields"] if section == "profile" else record[section]
            target[key] = value
            self.assertFalse(active(record, self.now))
        for key, value in (("identity", "other"), ("binary", "different-binary"), ("revoked", True)):
            self.assertFalse(active(original | {key: value}, self.now))
        self.assertFalse(active(original, original["runtime_use"]["expires_at"]))
        self.assertFalse(active(original, NOW-1))
        self.assertTrue(active(original | {"policy": Policy(allow_checkout=True).model_dump()}, self.now))

    def test_default_limit_and_grant_get_do_not_authorize_an_order_signature(self):
        with self.assertRaisesRegex(EzMcdError, "runtime_expired"): Signer(self.record).runtime()
        self.grant(); transport = self.transport()
        transport.verify_runtime(historical=True)
        with self.assertRaisesRegex(EzMcdError, "new_order_check"):
            transport.send("POST", "/bff/order/orders", body=self.body)
        self.assertEqual(transport._open.call_count, 1)
        self.assertEqual(transport._open.call_args.args[0], "GET")

    def test_malformed_authorization_cannot_waive_the_default_limit(self):
        self.grant(); original = self.store.account("mine")
        for key, value in (("schema_version", True), ("maximum_observation_age_hours", "168"),
                           ("granted_at", True), ("granted_at", NOW+1), ("expires_at", NOW+168*3600),
                           ("binding", "different")):
            record = deepcopy(original); record["runtime_use"][key] = value
            with self.subTest(key=key, value=value):
                self.assertFalse(available(record, self.now))

    def test_each_historical_order_requires_a_new_body_bound_check(self):
        self.grant(); transport = self.transport()
        self.assertTrue(transport.prepare_order_runtime(self.body))
        transport._open.assert_called_once()
        self.assertEqual(transport._open.call_args.args[:2], ("GET", "https://api.mcd.cn/bff/order/orders?orderCategoryId=0&page=1&size=1"))
        transport.send("POST", "/bff/order/orders", body=self.body)
        self.assertEqual(transport._open.call_count, 2)
        self.assertEqual(transport._open.call_args.args[0], "POST")
        with self.assertRaisesRegex(EzMcdError, "new_order_check"):
            transport.send("POST", "/bff/order/orders", body=self.body)
        self.assertEqual(transport._open.call_count, 2)
        self.assertEqual(transport.signer.account, self.store.account("mine"))

    def test_body_change_timeout_restart_and_revocation_cannot_reuse_check(self):
        self.grant()
        for variant in ("body", "timeout", "restart", "revoked", "grant_changed"):
            self.now = NOW; transport = self.transport(); transport.prepare_order_runtime(self.body)
            body = self.body
            if variant == "body": body = {"synthetic": "different-order"}
            if variant == "timeout": self.now += 61
            if variant == "restart": transport = self.transport()
            if variant == "revoked": transport.signer.account.pop("runtime_use")
            if variant == "grant_changed": transport.signer.account["runtime_use"]["granted_at"] -= 1
            count = transport._open.call_count
            with self.subTest(variant=variant), self.assertRaises(EzMcdError):
                transport.send("POST", "/bff/order/orders", body=body)
            self.assertEqual(transport._open.call_count, count)

    def test_failed_or_expired_preorder_read_sends_no_order(self):
        self.grant(); transport = self.transport()
        transport._open.return_value = {"http_status": 401, "response": {"success": False}}
        with self.assertRaisesRegex(EzMcdError, "readonly_check_failed"):
            transport.prepare_order_runtime(self.body)
        self.assertIsNone(transport.signer.historical_order_check)
        with self.assertRaises(EzMcdError): transport.send("POST", "/bff/order/orders", body=self.body)
        self.assertEqual(transport._open.call_count, 1)

    def test_provider_runs_runtime_get_after_duplicate_reads_and_before_order_creation(self):
        self.grant(); transport = self.transport(); provider = McdProvider(transport.signer.account, transport)
        with patch.object(provider, "get", return_value={"list": []}) as history:
            provider.preflight({"body": self.body, "store_code": "1234567", "pickup_at": "2027-01-16T10:15:00+08:00"})
        history.assert_called_once()
        self.assertEqual(transport._open.call_count, 1)
        self.assertIsNotNone(transport.signer.historical_order_check)
        self.assertEqual(transport._open.call_args.args[0], "GET")

    def test_recent_evidence_does_not_need_or_implicitly_create_an_authorization(self):
        record = deepcopy(self.record); record["runtime"]["observed_at"] = NOW-3600
        transport = Transport(record); transport._open = Mock(return_value=ACCEPTED)
        self.assertFalse(transport.prepare_order_runtime(self.body))
        transport._open.assert_not_called()
        self.assertNotIn("runtime_use", record)
        self.assertEqual(view(record, NOW)["maximum_observation_age_hours"], 24)

    def test_local_account_revocation_removes_reuse_authority(self):
        self.grant(); revoke_local(self.store, "mine")
        self.assertNotIn("runtime_use", self.store.account("mine"))

    def test_account_rotation_before_checkout_lock_uses_no_stale_snapshot(self):
        self.grant(); record = self.store.account("mine"); changed = deepcopy(record)
        changed.pop("runtime_use")
        provider = Mock()
        service = Service(self.store, provider_factory=Mock(return_value=provider))
        with patch.object(self.store, "account", side_effect=[record, changed]):
            with self.assertRaisesRegex(EzMcdError, "account_changed_before_checkout"):
                service.submit("mine", "unused", "synthetic-key")
        provider.assert_not_called()
        self.assertEqual(provider.mock_calls, [])

    def test_read_failure_stops_checkout_before_reserving_or_claiming_an_order(self):
        self.grant()
        def enable(record):
            record["policy"] = Policy(allow_checkout=True).model_dump()
            return record
        record = self.store.modify_account("mine", enable)
        quote = {"quote_id": "synthetic-quote", "intent": "synthetic-intent", "body": self.body,
            "store_code": "1234567", "pickup_at": "2027-01-16T10:15:00+08:00", "amount_cents": 100,
            "expires_at": NOW+60, "request": {"account": "mine", "store_code": "1234567",
                "pickup_at": "2027-01-16T10:15:00+08:00", "max_amount_cny": "20.00"},
            "checkout_digest": "synthetic-digest"}
        self.store.save_quote(record["identity"], quote)
        transport = self.transport(); transport._open.return_value = {"http_status": 401, "response": {"success": False}}
        provider = McdProvider(record, transport)
        with patch.object(provider, "quote", return_value=quote), patch.object(provider, "get", return_value={"list": []}), \
             patch.object(provider, "create_order") as create:
            with self.assertRaisesRegex(EzMcdError, "readonly_check_failed"):
                Service(self.store, provider_factory=lambda _: provider).submit("mine", "synthetic-quote", "new-key")
            create.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 1)

    def test_runtime_permission_cannot_enable_spending(self):
        self.grant(); record = self.store.account("mine")
        quote = {"quote_id": "synthetic-quote", "intent": "synthetic-intent"}
        self.store.save_quote(record["identity"], quote)
        provider = Mock()
        with self.assertRaisesRegex(EzMcdError, "local_checkout_policy_required"):
            Service(self.store, provider_factory=lambda _: provider).submit("mine", "synthetic-quote", "new-key")
        self.assertEqual(provider.mock_calls, [])

    def test_successful_refresh_drops_authorization_even_if_sid_is_unchanged(self):
        from ezmcd.sessions import refresh_account
        def token(record):
            record["profile"]["fields"]["refreshToken"] = "private-refresh"
            return record
        self.store.modify_account("mine", token); self.grant()
        http = Mock(); http.send.return_value = {"http_status": 200, "response": {"success": True,
            "data": {"sid": "private-session", "refreshToken": "private-rotated-refresh"}}}
        provider = Mock(); provider.wallet.return_value = {"balance_cents": 1000}
        refresh_account(self.store, "mine", provider_factory=lambda _: provider,
                        transport_factory=lambda _: http, clock=lambda: self.now)
        self.assertNotIn("runtime_use", self.store.account("mine"))


if __name__ == "__main__":
    unittest.main()
