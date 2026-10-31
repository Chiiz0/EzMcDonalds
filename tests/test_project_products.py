from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from ezmcd.errors import EzMcdError
from ezmcd.models import CheckoutInput, ItemSelection, SHANGHAI
from ezmcd.products import configured_product, public_detail
from ezmcd.provider import McdProvider
from ezmcd.transport import Transport


def fixture():
    offer = {"productCode": "900001", "productType": 7, "offer_id": "synthetic-offer", "cardName": "示例卡",
             "cardId": "private-card", "cardType": 1, "couponId": "private-coupon", "couponCode": "test-coupon", "promotionId": "test-promotion"}
    detail = {"maxPurchaseQuantity": 10, "product": {"code": "900001", "type": 7, "name": "示例组合", "comboItems": [
        {"round": 1, "isChoices": 0, "choicesCode": "", "name": "主食", "minQuantity": 1, "maxQuantity": 1,
         "comboProducts": [{"code": "100001", "name": "汉堡", "unsellable": False, "diffPrice": 0}]},
        {"round": 2, "isChoices": 1, "choicesCode": "900002", "name": "饮品", "minQuantity": 1, "maxQuantity": 1,
         "comboProducts": [{"code": "200001", "name": "咖啡", "unsellable": False, "diffPrice": 0},
                           {"code": "200002", "name": "豆浆", "unsellable": False, "diffPrice": 200},
                           {"code": "200003", "name": "售罄饮品", "unsellable": True, "diffPrice": 0}]}]}}
    selection = ItemSelection(product_code="900001", offer_id="synthetic-offer", combo=[
        {"round": 1, "items": [{"product_code": "100001"}]}, {"round": 2, "items": [{"product_code": "200002"}]}])
    return offer, detail, selection


class ProductTests(unittest.TestCase):
    def test_live_offer_identifiers_and_explicit_component_choices(self):
        offer, detail, selection = fixture()
        product = configured_product(selection, offer, detail, 1)
        self.assertEqual(product["comboItems"][1], {"round": 2, "isChoices": 1, "choicesCode": "900002",
            "comboProducts": [{"code": "200002", "name": "豆浆", "quantity": 1, "customization": {"options": [], "items": []}}]})
        self.assertEqual(product["couponId"], "private-coupon")
        public = public_detail(detail, offer)
        self.assertNotIn("private-", json.dumps(public))
        self.assertEqual(public["groups"][1]["choices"][1]["extra_cny"], "2.00")

    def test_missing_round_wrong_round_duplicates_and_excess_quantities_stop(self):
        offer, detail, selection = fixture()
        variants = []
        p = selection.model_dump(); p["combo"].pop(); variants.append(p)
        p = selection.model_dump(); p["combo"][1]["items"][0]["product_code"] = "100001"; variants.append(p)
        p = selection.model_dump(); p["combo"].append(p["combo"][0]); variants.append(p)
        p = selection.model_dump(); p["combo"][1]["items"][0]["quantity"] = 2; variants.append(p)
        p = selection.model_dump(); p["combo"][1]["items"][0]["product_code"] = "200003"; variants.append(p)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(EzMcdError):
                configured_product(ItemSelection.model_validate(value), offer, detail, 1)

    def test_unsupported_customization_is_visible_and_cannot_be_silently_dropped(self):
        offer, detail, selection = fixture()
        selected = detail["product"]["comboItems"][1]["comboProducts"][1]
        selected["hasCustomization"] = 1
        selected["modification"] = {"items": [{"minValues": 1, "maxValues": 1, "values": []}]}
        self.assertFalse(public_detail(detail, offer)["groups"][1]["choices"][1]["standard_configuration_supported"])
        with self.assertRaisesRegex(EzMcdError, "advanced_customization"):
            configured_product(selection, offer, detail, 1)

    def test_known_default_condiment_encoding(self):
        offer, detail, selection = fixture()
        detail["product"]["comboItems"][0]["comboProducts"][0]["modification"] = {"items": [
            {"minValues": 0, "maxValues": 1, "values": [{"code": "111", "name": "酱", "displayMode": 2,
                "selectedKey": "0-1", "selectedQuantity": 1, "price": 0, "soldOut": False}]}]}
        product = configured_product(selection, offer, detail, 1)
        self.assertEqual(product["comboItems"][0]["comboProducts"][0]["customization"], {"options": [
            {"code": "111", "name": "酱", "key": "0-1", "checked": 1, "price": 0,
             "quantity": 1, "checkedCode": "1", "displayText": ""}], "items": []})

    def test_draft_does_not_touch_nonempty_cart_or_send_writes(self):
        provider = McdProvider({}, transport=Mock())
        when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
        args = CheckoutInput(account="test", store_code="9000001", pickup_at=when.isoformat(), max_amount_cny="20")
        with patch.object(provider, "context", return_value=("1", {})), patch.object(provider, "get", return_value={
                "storeCode": "9000001", "beType": 1, "orderType": 1, "products": [{"code": "existing"}]}):
            with self.assertRaisesRegex(EzMcdError, "cart_not_empty"):
                provider.configured_cart_draft(args, [fixture()[2]])
        provider.http.send.assert_not_called()

    def test_read_only_post_route_is_narrowly_allowlisted(self):
        transport = Transport({})
        transport.signer = Mock()
        transport.signer.headers.return_value = {}
        with patch.object(transport, "_open", return_value={}) as send:
            transport.send("POST", "/bff/spc/products/detail/123", body={})
            self.assertEqual(send.call_count, 1)
            for path in ("/bff/spc/products/detail/../orders", "/bff/spc/products/detail/123/pay"):
                with self.assertRaises(EzMcdError):
                    transport.send("POST", path, body={})
            self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
