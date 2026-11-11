"""Local one-meal grants must not become authority for another purchase."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid

from ezmcd.demo import DemoProvider
from ezmcd.errors import EzMcdError
from ezmcd.models import Policy
from ezmcd.service import Service, quote_authorization_fingerprint
from ezmcd.transport import Transport
import test_project_service as checkout


class QuoteAuthorizationTests(unittest.TestCase):
    def setUp(self):
        checkout.ProjectCheckoutTests.setUp(self)
        self.service.set_policy("demo", Policy())
        self.q = self.service.quote(self.args)
        self.identity = self.store.account("demo")["identity"]
        self.raw = self.store.quote(self.identity, self.q["quote_id"])
        guard = patch.object(Transport, "_open", side_effect=AssertionError("No real network"))
        guard.start(); self.addCleanup(guard.stop)

    def grant(self, policy=None, quote_id=None):
        return self.service.set_policy("demo", policy or Policy(allow_checkout=True),
                                       quote_id=quote_id or self.q["quote_id"])

    def writes(self):
        with self.store.connect() as db:
            return dict(db.execute("SELECT stage,count(*) FROM writes GROUP BY stage"))

    def test_grant_is_local_bounded_reviewable_and_does_not_purchase(self):
        before = self.store.account("demo")
        result = self.grant()
        after = self.store.account("demo")
        self.assertEqual({k:v for k,v in before.items() if k != "policy"},
                         {k:v for k,v in after.items() if k != "policy"})
        self.assertTrue(result["one_purchase_content_only"])
        self.assertFalse(result["order_created"])
        self.assertFalse(result["payment_attempted"])
        self.assertEqual(result["authorized_quote"]["items"], self.q["items"])
        self.assertEqual(result["policy"]["quote_scope"]["content_fingerprint"],
                         quote_authorization_fingerprint(self.identity, self.raw))
        remaining = datetime.fromisoformat(result["policy"]["expires_at"]).timestamp()-time.time()
        self.assertGreater(remaining, 13*60)
        self.assertLessEqual(remaining, 15*60)
        self.assertEqual(self.writes(), {})

    def test_fresh_identical_quote_and_repeat_grant_preserve_first_scope_and_deadline(self):
        first = self.grant()
        fresh = self.service.quote(self.args)
        self.assertNotEqual(fresh["quote_id"], self.q["quote_id"])
        repeated = self.grant(quote_id=fresh["quote_id"])
        self.assertEqual(first["policy"], repeated["policy"])
        paid = Service(self.store).submit("demo", fresh["quote_id"], "one-meal")
        self.assertTrue(paid["paid_verified"])
        repeated_paid = Service(self.store).submit("demo", fresh["quote_id"], "one-meal")
        self.assertEqual(paid["order_id"], repeated_paid["order_id"])
        self.assertEqual(self.writes(), {"order":1, "preorder":1, "consume":1})

    def test_same_price_other_store_date_or_pickup_method_cannot_use_grant(self):
        self.grant()
        changes = ({"store_code":"9000002"},
                   {"pickup_at":(datetime.fromisoformat(self.args.pickup_at)+timedelta(days=1)).isoformat()},
                   {"eat_type":"take-in-store"})
        for update in changes:
            with self.subTest(update=update):
                args = self.args.model_copy(update=update)
                if args.eat_type == "take-in-store":
                    cart = self.service.cart(args)
                    other = self.service.quote_pickup(args, cart["cart_ref"], "counter-choice")
                else:
                    other = self.service.quote(args)
                self.assertIn("quote_outside_authorized_purchase", other["checkout_blockers"])
                with self.assertRaisesRegex(EzMcdError, "quote_outside_authorized_purchase"):
                    self.service.submit("demo", other["quote_id"], "another-meal")
        self.assertEqual(self.writes(), {"pickup_quote":1})

    def test_each_product_promotion_price_or_body_change_stops_before_financial_requests(self):
        self.grant()
        changes = {"items":[], "discount_names":["different coupon"], "amount_cents":999,
                   "regular_cents":2200, "discount_cents":1200,
                   "body":{"different_customization":True}, "intent":"b"*64, "checkout_digest":"c"*64}
        for field,value in changes.items():
            with self.subTest(field=field), patch.object(DemoProvider, "preflight") as preflight, \
                    patch.object(DemoProvider, "create_order") as create:
                changed = deepcopy(self.raw); changed.update(quote_id=uuid.uuid4().hex)
                changed[field] = value
                self.store.save_quote(self.identity, changed)
                with self.assertRaisesRegex(EzMcdError, "quote_outside_authorized_purchase"):
                    self.service.submit("demo", changed["quote_id"], "one-meal")
                preflight.assert_not_called(); create.assert_not_called()
        self.assertEqual(self.writes(), {})

    def test_actual_cart_change_requires_new_review_even_below_budget(self):
        self.grant()
        _, provider = self.service.provider("demo")
        provider.write_cart({"quantity":2})
        fresh = self.service.quote(self.args)
        with self.assertRaisesRegex(EzMcdError, "quote_outside_authorized_purchase"):
            self.service.submit("demo", fresh["quote_id"], "changed-cart")
        self.assertEqual(self.writes(), {})

    def test_live_revalidation_checks_reviewed_content_even_when_protocol_digest_matches(self):
        self.grant()
        original = DemoProvider.quote
        def changed(provider, args):
            fresh = original(provider, args)
            fresh["discount_names"] = ["changed display offer"]
            return fresh
        with patch.object(DemoProvider, "quote", changed), patch.object(DemoProvider, "preflight") as preflight:
            with self.assertRaisesRegex(EzMcdError, "quote_outside_authorized_purchase"):
                self.service.submit("demo", self.q["quote_id"], "one-meal")
            preflight.assert_not_called()
        self.assertEqual(self.writes(), {})

    def test_scope_is_bound_to_member_even_when_quote_contents_are_equal(self):
        from ezmcd.demo import setup_demo
        self.grant()
        setup_demo(self.store, "other")
        other = self.store.account("other")
        other["policy"] = self.store.account("demo")["policy"]
        self.store.update_account(other)
        q = self.service.quote(self.args.model_copy(update={"account":"other"}))
        with self.assertRaisesRegex(EzMcdError, "quote_outside_authorized_purchase"):
            self.service.submit("other", q["quote_id"], "copied-policy")
        self.assertEqual(self.writes(), {})

    def test_expired_quote_wrong_store_budget_and_disabled_mode_do_not_change_policy(self):
        before = self.store.account("demo")
        for policy, code in (
            (Policy(), "quote_authorization_requires_enabled_policy"),
            (Policy(allow_checkout=True, max_order_cents=999), "order_outside_authorized_policy"),
            (Policy(allow_checkout=True, daily_budget_cents=999), "order_outside_authorized_policy"),
            (Policy(allow_checkout=True, allowed_stores=["9000002"]), "order_outside_authorized_policy")):
            with self.assertRaisesRegex(EzMcdError, code): self.grant(policy)
            self.assertEqual(self.store.account("demo"), before)
        with patch("ezmcd.service.time.time", return_value=self.q["expires_at"]):
            with self.assertRaisesRegex(EzMcdError, "quote_expired_get_new_quote"): self.grant()
        self.assertEqual(self.store.account("demo"), before)

    def test_expired_same_content_grant_never_silently_extends_itself(self):
        self.grant()
        now = datetime.fromisoformat(self.store.account("demo")["policy"]["expires_at"]).timestamp()
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return datetime.fromtimestamp(now, tz)
        before = self.store.account("demo")
        with patch("ezmcd.service.datetime", Clock), patch("ezmcd.service.time.time", return_value=now):
            fresh = self.service.quote(self.args)
            with self.assertRaisesRegex(EzMcdError, "checkout_policy_expired"):
                self.grant(quote_id=fresh["quote_id"])
            self.assertEqual(self.store.account("demo"), before)
            # Only the local owner's explicitly supplied future deadline renews it.
            explicit = Policy(allow_checkout=True,
                expires_at=(Clock.now(timezone.utc)+timedelta(minutes=5)).isoformat(timespec="minutes"))
            self.grant(explicit, fresh["quote_id"])
            self.assertEqual(self.store.account("demo")["policy"]["expires_at"], explicit.expires_at)
        self.assertEqual(self.writes(), {})

    def test_used_or_unknown_meal_cannot_receive_a_replacement_grant(self):
        self.grant()
        with patch.object(DemoProvider, "create_order", side_effect=EzMcdError("lost_reply")):
            first = self.service.submit("demo", self.q["quote_id"], "one-meal")
        self.assertEqual(first["state"], "UNKNOWN")
        before = self.store.account("demo")
        with self.assertRaisesRegex(EzMcdError, "existing_local_attempt_at_same_store_and_pickup_time"):
            self.grant()
        self.assertEqual(self.store.account("demo"), before)
        again = Service(self.store).submit("demo", self.q["quote_id"], "one-meal")
        self.assertEqual(again["attempt_id"], first["attempt_id"])
        self.assertEqual(self.writes(), {"order":1})

    def test_scope_is_rechecked_before_preorder_not_only_at_order_creation(self):
        self.grant()
        original = DemoProvider.payment_ready
        def changed(provider, attempt, quote):
            result = original(provider, attempt, quote)
            quote["discount_names"] = ["unapproved promotion"]
            return result
        with patch.object(DemoProvider, "payment_ready", changed), patch.object(DemoProvider, "preorder") as preorder:
            result = self.service.submit("demo", self.q["quote_id"], "one-meal")
        preorder.assert_not_called()
        self.assertEqual(result["stop_reason"], "quote_outside_authorized_purchase")
        self.assertEqual(self.writes(), {"order":1})

    def test_cli_scope_grant_uses_original_data_directory_and_disable_removes_scope(self):
        args = [sys.executable, "-m", "ezmcd.cli", "--data-dir", str(self.store.root),
                "account", "policy", "demo", "--enable-checkout", "--quote-id", self.q["quote_id"],
                "--max-order", "20.00", "--daily-budget", "20.00", "--max-orders-day", "1"]
        result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=10)
        self.assertTrue(json.loads(result.stdout)["one_purchase_content_only"])
        self.assertEqual(self.writes(), {})
        self.service.set_policy("demo", Policy())
        self.assertNotIn("quote_scope", self.store.account("demo")["policy"])


if __name__ == "__main__":
    unittest.main()
