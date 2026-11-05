"""Slow read/preparation steps must not carry expired authority into writes."""
from datetime import datetime, timezone
import time
import unittest
from unittest.mock import patch

from ezmcd.demo import DemoProvider
from ezmcd.errors import EzMcdError, error_result
from ezmcd.models import Policy
from ezmcd.service import Service
from ezmcd.transport import Transport
import test_project_service as checkout


class CheckoutDeadlineTests(unittest.TestCase):
    def setUp(self):
        checkout.ProjectCheckoutTests.setUp(self)
        self.now = time.time()
        self.start = self.now
        self.deadline = (int(self.now) // 60 + 1) * 60
        owner = self
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(owner.now, tz)
        for guard in (patch("ezmcd.service.datetime", Clock),
                      patch("ezmcd.service.time.time", side_effect=lambda: self.now),
                      patch.object(Transport, "_open", side_effect=AssertionError("No real network in deadline tests"))):
            guard.start(); self.addCleanup(guard.stop)
        self.service.set_policy("demo", Policy(allow_checkout=True,
            expires_at=datetime.fromtimestamp(self.deadline, timezone.utc).isoformat()))
        self.quote = self.service.quote(self.args)

    def delayed(self, method, until):
        original = getattr(DemoProvider, method)
        def call(provider, *args, **kwargs):
            value = original(provider, *args, **kwargs)
            self.now = until
            return value
        return patch.object(DemoProvider, method, call)

    def writes(self):
        with self.store.connect() as db:
            return dict(db.execute("SELECT stage,count(*) FROM writes GROUP BY stage").fetchall())

    def assert_no_attempts(self):
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM request_keys").fetchone()[0], 0)
        self.assertEqual(self.writes(), {})

    def test_quote_expiring_during_duplicate_preflight_stops_before_reservation(self):
        self.service.set_policy("demo", Policy(allow_checkout=True))
        with self.delayed("preflight", self.quote["expires_at"]), patch.object(DemoProvider, "create_order") as create:
            with self.assertRaisesRegex(EzMcdError, "quote_expired_get_new_quote"):
                self.service.submit("demo", self.quote["quote_id"], "slow-preflight")
        create.assert_not_called(); self.assert_no_attempts()

    def test_quote_expiring_during_final_wallet_read_stops_before_reservation(self):
        self.service.set_policy("demo", Policy(allow_checkout=True))
        with self.delayed("wallet", self.quote["expires_at"] + 1), patch.object(DemoProvider, "create_order") as create:
            with self.assertRaisesRegex(EzMcdError, "quote_expired_get_new_quote"):
                self.service.submit("demo", self.quote["quote_id"], "slow-wallet")
        create.assert_not_called(); self.assert_no_attempts()

    def test_policy_expiring_during_any_preparation_read_stops_without_reserving(self):
        for method in ("quote", "preflight", "wallet"):
            self.now = self.start
            with self.subTest(method=method), self.delayed(method, self.deadline), patch.object(DemoProvider, "create_order") as create:
                with self.assertRaisesRegex(EzMcdError, "checkout_policy_expired"):
                    self.service.submit("demo", self.quote["quote_id"], "policy-deadline")
                create.assert_not_called(); self.assert_no_attempts()

    def test_expiry_during_payment_checks_never_sends_preorder(self):
        with self.delayed("payment_ready", self.deadline), patch.object(DemoProvider, "preorder") as preorder:
            result = self.service.submit("demo", self.quote["quote_id"], "slow-payment-check")
        preorder.assert_not_called()
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["stop_reason"], "checkout_policy_expired")
        self.assertEqual(self.writes(), {"order": 1})
        self.assertFalse(result["paid_verified"])
        self.assertEqual(result["wallet_balance_cny"], "100.00")

    def test_expiry_during_preorder_never_sends_wallet_consume_or_retries(self):
        with self.delayed("preorder", self.deadline), patch.object(DemoProvider, "consume") as consume:
            result = self.service.submit("demo", self.quote["quote_id"], "slow-preorder")
        consume.assert_not_called()
        self.assertEqual(result["stop_reason"], "checkout_policy_expired")
        self.assertEqual(self.writes(), {"order": 1, "preorder": 1})
        self.assertFalse(result["paid_verified"])
        # A new policy does not turn an interrupted purchase into a fresh try.
        self.service.set_policy("demo", Policy(allow_checkout=True))
        restarted = Service(self.store)
        repeated = restarted.submit("demo", self.quote["quote_id"], "slow-preorder")
        recovered = restarted.pay_existing("demo", result["attempt_id"])
        self.assertEqual(repeated["attempt_id"], result["attempt_id"])
        self.assertEqual(recovered["state"], "UNKNOWN")
        self.assertEqual(self.writes(), {"order": 1, "preorder": 1})
        self.assertEqual(recovered["wallet_balance_cny"], "100.00")

    def test_expiry_while_preparing_wallet_inputs_never_sends_consume(self):
        with self.delayed("wallet_inputs", self.deadline), patch.object(DemoProvider, "consume") as consume:
            result = self.service.submit("demo", self.quote["quote_id"], "slow-wallet-inputs")
        consume.assert_not_called()
        self.assertEqual(result["stop_reason"], "checkout_policy_expired")
        self.assertEqual(self.writes(), {"order": 1, "preorder": 1})

    def test_quote_expiry_after_order_creation_does_not_replace_order_or_stop_authorized_payment(self):
        self.service.set_policy("demo", Policy(allow_checkout=True))
        with self.delayed("payment_ready", self.quote["expires_at"] + 1):
            result = self.service.submit("demo", self.quote["quote_id"], "accepted-order")
        self.assertTrue(result["paid_verified"])
        self.assertEqual(self.writes(), {"order": 1, "preorder": 1, "consume": 1})

    def test_expiry_while_persisting_write_claim_keeps_claim_but_does_not_send(self):
        original = self.store.claim
        def delayed(attempt, stage):
            original(attempt, stage)
            self.now = self.deadline
        with patch.object(self.store, "claim", delayed), patch.object(DemoProvider, "create_order") as create:
            result = self.service.submit("demo", self.quote["quote_id"], "slow-journal")
        create.assert_not_called()
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["stop_reason"], "checkout_policy_expired")
        self.assertEqual(self.writes(), {"order": 1})
        repeated = Service(self.store).submit("demo", self.quote["quote_id"], "slow-journal")
        self.assertEqual(repeated["attempt_id"], result["attempt_id"])
        self.assertEqual(self.writes(), {"order": 1})

    def test_deadline_messages_do_not_authorize_repeating_an_interrupted_purchase(self):
        for code in ("checkout_policy_expired", "quote_expired_get_new_quote"):
            result = error_result(code)
            self.assertEqual(result["error"], code)
            self.assertFalse(result["automatic_write_retry"])
            self.assertIn("原单", result["next_action"])


if __name__ == "__main__":
    unittest.main()
