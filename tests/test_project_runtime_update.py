from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ezmcd.accounts import update_runtime, update_runtime_from_device, connect_samples
from ezmcd.capture import runtime_sample_from_har
from ezmcd.errors import EzMcdError
from ezmcd.models import Policy, fingerprint
from ezmcd.protocol.signing import BUILD
from ezmcd.protocol.v5 import ObservedRuntime, encode
from ezmcd.storage import Store, write_private
from test_project_accounts import sample

NOW = 1800000000
CONSTANTS = ("test-ak", "test-secret", tuple(range(64)), ())


def evidence(age=10):
    signature = encode("/bff/order/orders", ObservedRuntime("A" * 32, "b" * 16),
        milliseconds=(NOW-age)*1000, random_value=11, counter=1, constants=CONSTANTS[2])
    return {"request": {"method": "POST", "host": "api.mcd.cn", "path": "/bff/order/orders",
        "headers": {"SID": "synthetic-session", "Token": "synthetic-device", "X-Mcd-Sign": signature},
        "body": "DO-NOT-REPLAY"}}


class RuntimeUpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name)); self.path = self.store.root / "evidence.json"
        self.record = {"alias": "mine", "identity": fingerprint({"member": "synthetic-member"}), "provider": "mcd-cn",
            "binary": "unused", "runtime": None, "policy": Policy(allow_checkout=True).model_dump(),
            "profile": {"build_sha256": BUILD, "verified_endpoint": "/bff/member/mwallet/balance",
                "client_headers": {"ct": "101", "v": "test", "language": "cn"},
                "fields": {"sid": "synthetic-session", "deviceToken": "synthetic-device", "meddyId": "synthetic-member",
                           "refreshToken": "keep-my-refresh-token"}}}
        self.store.add_account(self.record)
        self.store.claim("old-write", "cart")
        self.addCleanup(patch.stopall)
        patch("ezmcd.accounts.constants", return_value=CONSTANTS).start()
        patch("ezmcd.accounts.time.time", return_value=NOW).start()
        self.wallet = patch("ezmcd.provider.McdProvider.wallet", return_value={"balance_cents": 100}).start()

    def test_update_preserves_session_policy_journal_and_observed_time(self):
        write_private(self.path, evidence())
        result = update_runtime(self.store, "mine", self.path)
        updated = self.store.account("mine")
        self.assertEqual(updated["profile"], self.record["profile"])
        self.assertEqual(updated["policy"], self.record["policy"])
        self.assertEqual(updated["runtime"]["observed_at"], NOW-10)
        self.assertEqual(self.wallet.call_count, 1)
        self.assertNotIn("synthetic-session", json.dumps(result))
        self.assertNotIn("DO-NOT-REPLAY", json.dumps(updated))
        update_runtime(self.store, "mine", self.path)
        self.assertEqual(self.store.account("mine")["runtime"]["observed_at"], NOW-10)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 1)

    def test_other_account_device_origin_path_and_signature_are_rejected_before_network(self):
        variants = []
        for key, value in (("host", "api.mcd.cn.example"), ("path", "/bff/order/orders?x=1"), ("method", "GET")):
            e = evidence(); e["request"][key] = value; variants.append(e)
        for key, value in (("SID", "different-session"), ("Token", "different-device"), ("X-Mcd-Sign", "broken")):
            e = evidence(); e["request"]["headers"][key] = value; variants.append(e)
        for e in variants:
            write_private(self.path, e)
            with self.assertRaises((EzMcdError, ValueError)):
                update_runtime(self.store, "mine", self.path)
        self.wallet.assert_not_called()
        self.assertEqual(self.store.account("mine"), self.record)

    def test_expired_future_and_older_evidence_cannot_renew_runtime(self):
        for age in (86401, -1):
            write_private(self.path, evidence(age))
            with self.assertRaisesRegex(EzMcdError, "expired"):
                update_runtime(self.store, "mine", self.path)
        self.wallet.assert_not_called()
        write_private(self.path, evidence()); update_runtime(self.store, "mine", self.path)
        self.wallet.reset_mock()
        write_private(self.path, evidence(30))
        with self.assertRaisesRegex(EzMcdError, "older_than"):
            update_runtime(self.store, "mine", self.path)
        self.wallet.assert_not_called()

    def test_expired_live_session_does_not_commit_runtime(self):
        write_private(self.path, evidence())
        self.wallet.side_effect = EzMcdError("session_expired_reconnect_locally")
        with self.assertRaises(EzMcdError): update_runtime(self.store, "mine", self.path)
        self.assertEqual(self.store.account("mine"), self.record)

    def test_incomplete_refresh_cannot_be_overwritten_by_runtime_import(self):
        for state in ("CLAIMED", "CANDIDATE", "STOPPED"):
            self.store.modify_account("mine", lambda r: r | {"session_refresh": {"state": state}})
            write_private(self.path, evidence())
            with self.assertRaisesRegex(EzMcdError, "complete_session_refresh"):
                update_runtime(self.store, "mine", self.path)
        self.wallet.assert_not_called()

    def test_runtime_only_har_discards_order_body_and_rejects_mixed_sessions(self):
        e = evidence()["request"]
        entry = {"request": {"method": "POST", "url": "https://api.mcd.cn/bff/order/orders",
            "headers": [{"name": k, "value": v} for k, v in e["headers"].items()],
            "postData": {"text": "DO-NOT-REPLAY"}}}
        har = {"log": {"entries": [entry]}}
        self.assertEqual(runtime_sample_from_har(har)["request"]["body"], "")
        write_private(self.path, har)
        self.assertTrue(update_runtime(self.store, "mine", self.path, har=True)["runtime_updated"])
        other = deepcopy(entry); other["request"]["headers"][0]["value"] = "other-session"
        har["log"]["entries"].append(other)
        with self.assertRaisesRegex(EzMcdError, "one_official_runtime_session"):
            runtime_sample_from_har(har)

    def test_balance_reimport_of_same_session_preserves_refresh_and_runtime_only(self):
        # Use the signed synthetic balance fixture's identity, with its own key.
        from test_project_accounts import KEYS
        with patch("ezmcd.accounts.constants", return_value=KEYS):
            path = self.store.root / "balance.json"; write_private(path, sample())
            connect_samples(self.store, "another", path, "unused")
            prior = {"device_hex": "A" * 32, "environment_hex": "b" * 16, "observed_at": NOW-100}
            def enrich(r):
                r["profile"]["fields"]["refreshToken"] = "synthetic-retained-refresh"
                r["runtime"] = prior
                return r
            self.store.modify_account("another", enrich)
            connect_samples(self.store, "another", path, "unused", update=True)
            record = self.store.account("another")
            self.assertEqual(record["runtime"], prior)
            self.assertEqual(record["profile"]["fields"]["refreshToken"], "synthetic-retained-refresh")
            # A changed session for the same member must not inherit either.
            from ezmcd.accounts import _enroll
            profile = deepcopy(record["profile"])
            profile["fields"].pop("refreshToken"); profile["fields"]["sid"] = "new-session"
            _enroll(self.store, "another", profile, "unused", None, None, update=True, check_live=True)
            self.assertIsNone(self.store.account("another")["runtime"])
            self.assertNotIn("refreshToken", self.store.account("another")["profile"]["fields"])

    def device_source(self):
        source = deepcopy(self.record)
        source.update(alias="device", identity=fingerprint({"member": "second-member"}),
                      runtime={"device_hex": "A" * 32, "environment_hex": "b" * 16, "observed_at": NOW-100})
        source["profile"]["fields"].update(sid="source-session-never-copy", meddyId="second-member", riskDeviceId="same-official-sdk-id")
        source["profile"]["client_headers"].update(sv="v4", biz_from="synthetic-source-business")
        self.store.add_account(source)
        def enrich(r):
            r["profile"]["fields"]["riskDeviceId"] = "same-official-sdk-id"
            return r
        self.store.modify_account("mine", enrich)
        return self.store.account("mine")

    def test_same_device_update_verifies_target_session_without_copying_source_credentials(self):
        before = self.device_source()
        with patch("ezmcd.transport.Transport.verify_runtime", autospec=True,
                   return_value={"http_status": 200, "response": {"success": True, "data": {"list": []}}}) as probe:
            result = update_runtime_from_device(self.store, "mine", "device")
        candidate = probe.call_args.args[0].signer.account
        self.assertEqual(candidate["profile"], before["profile"])
        after = self.store.account("mine")
        self.assertEqual(after["profile"], before["profile"])
        self.assertEqual(after["policy"], before["policy"])
        self.assertEqual(after["runtime"]["observed_at"], NOW-100)
        self.assertFalse(result["order_post_acceptance_verified"])
        self.assertFalse(result["cross_day_validity_verified"])
        self.wallet.assert_not_called()

    def test_same_device_update_rejection_and_wrong_device_leave_account_unchanged(self):
        before = self.device_source()
        with patch("ezmcd.transport.Transport.verify_runtime", return_value={"http_status": 401, "response": {"success": False}}):
            with self.assertRaises(EzMcdError): update_runtime_from_device(self.store, "mine", "device")
        self.assertEqual(self.store.account("mine"), before)
        for field in ("riskDeviceId", "deviceToken"):
            def change(r):
                r["profile"]["fields"][field] = "another-device"
                return r
            self.store.modify_account("device", change)
            with patch("ezmcd.transport.Transport.verify_runtime") as probe:
                with self.assertRaisesRegex(EzMcdError, "same_verified_official_device"):
                    update_runtime_from_device(self.store, "mine", "device")
                probe.assert_not_called()
            self.assertEqual(self.store.account("mine"), before)

    def test_same_device_update_cannot_extend_expired_observation(self):
        self.device_source()
        def expire(r):
            r["runtime"]["observed_at"] = NOW-86401
            return r
        self.store.modify_account("device", expire)
        with patch("ezmcd.transport.Transport.verify_runtime") as probe:
            with self.assertRaisesRegex(EzMcdError, "expired"):
                update_runtime_from_device(self.store, "mine", "device")
            probe.assert_not_called()

    def test_transport_runtime_probe_is_only_fixed_get_and_has_no_body(self):
        from ezmcd.transport import Transport
        from unittest.mock import Mock
        transport = Transport(self.record); transport.signer = Mock()
        transport.signer.headers.return_value = {"test": "synthetic"}
        with patch.object(transport, "_open", return_value={}) as request:
            transport.verify_runtime()
        request.assert_called_once_with("GET", "https://api.mcd.cn/bff/order/orders?orderCategoryId=0&page=1&size=1", {"test": "synthetic"}, None)
        self.assertTrue(transport.signer.headers.call_args.kwargs["v5"])


if __name__ == "__main__":
    unittest.main()
