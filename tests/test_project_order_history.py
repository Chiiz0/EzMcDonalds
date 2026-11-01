from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ezmcd.accounts import revoke_local
from ezmcd.cli import main
from ezmcd.demo import setup_demo
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, Policy, SHANGHAI
from ezmcd.service import Service
from ezmcd.storage import Store


class OrderHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        setup_demo(self.store)
        self.service = Service(self.store)
        pickup = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account="demo", store_code="9000001", pickup_at=pickup.isoformat(), max_amount_cny="20.00")
        self.serial = 0

    def seed(self, state, *, days=0, account="demo"):
        self.serial += 1
        pickup = datetime.fromisoformat(self.args.pickup_at) + timedelta(days=days, minutes=self.serial * 5)
        args = self.args.model_copy(update={"account": account, "pickup_at": pickup.isoformat()})
        view = self.service.quote(args)
        identity = self.store.account(account)["identity"]
        quote = self.store.quote(identity, view["quote_id"])
        attempt = {"attempt_id": f"attempt-{self.serial}", "quote_id": view["quote_id"], "state": state,
                   "pickup_at": args.pickup_at, "store_code": args.store_code, "amount_cents": 1000,
                   "wallet_before_cents": 10000, "paid_verified": state == "PAID", "simulation": True,
                   "pay_id": "private-pay-reference", "internal": "private-journal-data"}
        if state == "PAID":
            attempt.update(order_id=f"order-{self.serial}", status_checked_at=1000.0)
        self.store.reserve(identity, f"key-{self.serial}", quote,
                           Policy(allow_checkout=True, daily_budget_cents=20000, max_orders_per_day=20), attempt)
        return attempt

    def test_page_recovers_paid_meal_without_exposing_internal_payment_data(self):
        attempt = self.seed("PAID")
        factory = Mock(side_effect=AssertionError("history must not construct a provider"))
        result = Service(Store(self.root), factory).orders("demo")
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["live_status_checked"])
        self.assertFalse(result["includes_official_app_orders"])
        self.assertEqual(result["source"], "local_project_journal")
        row = result["orders"][0]
        self.assertEqual(row["attempt_id"], attempt["attempt_id"])
        self.assertTrue(row["meal_details_available"])
        self.assertEqual(row["items"][0]["name"], "演示汉堡")
        self.assertEqual(row["amount_cny"], "10.00")
        self.assertEqual(row["status_checked_at"], 1000.0)
        self.assertEqual(row["next_action"], "query_order_status")
        self.assertNotIn("private-", json.dumps(result))
        self.assertNotIn("key-1", json.dumps(result))
        factory.assert_not_called()

    def test_unknown_without_order_response_remains_visible_and_query_only(self):
        attempt = self.seed("UNKNOWN")
        row = self.service.orders("demo")["orders"][0]
        self.assertEqual(row["attempt_id"], attempt["attempt_id"])
        self.assertIsNone(row["order_id"])
        self.assertFalse(row["automatic_write_retry"])
        self.assertEqual(row["next_action"], "official_orders_list")

    def test_filters_and_pagination_never_hide_other_accounts_in_total(self):
        first = self.seed("UNKNOWN")
        second = self.seed("PAID", days=1)
        third = self.seed("CANCELLED", days=1)
        setup_demo(self.store, "other")
        self.seed("PAID", days=2, account="other")
        first_page = self.service.orders("demo", limit=1)
        self.assertEqual(first_page["total"], 3)
        self.assertTrue(first_page["has_more"])
        self.assertEqual(first_page["orders"][0]["attempt_id"], third["attempt_id"])
        second_page = self.service.orders("demo", limit=1, offset=1)
        self.assertEqual(second_page["orders"][0]["attempt_id"], second["attempt_id"])
        last_page = self.service.orders("demo", limit=1, offset=2)
        self.assertFalse(last_page["has_more"])
        self.assertEqual(last_page["orders"][0]["attempt_id"], first["attempt_id"])
        filtered = self.service.orders("demo", pickup_date=second["pickup_at"][:10], state="PAID")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["orders"][0]["attempt_id"], second["attempt_id"])
        self.assertEqual(self.service.orders("other")["total"], 1)

    def test_revoked_or_stopped_account_can_still_recover_its_local_records(self):
        attempt = self.seed("PAID")
        for state in ("CLAIMED", "CANDIDATE", "STOPPED"):
            record = self.store.account("demo")
            record["session_refresh"] = {"state": state}
            self.store.update_account(record)
            self.assertEqual(self.service.orders("demo")["orders"][0]["attempt_id"], attempt["attempt_id"])
        revoke_local(self.store, "demo")
        self.assertEqual(self.service.orders("demo")["total"], 1)
        with self.assertRaisesRegex(EzMcdError, "revoked"):
            self.service.status("demo", attempt["attempt_id"])

    def test_missing_or_wrong_identity_quote_does_not_erase_attempt_or_leak_details(self):
        attempt = self.seed("UNKNOWN")
        setup_demo(self.store, "other")
        self.seed("PAID", account="other")
        with self.store.connect() as db:
            db.execute("UPDATE quotes SET identity=? WHERE id=?",
                       (self.store.account("other")["identity"], attempt["quote_id"]))
        row = self.service.orders("demo")["orders"][0]
        self.assertFalse(row["meal_details_available"])
        self.assertIsNone(row["items"])
        self.assertEqual(row["attempt_id"], attempt["attempt_id"])

    def test_reading_history_leaves_accounts_budget_keys_and_writes_unchanged(self):
        self.seed("UNKNOWN")
        paths = list((self.root / "accounts").glob("*.json")) + [self.root / "journal.sqlite3"]
        def hashes():
            return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        before = hashes()
        self.service.orders("demo")
        self.service.orders("demo", state="UNKNOWN")
        self.assertEqual(before, hashes())

    def test_rejects_invalid_pagination_date_and_state_before_query(self):
        cases = [{"limit": 0}, {"limit": 101}, {"limit": True}, {"offset": -1}, {"offset": 10001},
                 {"pickup_date": "2026-02-30"}, {"pickup_date": "20261102"}, {"pickup_date": "2026-11-02T00:00:00+08:00"},
                 {"state": "paid"}, {"state": "PAID' OR 1=1"}]
        for args in cases:
            with self.subTest(args=args), self.assertRaisesRegex(EzMcdError, "invalid_order_history"):
                self.service.orders("demo", **args)
        self.assertEqual(self.service.orders("demo")["orders"], [])
        self.assertFalse(self.service.orders("demo", offset=10000)["has_more"])

    def test_cli_finds_history_with_date_and_state_filters(self):
        attempt = self.seed("PAID")
        args = ["ezmcd", "--data-dir", str(self.root), "orders", "demo", "--date", attempt["pickup_at"][:10],
                "--state", "PAID", "--limit", "1", "--offset", "0"]
        with patch.object(sys, "argv", args), patch("sys.stdout", new_callable=io.StringIO) as output:
            main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["orders"][0]["attempt_id"], attempt["attempt_id"])
        self.assertFalse(result["payment_attempted"])

    def test_live_status_supplies_check_time_then_history_retains_it(self):
        quote = self.service.quote(self.args)
        with patch("ezmcd.service.time.time", return_value=1234567890.0):
            receipt = self.service.submit("demo", quote["quote_id"], "once")
        self.assertEqual(receipt["status_checked_at"], 1234567890.0)
        restarted = Service(Store(self.root))
        record = restarted.orders("demo")["orders"][0]
        self.assertEqual(record["attempt_id"], receipt["attempt_id"])
        self.assertEqual(record["status_checked_at"], receipt["status_checked_at"])
        self.assertTrue(restarted.status("demo", record["attempt_id"])["paid_verified"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
