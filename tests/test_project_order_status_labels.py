import unittest
from unittest.mock import patch

from ezmcd.demo import DemoProvider
from ezmcd.errors import EzMcdError
from ezmcd.order_views import public_order, status_view
from ezmcd.service import Service
import test_project_official_orders as official
import test_project_service as checkout


class OrderStatusLabelTests(unittest.TestCase):
    def test_completed_detail_uses_explicit_state_instead_of_pickup_headline(self):
        raw = official.order() | {"orderStatus": "已完成", "orderStatusTitle": "12345",
                                "orderStatusTitleType": 1, "pickupCode": "12345",
                                "orderStatusSubTitle": "请至人工窗口取餐"}
        result = public_order(raw, detail=True)
        self.assertEqual(result["status_title"], "已完成")
        self.assertEqual(result["status_source"], "orderStatus")
        self.assertEqual(result["official_order_state"], "COMPLETED")
        self.assertEqual(result["pickup_code"], "12345")
        self.assertEqual(result["status_description"], "请至人工窗口取餐")
        self.assertFalse(result["collection_verified"])

    def test_explicit_state_precedes_headline_even_when_headline_is_a_known_label(self):
        for headline in ("预约完成", "已取消", "已完成"):
            self.assertEqual(status_view({"orderStatus": "制作中", "orderStatusTitle": headline}), {
                "status_title": "制作中", "status_source": "orderStatus", "official_order_state": "UNKNOWN",
                "status_description": None, "collection_verified": False})

    def test_missing_state_uses_only_observed_textual_legacy_headlines(self):
        for title in ("待支付", "预约完成", "已取消", "已完成"):
            result = status_view({"orderStatusTitle": title})
            self.assertEqual(result["status_title"], title)
            self.assertEqual(result["status_source"], "orderStatusTitle")
        for title in ("12345", "A1001", "an unknown headline", "", None):
            result = status_view({"orderStatusTitle": title, "orderStatusCode": "6", "orderStatusTitleType": 1})
            self.assertIsNone(result["status_title"])
            self.assertIsNone(result["status_source"])
            self.assertEqual(result["official_order_state"], "UNKNOWN")

    def test_malformed_explicit_state_cannot_fall_back_to_a_safe_looking_headline(self):
        for value in (True, 6, {}, [], "bad\nstatus", "x" * 201):
            with self.subTest(value=value), self.assertRaisesRegex(EzMcdError, "response_schema_changed"):
                public_order(official.order() | {"orderStatus": value})

    def test_cancelled_and_completed_are_different_from_payment_verification(self):
        self.assertEqual(status_view({"orderStatus": "已取消", "orderStatusTitle": "12345"})["official_order_state"], "CANCELLED")
        result = public_order(official.order() | {"orderStatus": "已完成", "paymentChannel": "WX"}, detail=True)
        self.assertEqual(result["official_order_state"], "COMPLETED")
        self.assertNotIn("paid_verified", result)


class OrderFulfillmentStatusTests(unittest.TestCase):
    setUp = checkout.ProjectCheckoutTests.setUp

    def paid(self):
        quote = self.service.quote(self.args)
        return quote, self.service.submit("demo", quote["quote_id"], "one-meal")

    def writes(self):
        with self.store.connect() as db:
            return [tuple(r) for r in db.execute("SELECT * FROM writes ORDER BY stage")]

    @staticmethod
    def status_with(label, headline="12345", pickup_code=""):
        original = DemoProvider.status
        def status(provider, attempt):
            result = original(provider, attempt)
            result["order"].update(orderStatus=label, orderStatusTitle=headline,
                                   orderStatusTitleType=1, pickupCode=pickup_code,
                                   orderStatusSubTitle="请核对官方取餐说明")
            return result
        return status

    def test_completed_paid_order_no_longer_requests_collection_or_missing_code(self):
        quote, paid = self.paid(); before = self.writes()
        with patch.object(DemoProvider, "status", self.status_with("已完成")):
            result = self.service.status("demo", paid["attempt_id"])
            restarted = Service(self.store)
            repeated = restarted.submit("demo", quote["quote_id"], "one-meal")
        self.assertEqual(result["state"], "PAID")
        self.assertTrue(result["paid_verified"])
        self.assertEqual(result["official_status"], "已完成")
        self.assertEqual(result["official_order_state"], "COMPLETED")
        self.assertEqual(result["next_action"], "review_completed_order")
        self.assertFalse(result["pickup_code_pending"])
        self.assertFalse(result["collection_verified"])
        self.assertEqual(result["official_status_detail"], "请核对官方取餐说明")
        self.assertEqual(repeated["attempt_id"], paid["attempt_id"])
        self.assertEqual(repeated["next_action"], "review_completed_order")
        self.assertEqual(self.writes(), before)
        local = restarted.orders("demo")["orders"][0]
        self.assertEqual(local["official_order_state"], "COMPLETED")

    def test_completed_label_does_not_establish_missing_wallet_payment_proof(self):
        quote, paid = self.paid(); identity = self.store.account("demo")["identity"]
        attempt = self.store.attempt(identity, paid["attempt_id"])
        attempt.update(state="UNKNOWN", paid_verified=False)
        self.store.update_attempt(identity, attempt)
        before = self.writes()
        with patch.object(DemoProvider, "status", self.status_with("已完成")):
            result = self.service.status("demo", paid["attempt_id"])
            repeated = self.service.submit("demo", quote["quote_id"], "one-meal")
        self.assertFalse(result["paid_verified"])
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["official_order_state"], "COMPLETED")
        self.assertEqual(result["next_action"], "query_order_status")
        self.assertFalse(repeated["paid_verified"])
        self.assertEqual(self.writes(), before)

    def test_explicit_cancelled_state_overrides_historical_paid_receipt(self):
        _, paid = self.paid(); before = self.writes()
        with patch.object(DemoProvider, "status", self.status_with("已取消", "预约完成")):
            result = self.service.status("demo", paid["attempt_id"])
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(result["official_order_state"], "CANCELLED")
        self.assertFalse(result["paid_verified"])
        self.assertEqual(result["next_action"], "review_cancelled_order")
        self.assertEqual(self.writes(), before)

    def test_an_unknown_numeric_headline_never_claims_completion(self):
        _, paid = self.paid()
        with patch.object(DemoProvider, "status", self.status_with(None)):
            result = self.service.status("demo", paid["attempt_id"])
        self.assertIsNone(result["official_status"])
        self.assertEqual(result["official_order_state"], "UNKNOWN")

    def test_failed_query_after_completion_retains_evidence_but_requests_recheck(self):
        _, paid = self.paid()
        with patch.object(DemoProvider, "status", self.status_with("已完成")):
            self.service.status("demo", paid["attempt_id"])
        with patch.object(DemoProvider, "status", side_effect=EzMcdError("offline")):
            result = self.service.status("demo", paid["attempt_id"])
        self.assertTrue(result["status_query_failed"])
        self.assertEqual(result["official_order_state"], "COMPLETED")
        self.assertEqual(result["next_action"], "query_order_status")

    def test_mismatched_order_cannot_update_fulfillment_evidence(self):
        _, paid = self.paid(); changed = self.status_with("已完成")
        def mismatch(provider, attempt):
            result = changed(provider, attempt)
            result["order"]["orderId"] = "another-order"
            return result
        with patch.object(DemoProvider, "status", mismatch):
            result = self.service.status("demo", paid["attempt_id"])
        self.assertTrue(result["status_query_failed"])
        self.assertEqual(result["official_order_state"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
