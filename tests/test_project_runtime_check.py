from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ezmcd.errors import EzMcdError
from ezmcd.models import Policy, fingerprint
from ezmcd.protocol.signing import Signer
from ezmcd.runtime_check import check_runtime
from ezmcd.storage import Store
from ezmcd.transport import Transport

NOW = 1800000000


class RuntimeCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name))
        self.record = {"alias": "mine", "provider": "mcd-cn", "identity": fingerprint({"member": "synthetic"}),
            "policy": Policy().model_dump(), "binary": "synthetic-binary", "profile": {
                "fields": {"sid": "private-session", "deviceToken": "private-token"},
                "client_headers": {"ct": "101", "v": "synthetic", "language": "cn"}},
            "runtime": {"device_hex": "A" * 32, "environment_hex": "b" * 16, "observed_at": NOW-90000}}
        self.store.add_account(self.record)
        self.store.claim("old-cart", "cart")
        self.http = Mock()
        self.http.verify_runtime.return_value = {"http_status": 200, "response": {"success": True,
            "data": {"list": [{"orderId": "private-order", "name": "private-name"}]}}}
        self.factory = Mock(return_value=self.http)

    def check(self):
        return check_runtime(self.store, "mine", transport_factory=self.factory, clock=lambda: NOW)

    def test_historical_check_does_not_renew_authorize_change_journal_or_disclose_history(self):
        path = self.store.root / "accounts" / (self.record["identity"] + ".json")
        before = path.read_bytes()
        result = self.check()
        self.assertTrue(result["read_only_v5_verified"])
        self.assertTrue(result["observation_expired_by_project_policy"])
        self.assertEqual(result["observation_age_hours"], 25)
        self.assertFalse(result["checkout_authorized_by_check"])
        self.assertFalse(result["order_post_acceptance_verified"])
        self.assertEqual(before, path.read_bytes())
        self.assertNotIn("private-", json.dumps(result))
        self.http.verify_runtime.assert_called_once_with(historical=True)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)
        with patch("ezmcd.protocol.signing.time.time", return_value=NOW):
            with self.assertRaisesRegex(EzMcdError, "runtime_expired"):
                Signer(self.store.account("mine")).runtime()

    def test_rejected_or_incomplete_response_does_not_infer_session_expiry_or_persist_success(self):
        for response in ({"http_status": 401, "response": {"success": False}},
                         {"http_status": 200, "response": {"success": True, "data": {}}},
                         {"http_status": 200, "response": {"success": False, "data": {"list": []}}}):
            self.http.verify_runtime.return_value = response
            result = self.check()
            self.assertFalse(result["read_only_v5_verified"])
            self.assertNotIn("session_expired", json.dumps(result))
            self.assertEqual(self.store.account("mine"), self.record)

    def test_invalid_missing_future_revoked_and_incomplete_accounts_stop_before_network(self):
        variants = [{"runtime": None}, {"revoked": True}, {"provider": "demo"}]
        variants += [{"runtime": self.record["runtime"] | {"observed_at": x}} for x in (True, "yesterday", NOW+1)]
        variants += [{"session_refresh": {"state": state}} for state in ("CLAIMED", "CANDIDATE", "STOPPED")]
        for change in variants:
            with self.subTest(change=change):
                record = deepcopy(self.record) | change
                self.store.modify_account("mine", lambda _: record)
                with self.assertRaises(EzMcdError): self.check()
        self.factory.assert_not_called()

    def test_account_rotation_during_lock_acquisition_stops_before_network(self):
        current = deepcopy(self.record)
        current["profile"]["fields"]["sid"] = "rotated"
        with patch.object(self.store, "account", side_effect=[self.record, current]):
            with self.assertRaisesRegex(EzMcdError, "account_changed"):
                self.check()
        self.factory.assert_not_called()

    def test_historical_signing_is_restricted_to_exact_read_not_order_or_payment(self):
        args = {"method": "GET", "path": "/bff/order/orders", "query": "orderCategoryId=0&page=1&size=1",
                "business": ("1001", "202"), "v5": True, "historical_runtime_probe": True}
        signer = Signer(self.record)
        with patch("ezmcd.protocol.signing.constants", return_value=("test-ak", "test-secret", tuple(range(64)), ())) as constants, \
             patch("ezmcd.protocol.signing.time.time", return_value=NOW):
            headers = signer.headers(**args)
            self.assertTrue(headers["x-mcd-sign"].startswith("i3"))
            constants.reset_mock()
            for change in ({"method": "POST"}, {"path": "/bff/order/prepay"}, {"body": b"{}"},
                           {"query": "orderCategoryId=0&page=2&size=1"}, {"v5": False}, {"business": None}):
                with self.subTest(change=change), self.assertRaisesRegex(EzMcdError, "probe_is_readonly"):
                    signer.headers(**(args | change))
            constants.assert_not_called()
            with self.assertRaisesRegex(EzMcdError, "runtime_expired"):
                signer.headers("POST", "/bff/order/orders", body=b"{}", business=("1001", "202"), v5=True)

    def test_transport_only_performs_one_fixed_get(self):
        transport = Transport(self.record); transport.signer = Mock()
        transport.signer.headers.return_value = {"test": "synthetic"}
        with patch.object(transport, "_open", return_value={}) as request:
            transport.verify_runtime(historical=True)
        request.assert_called_once_with("GET", "https://api.mcd.cn/bff/order/orders?orderCategoryId=0&page=1&size=1", {"test": "synthetic"}, None)
        self.assertTrue(transport.signer.headers.call_args.kwargs["historical_runtime_probe"])


if __name__ == "__main__":
    unittest.main()
