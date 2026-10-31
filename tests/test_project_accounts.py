from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ezmcd.accounts import connect_samples, profile_from_balance_sample, revoke_local
from ezmcd.capture import samples_from_har
from ezmcd.errors import EzMcdError
from ezmcd.models import Policy
from ezmcd.protocol.signing import sign_v4
from ezmcd.service import Service
from ezmcd.storage import Store, write_private

KEYS = ("synthetic-access", "synthetic-secret", (), ())


def sample():
    path = "/bff/member/mwallet/balance"
    headers = {"sid": "synthetic-session", "token": "synthetic-device", "meddyid": "synthetic-member",
               "ct": "101", "v": "test", "language": "zh", "sv": "v4", "x-mcd-gw-v": "1"}
    headers.update(sign_v4(headers, *KEYS[:2], datetime.now(timezone.utc), "GET", path))
    return {"request": {"host": "api.mcd.cn", "method": "GET", "path": path, "body": "", "headers": headers},
            "response": {"status": 200, "body": json.dumps({"success": True, "data": {"balance": "1.00"}})}}


class AccountTests(unittest.TestCase):
    def test_har_only_uses_matching_account_and_never_replays_orders(self):
        def entry(path, method="GET", session="same"):
            return {"request": {"url": "https://api.mcd.cn" + path, "method": method,
                "headers": [{"name": "sid", "value": session}, {"name": "token", "value": "device"},
                            {"name": "x-mcd-sign", "value": "synthetic"}], "postData": {"text": "private-order-body"} if method == "POST" else {}},
                "response": {"status": 200, "content": {"text": '{"success":true}'}}}
        entries = [entry("/bff/member/mwallet/balance"), entry("/bff/order/orders", "POST"),
                   entry("/bff/order/orders", "POST", "other"), entry("/unrelated/private")]
        balance, runtime = samples_from_har({"log": {"entries": entries}})
        self.assertEqual(runtime["request"]["headers"]["sid"], "same")
        self.assertEqual(runtime["request"]["body"], "")
        self.assertNotIn("private-order-body", json.dumps((balance, runtime)))
        entries.append(entry("/bff/member/mwallet/balance", session="other"))
        with self.assertRaisesRegex(EzMcdError, "one_official_session"):
            samples_from_har({"log": {"entries": entries}})

    def test_har_rejects_lookalike_origins(self):
        with self.assertRaises(EzMcdError):
            samples_from_har({"log": {"entries": [{"request": {"url": "https://api.mcd.cn.attacker.example/bff/member/mwallet/balance"}}]}})

    def test_balance_sample_signature_and_origin_are_verified(self):
        with patch("ezmcd.accounts.constants", return_value=KEYS):
            self.assertEqual(profile_from_balance_sample(sample(), "unused")["fields"]["sid"], "synthetic-session")
            altered = sample(); altered["request"]["headers"]["sid"] = "different-session"
            with self.assertRaisesRegex(EzMcdError, "invalid_official_sample_signature"):
                profile_from_balance_sample(altered, "unused")
            wrong_host = sample(); wrong_host["request"]["host"] = "other.example"
            with self.assertRaisesRegex(EzMcdError, "successful_balance_sample_required"):
                profile_from_balance_sample(wrong_host, "unused")

    def test_connect_checks_live_reconnect_preserves_policy_and_revoke_keeps_journal(self):
        with tempfile.TemporaryDirectory() as temp, patch("ezmcd.accounts.constants", return_value=KEYS), \
                patch("ezmcd.provider.McdProvider.wallet", return_value={"balance_cents": 100}):
            store = Store(Path(temp))
            path = store.root / "balance.json"
            write_private(path, sample())
            result = connect_samples(store, "mine", path, "unused", phone_label="13800000000")
            self.assertEqual(result["session_status"], "verified_now")
            self.assertEqual(result["phone_label"], "138****0000")
            self.assertNotIn("synthetic-session", json.dumps(result))
            Service(store).set_policy("mine", Policy(allow_checkout=True, max_order_cents=1234))
            result = connect_samples(store, "mine", path, "unused", update=True)
            self.assertEqual(result["policy"]["max_order_cents"], 1234)
            store.claim("synthetic-attempt", "consume")
            revoke_local(store, "mine")
            self.assertNotIn("profile", store.account("mine"))
            with self.assertRaisesRegex(EzMcdError, "account_revoked"):
                Service(store).check_account("mine")
            with self.assertRaisesRegex(EzMcdError, "account_revoked"):
                Service(store).set_policy("mine", Policy(allow_checkout=True))
            connect_samples(store, "mine", path, "unused", update=True)
            self.assertFalse(store.account("mine")["policy"]["allow_checkout"])
            with store.connect() as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 1)

    def test_rejected_live_session_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as temp, patch("ezmcd.accounts.constants", return_value=KEYS), \
                patch("ezmcd.provider.McdProvider.wallet", side_effect=EzMcdError("session_expired")):
            store = Store(Path(temp)); path = store.root / "balance.json"; write_private(path, sample())
            with self.assertRaisesRegex(EzMcdError, "session_expired"):
                connect_samples(store, "mine", path, "unused")
            self.assertEqual(store.accounts(), [])
