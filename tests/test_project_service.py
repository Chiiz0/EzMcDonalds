from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ezmcd.demo import DemoProvider, setup_demo
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, Policy, SHANGHAI
from ezmcd.service import Service
from ezmcd.storage import Store


class ProjectCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        setup_demo(self.store)
        self.service = Service(self.store)
        pickup = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        self.args = CheckoutInput(account="demo", store_code="9000001", pickup_at=pickup.isoformat(), max_amount_cny="20.00")

    def quote(self):
        return self.service.quote(self.args)

    def test_full_flow_duplicate_key_new_key_and_restart_only_charge_once(self):
        q = self.quote()
        first = self.service.submit("demo", q["quote_id"], "breakfast")
        self.assertTrue(first["simulation"])
        self.assertTrue(first["paid_verified"])
        self.assertEqual(first["wallet_balance_cny"], "90.00")
        restarted = Service(Store(Path(self.temp.name)))
        for key in ("breakfast", "agent-generated-another-key"):
            repeated = restarted.submit("demo", q["quote_id"], key)
            self.assertEqual(repeated["order_id"], first["order_id"])
            self.assertEqual(repeated["wallet_balance_cny"], "90.00")
        next_args = self.args.model_copy(update={"store_code": "9000002"})
        next_quote = restarted.quote(next_args)
        with self.assertRaisesRegex(EzMcdError, "idempotency_key_reused"):
            restarted.submit("demo", next_quote["quote_id"], "agent-generated-another-key")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 1)

    def test_account_and_quote_isolation(self):
        q = self.quote()
        setup_demo(self.store, "someone-else")
        with self.assertRaisesRegex(EzMcdError, "quote_not_found_for_account"):
            self.service.submit("someone-else", q["quote_id"], "key")

    def test_quote_remains_reviewable_with_empty_wallet_but_cannot_create_order(self):
        with patch.object(DemoProvider, "wallet", return_value={"balance_cents": 0}), \
                patch.object(DemoProvider, "create_order") as create:
            q = self.quote()
            self.assertEqual(q["amount_cny"], "10.00")
            self.assertFalse(q["wallet_sufficient"])
            self.assertEqual(q["wallet_shortfall_cny"], "10.00")
            self.assertIn("insufficient_wallet_balance", q["checkout_blockers"])
            with self.assertRaisesRegex(EzMcdError, "insufficient_wallet_balance"):
                self.service.submit("demo", q["quote_id"], "empty-wallet")
            create.assert_not_called()
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)

    def test_alias_cannot_reset_identity_budget(self):
        old = self.store.account("demo")
        with self.assertRaisesRegex(EzMcdError, "account_exists"):
            self.store.add_account(old | {"alias": "another-name"})

    def test_unknown_create_never_retries(self):
        q = self.quote()
        with patch.object(DemoProvider, "create_order", side_effect=EzMcdError("synthetic_timeout")) as create:
            first = self.service.submit("demo", q["quote_id"], "key")
            second = self.service.submit("demo", q["quote_id"], "new-key")
            self.assertEqual(first["state"], "UNKNOWN")
            self.assertEqual(second["attempt_id"], first["attempt_id"])
            self.assertEqual(create.call_count, 1)

    def test_unknown_create_cannot_be_replaced_by_changing_cart_and_key(self):
        q = self.quote()
        with patch.object(DemoProvider, "create_order", side_effect=EzMcdError("synthetic_timeout")) as create:
            first = self.service.submit("demo", q["quote_id"], "first")
            self.assertEqual(first["state"], "UNKNOWN")
            _, provider = self.service.provider("demo")
            provider.write_cart({"quantity": 2})
            changed = self.quote()
            with self.assertRaisesRegex(EzMcdError, "existing_local_attempt"):
                self.service.submit("demo", changed["quote_id"], "different-key")
            self.assertEqual(create.call_count, 1)

    def test_timeout_after_success_reconciles_without_second_consume(self):
        original = DemoProvider.consume
        def timeout_after_success(provider, inputs):
            original(provider, inputs)
            raise EzMcdError("synthetic_timeout")
        q = self.quote()
        with patch.object(DemoProvider, "consume", timeout_after_success):
            result = self.service.submit("demo", q["quote_id"], "key")
        self.assertTrue(result["paid_verified"])
        self.assertTrue(self.service.pay_existing("demo", result["attempt_id"])["paid_verified"])
        self.assertEqual(self.service.check_account("demo")["wallet_balance_cny"], "90.00")

    def test_unknown_consume_and_payment_reentry_never_charge_again(self):
        q = self.quote()
        with patch.object(DemoProvider, "consume", side_effect=EzMcdError("synthetic_timeout")) as consume:
            result = self.service.submit("demo", q["quote_id"], "key")
            self.service.pay_existing("demo", result["attempt_id"])
            self.service.submit("demo", q["quote_id"], "new-key")
            self.assertEqual(consume.call_count, 1)
        self.assertFalse(result["paid_verified"])
        self.assertEqual(result["wallet_balance_cny"], "100.00")

    def test_delayed_settlement_queries_until_paid_without_repeating_writes(self):
        q = self.quote()
        original = DemoProvider.status
        calls = []
        def delayed(provider, attempt):
            state = original(provider, attempt)
            calls.append(attempt["order_id"])
            if len(calls) < 3:
                state["cashier"]["payStatus"] = "PROCESSING"
            state["order"]["pickupCode"] = ""
            return state
        with patch.object(DemoProvider, "status", delayed), patch("ezmcd.service.time.sleep") as sleep:
            result = self.service.submit("demo", q["quote_id"], "delayed")
        self.assertTrue(result["paid_verified"])
        self.assertTrue(result["pickup_code_pending"])
        self.assertEqual(result["next_action"], "query_pickup_code_later")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])
        self.assertEqual(calls, [result["order_id"]] * 3)
        with self.store.connect() as conn:
            self.assertEqual(dict(conn.execute("SELECT stage,count(*) FROM writes GROUP BY stage")),
                             {"order": 1, "preorder": 1, "consume": 1})

    def test_pending_settlement_is_bounded_and_repeat_only_queries_original_order(self):
        q = self.quote()
        original = DemoProvider.status
        def pending(provider, attempt):
            state = original(provider, attempt)
            state["cashier"]["payStatus"] = "PROCESSING"
            return state
        with patch.object(DemoProvider, "status", autospec=True, side_effect=pending) as status, \
                patch("ezmcd.service.time.sleep") as sleep:
            result = self.service.submit("demo", q["quote_id"], "pending")
            self.assertEqual(status.call_count, 4)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4])
            self.assertEqual(result["state"], "UNKNOWN")
            self.assertEqual(result["next_action"], "query_order_status")
            self.assertFalse(result["paid_verified"])
            self.assertFalse(result["automatic_write_retry"])
            self.service.set_policy("demo", Policy())
            repeated = Service(self.store).submit("demo", q["quote_id"], "pending")
            self.assertEqual(status.call_count, 5)
            self.assertEqual(sleep.call_count, 3)
            self.assertEqual(repeated["attempt_id"], result["attempt_id"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 3)
        self.assertEqual(self.service.check_account("demo")["wallet_balance_cny"], "90.00")

    def test_failed_status_query_stops_waiting_without_another_consume(self):
        q = self.quote()
        with patch.object(DemoProvider, "status", side_effect=EzMcdError("synthetic_timeout")) as status, \
                patch("ezmcd.service.time.sleep") as sleep:
            result = self.service.submit("demo", q["quote_id"], "query-failure")
            status.assert_called_once()
            sleep.assert_not_called()
        self.assertTrue(result["status_query_failed"])
        self.assertEqual(result["next_action"], "query_order_status")
        self.assertFalse(result["paid_verified"])
        self.assertTrue(self.service.status("demo", result["attempt_id"])["paid_verified"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 3)

    def test_rejected_consume_does_not_start_settlement_wait(self):
        q = self.quote()
        with patch.object(DemoProvider, "consume", return_value={"http_status": 200, "response": {"code": "420"}}) as consume, \
                patch("ezmcd.service.time.sleep") as sleep:
            result = self.service.submit("demo", q["quote_id"], "rejected")
            consume.assert_called_once()
            sleep.assert_not_called()
        self.assertEqual(result["stop_reason"], "payment_requires_official_app_or_status_query")
        self.assertFalse(result["paid_verified"])
        self.assertEqual(result["next_action"], "query_order_status")

    def test_settlement_wait_never_waives_wallet_amount_check(self):
        q = self.quote()
        original = DemoProvider.status
        def wrong_balance(provider, attempt):
            state = original(provider, attempt)
            state["wallet_cents"] += 1
            return state
        with patch.object(DemoProvider, "status", wrong_balance), patch("ezmcd.service.time.sleep"):
            result = self.service.submit("demo", q["quote_id"], "balance-mismatch")
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertFalse(result["paid_verified"])

    def test_disabled_revoked_expired_and_store_restricted_policy(self):
        q = self.quote()
        policies = [Policy(), Policy(allow_checkout=True, allowed_stores=["9000002"]),
            Policy(allow_checkout=True, expires_at="2020-01-01T00:00:00+08:00"),
            Policy(allow_checkout=True, max_order_cents=500)]
        for policy in policies:
            self.service.set_policy("demo", policy)
            with self.assertRaises(EzMcdError):
                self.service.submit("demo", q["quote_id"], "key")
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM writes").fetchone()[0], 0)

    def test_expired_quote_and_price_change_stop_before_reservation(self):
        q = self.quote()
        with patch("ezmcd.service.time.time", return_value=q["expires_at"] + 1):
            with self.assertRaisesRegex(EzMcdError, "quote_expired"):
                self.service.submit("demo", q["quote_id"], "key")
        original = DemoProvider.quote
        def changed(provider, args):
            result = original(provider, args)
            result["checkout_digest"] = "changed"
            return result
        with patch.object(DemoProvider, "quote", changed):
            with self.assertRaisesRegex(EzMcdError, "checkout_changed"):
                self.service.submit("demo", q["quote_id"], "key")

    def test_daily_budget_and_different_order_key_collision(self):
        self.service.set_policy("demo", Policy(allow_checkout=True, max_order_cents=2000,
            daily_budget_cents=1500, max_orders_per_day=2))
        q = self.quote()
        self.service.submit("demo", q["quote_id"], "key")
        self.args = self.args.model_copy(update={"store_code": "9000002"})
        q2 = self.quote()
        with self.assertRaisesRegex(EzMcdError, "idempotency_key_reused"):
            self.service.submit("demo", q2["quote_id"], "key")
        with self.assertRaisesRegex(EzMcdError, "daily_order_or_budget"):
            self.service.submit("demo", q2["quote_id"], "key2")

    def test_cancelled_with_historical_paid_is_not_active_paid(self):
        q = self.quote()
        result = self.service.submit("demo", q["quote_id"], "key")
        original = DemoProvider.status
        def cancelled(provider, attempt):
            state = original(provider, attempt)
            state["order"]["orderStatusTitle"] = "已取消"
            state["wallet_cents"] = 10000
            return state
        with patch.object(DemoProvider, "status", cancelled):
            result = self.service.status("demo", result["attempt_id"])
        self.assertEqual(result["state"], "CANCELLED")
        self.assertFalse(result["paid_verified"])
        self.assertEqual(result["next_action"], "review_cancelled_order")

    def test_exclusive_account_lock(self):
        identity = self.store.account("demo")["identity"]
        with self.store.lock(identity):
            with self.assertRaisesRegex(EzMcdError, "account_busy"):
                self.quote()

    def test_private_permissions_and_path_traversal(self):
        self.assertEqual((self.store.root / "journal.sqlite3").stat().st_mode & 0o777, 0o600)
        for alias in ("../demo", "/etc/passwd", "x/y"):
            with self.assertRaisesRegex(EzMcdError, "invalid_account_alias"):
                self.store.account(alias)
