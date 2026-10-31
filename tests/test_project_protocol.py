from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, Mock

from ezmcd.catalog import checkout, menu_products, public_menu
from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput
from ezmcd.protocol.signing import sign_v4, constants, Signer
from ezmcd.protocol.v5 import decode_verified_sample, encode
from ezmcd.transport import Transport
from ezmcd.provider import McdProvider

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path("/Applications/McdApp.app/WrappedBundle/McdApp")


class ProjectProtocolTests(unittest.TestCase):
    def test_nested_menus_and_distinct_offers_are_preserved_without_coupon_credentials(self):
        source = {"menu": [{"categoryName": "parent", "categories": [{"categoryName": "child", "productList": [
            {"productCode": "123", "productName": "coffee", "cardId": "private-card", "couponId": "private-coupon", "priceInfo": {"eatInPrice": 600}},
            {"productCode": "123", "productName": "coffee", "priceInfo": {"eatInPrice": 1000}}]}]}]}
        products = public_menu(source)
        self.assertEqual(len(products), 2)
        self.assertNotEqual(products[0]["offer_id"], products[1]["offer_id"])
        self.assertNotIn("private-card", json.dumps(products))
        self.assertNotIn("private-coupon", json.dumps(products))

    def test_transport_cannot_be_repurposed_for_unlisted_requests(self):
        transport = Transport({})
        with patch("urllib.request.build_opener") as opener:
            for method, path in [("GET", "https://other.example"), ("POST", "/bff/member/mwallet/freePayment"),
                                 ("DELETE", "/bff/order/orders/1"), ("POST", "/bff/member/mwallet/recharge")]:
                with self.assertRaises(EzMcdError):
                    transport.send(method, path)
            opener.assert_not_called()

    def test_runtime_missing_stale_or_future_stops(self):
        for runtime in (None, {"observed_at": 0}, {"observed_at": 4102444800}):
            with self.assertRaises(EzMcdError):
                Signer({"runtime": runtime}).runtime()

    def test_wallet_change_or_nonpaying_cashier_stops_before_preorder(self):
        provider = McdProvider({}, transport=Mock())
        attempt = {"order_id": "o", "pay_id": "p", "wallet_before_cents": 10000}
        quote = {"amount_cents": 1000}
        state = {"order": {"orderStatusTitle": "待支付", "leftPaySecond": 500, "realPayAmount": ""},
                 "cashier": {"payStatus": "PAYING"}, "wallet_cents": 9000}
        with patch.object(provider, "validate_order"), patch.object(provider, "status", return_value=state):
            with self.assertRaisesRegex(EzMcdError, "wallet_changed"):
                provider.payment_ready(attempt, quote)
            state["wallet_cents"] = 10000
            state["cashier"]["payStatus"] = "CLOSED"
            with self.assertRaisesRegex(EzMcdError, "order_not_payable"):
                provider.payment_ready(attempt, quote)
        provider.http.send.assert_not_called()

    def test_v4_matches_previously_verified_implementation_with_synthetic_keys(self):
        # Fixed synthetic vectors cross-checked against the original implementation.
        signatures = {"GET": "f0QciLQ+jMxJW9GBjeJRH1zZopwJTwKnDfW4Di0cBYk=",
            "POST": "oRxwClwvKFURndFxzKwiBKqAjgRhWq48NN6iOivJBcg=",
            "PUT": "q6VQYgNpjKVU6+pnuYnDU/xk/v2eS6ma2R1zXsr8sVs="}
        headers = {"sid": "synthetic-session", "token": "synthetic-device", "sv": "v4", "v": "test", "ct": "101"}
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for method in ("GET", "POST", "PUT"):
            actual = sign_v4(headers, "test-ak", "test-secret", moment, method, "/test", "a=1", b'{}')
            self.assertEqual(actual["authorization"], "hmac-auth-v1#test-ak#" + signatures[method] +
                "#hmac-sha256#Thu, 01 Jan 2026 00:00:00 GMT#ct;sid;sv;token;v")
            self.assertEqual(actual["x-hmac-digest"], "L1kEC2O3IAWYREI52iyaTzq8XENCWbIxJtclFNq4zQk=")

    @unittest.skipUnless(BINARY.exists() and (ROOT / ".private/checkout-capture-order/01.json").exists(), "Local authorized sample unavailable")
    def test_extracted_v5_matches_real_sample_without_printing_secrets(self):
        sample = json.loads((ROOT / ".private/checkout-capture-order/01.json").read_text())["request"]
        digest = constants(BINARY)[2]
        runtime, inputs = decode_verified_sample(sample["path"], sample["headers"]["x-mcd-sign"], digest)
        self.assertTrue(encode(sample["path"], runtime, constants=digest, **inputs) == sample["headers"]["x-mcd-sign"])

    @unittest.skipUnless((ROOT / ".private").exists(), "Local quote evidence unavailable")
    def test_generic_checkout_matches_real_breakfast_order_body(self):
        evidence = {}
        for path in sorted((ROOT / ".private").glob("wallet-probe-*.json"), reverse=True):
            record = json.loads(path.read_text())
            route, response = record.get("request_path"), record.get("response", {})
            if route not in ("/bff/cart/carts", "/bff/order/confirmation/info", "/bff/spc/menu") or route in evidence:
                continue
            data = response.get("data")
            if not isinstance(data, dict) or (route.endswith("carts") and not data.get("products")):
                continue
            evidence[route] = data
            if len(evidence) == 3:
                break
        if len(evidence) != 3:
            self.skipTest("Local quote evidence unavailable")
        cart, conf = evidence["/bff/cart/carts"], evidence["/bff/order/confirmation/info"]
        args = CheckoutInput(account="test", store_code=str(cart["storeCode"]),
            pickup_at=conf["storePickUpInfo"]["pickupExpectTimeValue"].replace(" ", "T") + ":00+08:00", max_amount_cny="20.00")
        q = checkout(args, cart, conf, str(cart["daypartCode"]))
        self.assertEqual(q["amount_cents"], 1260)
        self.assertEqual(q["discount_cents"], 840)
        # The comparison intentionally checks a boolean to avoid printing private order fields.
        sys.path.insert(0, str(ROOT / "scripts"))
        from checkout_protocol import build_order_body
        preferences = json.loads((ROOT / ".private/breakfast-preferences.json").read_text())
        expected = build_order_body(preferences, evidence["/bff/spc/menu"], cart, conf,
            datetime.fromisoformat(args.pickup_at), str(cart["daypartCode"]))
        self.assertTrue(q["body"] == expected)
        # Generic validation must still stop if exact components differ.
        changed = deepcopy(conf)
        changed["productPriceInfo"]["cartProductList"][0]["comboItemList"][1]["productCode"] = "999999"
        with self.assertRaises(EzMcdError):
            checkout(args, cart, changed, str(cart["daypartCode"]))
