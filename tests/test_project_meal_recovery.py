from datetime import datetime, timedelta
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from ezmcd.accounts import revoke_local
from ezmcd.cli import main
from ezmcd.demo import DemoProvider, setup_demo
from ezmcd.models import CheckoutInput, Policy, SHANGHAI
from ezmcd.preferences import MealPreferences, plan_meal, save_preferences
from ezmcd.service import Service
from ezmcd.storage import Store


class MealRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root)
        setup_demo(self.store)
        self.service = Service(self.store)
        self.when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account="demo", store_code="9000001", pickup_at=self.when.isoformat(), max_amount_cny="20")
        self.preferences = MealPreferences(account="demo", store_code="9000001", pickup_time="10:15",
                                           items=[{"name": "演示汉堡"}], max_amount_cny="20")
        save_preferences(self.store, self.preferences)
        self.serial = 0

    def seed(self, state="UNKNOWN", args=None):
        self.serial += 1
        args = args or self.args
        with patch("ezmcd.demo.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat(args.pickup_at) - timedelta(hours=1)
            view = self.service.quote(args)
        identity = self.store.account(args.account)["identity"]
        quote = self.store.quote(identity, view["quote_id"])
        attempt = {"attempt_id": "test-attempt-" + str(self.serial), "quote_id": quote["quote_id"], "state": state,
            "pickup_at": args.pickup_at, "store_code": args.store_code, "amount_cents": 1000,
            "wallet_before_cents": 10000, "paid_verified": state == "PAID", "simulation": True,
            "pay_id": "private-pay-reference", "internal": "private-journal-data"}
        if state == "PAID": attempt["order_id"] = "test-order-" + str(self.serial)
        self.store.reserve(identity, "test-key-" + str(self.serial), quote,
            Policy(allow_checkout=True, daily_budget_cents=20000, max_orders_per_day=20), attempt)
        return attempt

    def plan(self, *, date=None, account=None):
        return plan_meal(self.service, account, date if date is not None else self.when.date().isoformat())

    def test_paid_purchase_is_recovered_after_restart_before_any_network(self):
        quote = self.service.quote(self.args)
        paid = self.service.submit("demo", quote["quote_id"], "original-purchase")
        self.service = Service(Store(self.root))
        with patch.object(Service, "provider", side_effect=AssertionError("No network provider")):
            result = self.plan()
        self.assertTrue(result["existing_purchase"])
        self.assertFalse(result["selection_ready"])
        self.assertFalse(result["quote_required"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["existing_orders"][0]["attempt_id"], paid["attempt_id"])
        self.assertEqual(result["existing_orders"][0]["items"][0]["name"], "演示汉堡")
        self.assertFalse(result["live_status_checked"])
        self.assertEqual(result["existing_orders"][0]["next_action"], "query_order_status")

    def test_unknown_reserved_unpaid_and_cancelled_never_become_a_new_draft(self):
        attempt = self.seed()
        for state in ("RESERVED", "CREATED", "PAYMENT_ATTEMPTED", "UNKNOWN", "PAID", "CANCELLED"):
            attempt.update(state=state, paid_verified=state == "PAID")
            self.store.update_attempt(self.store.account("demo")["identity"], attempt)
            with self.subTest(state=state), patch.object(Service, "provider", side_effect=AssertionError("No provider")):
                result = self.plan()
                self.assertEqual(result["existing_orders"][0]["state"], state)
                self.assertEqual(result["existing_orders"][0]["next_action"], "official_orders_list")
                self.assertFalse(result["selection_ready"])
                self.assertNotIn("private-", json.dumps(result))

    def test_revoked_or_interrupted_session_can_still_recover_locally(self):
        attempt = self.seed("PAID")
        for state in ("CLAIMED", "CANDIDATE", "STOPPED"):
            record = self.store.account("demo"); record["session_refresh"] = {"state": state}
            self.store.update_account(record)
            self.assertEqual(self.plan()["existing_orders"][0]["attempt_id"], attempt["attempt_id"])
        revoke_local(self.store, "demo")
        self.assertTrue(self.plan()["existing_purchase"])

    def test_explicit_past_date_recovers_even_if_weekday_preference_changed(self):
        past = self.when - timedelta(days=4)
        self.seed("PAID", self.args.model_copy(update={"pickup_at": past.isoformat()}))
        self.preferences.weekdays = [(past.weekday() + 1) % 7]
        save_preferences(self.store, self.preferences)
        with patch.object(Service, "provider", side_effect=AssertionError("No provider")):
            result = self.plan(date=past.date().isoformat())
        self.assertTrue(result["existing_purchase"])
        self.assertEqual(result["pickup_at"], past.isoformat())

    def test_changed_meal_price_or_pickup_method_cannot_replace_existing_scope(self):
        self.seed("PAID")
        self.preferences.items[0].name = "另一款餐品"
        self.preferences.max_amount_cny = "5.00"
        self.preferences.eat_type = "take-in-store"
        save_preferences(self.store, self.preferences)
        result = self.plan()
        self.assertTrue(result["existing_purchase"])
        self.assertEqual(result["existing_orders"][0]["items"][0]["name"], "演示汉堡")
        self.assertEqual(result["existing_orders"][0]["amount_cny"], "10.00")

    def test_same_time_at_another_store_or_account_does_not_match(self):
        setup_demo(self.store, "other")
        self.seed("PAID", self.args.model_copy(update={"account": "other"}))
        self.seed("PAID", self.args.model_copy(update={"store_code": "9000002"}))
        self.seed("PAID", self.args.model_copy(update={"pickup_at": (self.when + timedelta(minutes=5)).isoformat()}))
        result = self.plan()
        self.assertTrue(result["selection_ready"])
        self.assertFalse(result["existing_purchase"])
        self.assertEqual(result["existing_orders"], [])

    def test_implicit_date_recovers_next_preferred_weekday_without_session(self):
        self.seed("PAID")
        self.preferences.weekdays = [self.when.weekday()]
        save_preferences(self.store, self.preferences)
        with patch("ezmcd.preferences.datetime", wraps=datetime) as clock, \
                patch.object(Service, "provider", side_effect=AssertionError("No provider")):
            clock.now.return_value = self.when - timedelta(days=2)
            result = plan_meal(self.service)
        self.assertEqual(result["pickup_at"], self.when.isoformat())

    def test_implicit_date_does_not_return_a_past_breakfast_instead_of_next_meal(self):
        self.seed("PAID")
        later = self.when + timedelta(days=1)
        slots = {"dates": [{"date": later.date().isoformat(), "periods": [{"pickup_times": [later.isoformat()]}]}]}
        with patch("ezmcd.preferences.datetime", wraps=datetime) as clock, \
                patch.object(DemoProvider, "pickup_slots", return_value=slots):
            clock.now.return_value = self.when + timedelta(minutes=1)
            result = plan_meal(self.service)
        self.assertTrue(result["selection_ready"])
        self.assertEqual(result["request"]["pickup_at"], later.isoformat())

    def test_later_live_slot_is_checked_before_loading_menu(self):
        self.seed("PAID")
        slots = {"dates": [{"date": self.when.date().isoformat(), "periods": [{"pickup_times": [self.when.isoformat()]}]}]}
        with patch("ezmcd.preferences.datetime", wraps=datetime) as clock, \
                patch.object(DemoProvider, "pickup_slots", return_value=slots), \
                patch.object(DemoProvider, "menu", side_effect=AssertionError("Do not rebuild menu")):
            clock.now.return_value = self.when - timedelta(days=1, hours=1)
            self.assertTrue(plan_meal(self.service)["existing_purchase"])

    def test_unrelated_newer_records_and_missing_quote_do_not_hide_purchase(self):
        attempt = self.seed()
        self.seed("PAID", self.args.model_copy(update={"store_code": "9000002"}))
        with self.store.connect() as db:
            db.execute("DELETE FROM quotes WHERE id=?", (attempt["quote_id"],))
        result = self.plan()
        self.assertEqual(len(result["existing_orders"]), 1)
        self.assertEqual(result["existing_orders"][0]["attempt_id"], attempt["attempt_id"])
        self.assertFalse(result["existing_orders"][0]["meal_details_available"])

    def test_local_recovery_does_not_modify_accounts_preferences_or_journal(self):
        self.seed("PAID")
        paths = [*self.root.joinpath("accounts").glob("*.json"), self.root / "preferences.json", self.root / "journal.sqlite3"]
        before = {p: sha256(p.read_bytes()).hexdigest() for p in paths}
        for _ in range(2): self.plan()
        self.assertEqual(before, {p: sha256(p.read_bytes()).hexdigest() for p in paths})

    def test_cli_recovery_requires_no_session_and_exposes_only_public_summary(self):
        attempt = self.seed()
        args = ["ezmcd", "--data-dir", str(self.root), "preferences", "plan", "--date", self.when.date().isoformat()]
        with patch.object(sys, "argv", args), patch("sys.stdout", new_callable=io.StringIO) as output, \
                patch.object(Service, "provider", side_effect=AssertionError("No provider")):
            main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["existing_orders"][0]["attempt_id"], attempt["attempt_id"])
        self.assertFalse(result["payment_attempted"])
        self.assertNotIn("private-", output.getvalue())


class MealRecoveryMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulated_paid_meal_is_found_by_new_stdio_process_after_revocation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)); setup_demo(store)
            service = Service(store)
            when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
            args = CheckoutInput(account="demo", store_code="9000001", pickup_at=when.isoformat(), max_amount_cny="20")
            save_preferences(store, MealPreferences(account="demo", store_code=args.store_code, pickup_time="10:15",
                items=[{"name": "演示汉堡"}], max_amount_cny="20"))
            quote = service.quote(args)
            paid = service.submit("demo", quote["quote_id"], "only-purchase")
            revoke_local(store, "demo")
            params = StdioServerParameters(command=sys.executable, args=["-m", "ezmcd.mcp_server"],
                                           env={"EZMCD_DATA_DIR": str(store.root)})
            for _ in range(2):
                async with Client(params) as client:
                    response = await client.call_tool("meal_plan", {"date": when.date().isoformat()})
                    self.assertFalse(response.is_error)
                    result = json.loads(response.content[0].text)
                    self.assertTrue(result["existing_purchase"])
                    self.assertFalse(result["selection_ready"])
                    self.assertEqual(result["existing_orders"][0]["attempt_id"], paid["attempt_id"])
            with store.connect() as db:
                self.assertEqual(dict(db.execute("SELECT stage,count(*) FROM writes GROUP BY stage")),
                                 {"order": 1, "preorder": 1, "consume": 1})


if __name__ == "__main__":
    unittest.main()
