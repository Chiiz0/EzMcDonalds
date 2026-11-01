import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ezmcd.diagnostics import doctor, credentials_guide
from ezmcd.errors import EzMcdError, error_result
from ezmcd.models import Policy, fingerprint
from ezmcd.service import Service
from ezmcd.storage import Store
from ezmcd.transport import data


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


if __name__ == "__main__":
    unittest.main()
