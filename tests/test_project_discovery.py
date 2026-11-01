from datetime import datetime
import json
import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from ezmcd.catalog import membership_assets, public_menu, menu_categories
from ezmcd.discovery import cities, resolve_city, restaurant, store_results, pickup_choices
from ezmcd.errors import EzMcdError
from ezmcd.models import StoreSearch, SHANGHAI
from ezmcd.provider import McdProvider


class DiscoveryTests(unittest.TestCase):
    def test_search_requires_one_explicit_location_and_bounded_finite_inputs(self):
        for args in ({}, {"latitude": 30.0}, {"city": "杭州", "latitude": 30.0, "longitude": 120.0},
                     {"latitude": float("nan"), "longitude": 120.0}, {"city": "杭州", "limit": 1000},
                     {"city": "杭州", "page": 0}, {"city": " "}, {"city": "杭州\n"}):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                StoreSearch(account="test", **args)
        self.assertEqual(StoreSearch(account="test", city=" 杭州 ").city, "杭州")

    def test_city_ambiguity_requires_selection_instead_of_guessing(self):
        raw = {"groups": [{"cities": [{"code": "900001", "name": "示例市"}, {"code": "900002", "name": "示例县"}]}]}
        self.assertEqual(len(cities(raw, "示例")), 2)
        self.assertEqual(resolve_city(raw, "示例市"), "900001")
        with self.assertRaisesRegex(EzMcdError, "not_unique"):
            resolve_city(raw, "示例")

    def test_city_search_uses_pickup_parameters_and_keeps_pagination(self):
        provider = McdProvider({}, transport=Mock())
        provider.get = Mock(return_value={"stores": [{"code": "9000001", "name": "示例店"}], "totalSize": 15})
        request = StoreSearch(account="test", city="900001", keyword="示例", page=2, limit=10)
        result = provider.search_stores(request)
        self.assertFalse(result["has_more"])
        provider.get.assert_called_once_with("/bff/store/stores", {"pageNo": "2", "pageSize": "10",
            "cityCode": "900001", "keyword": "示例", "hotTagCode": "", "beType": "1", "dayPartCodes": ""})
        provider.http.send.assert_not_called()

    def test_nearby_filter_and_pagination_are_explicitly_limited_to_server_results(self):
        raw = {"stores": [{"code": "9000001", "name": "测试甲店"}, {"code": "9000002", "name": "测试乙店"},
                          {"code": "9000003", "name": "其他店"}]}
        request = StoreSearch(account="test", latitude=30.0, longitude=120.0, keyword="测试", limit=1, page=2)
        result = store_results(raw, request, nearby=True)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["stores"][0]["store_code"], "9000002")
        self.assertEqual(result["scope"], "server_nearby_results")
        self.assertFalse(result["has_more"])

    def test_store_view_does_not_infer_locker_support_or_expose_jump_urls(self):
        result = restaurant({"code": "9000001", "name": "测试", "isPin": 1, "isReservation": 1,
                             "onlineBusinessStatus": True, "jumpUrl": "https://example.invalid/?token=secret"})
        self.assertTrue(result["can_order_now"])
        self.assertTrue(result["pickup_slots_must_be_checked"])
        self.assertNotIn("locker", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_pickup_times_use_shanghai_and_keep_interval_anchor(self):
        raw = {"timeInterval": 5, "reservationOptions": [{"date": "2027-01-01", "timeOptions": [
            {"timeStart": "10:03", "timeEnd": "10:18", "dayPartCode": 1, "dayPartName": "早餐"}]}]}
        result = pickup_choices(raw, "9000001", now=datetime(2027, 1, 1, 10, 7, tzinfo=SHANGHAI))
        self.assertEqual(result["dates"][0]["periods"][0]["pickup_times"],
                         ["2027-01-01T10:08:00+08:00", "2027-01-01T10:13:00+08:00", "2027-01-01T10:18:00+08:00"])
        raw["timeInterval"] = 0
        with self.assertRaisesRegex(EzMcdError, "schema_changed"):
            pickup_choices(raw, "9000001")

    def test_overnight_periods_are_not_silently_assigned_to_wrong_date(self):
        raw = {"timeInterval": 5, "reservationOptions": [{"date": "2027-01-01", "timeOptions": [
            {"timeStart": "23:00", "timeEnd": "01:00", "dayPartCode": 1}]}]}
        with self.assertRaisesRegex(EzMcdError, "schema_changed"):
            pickup_choices(raw, "9000001")

    def test_card_summary_excludes_card_numbers_ids_and_jump_links(self):
        result = membership_assets({"cards": [{"title": "示例早餐卡", "cardNo": "private-number", "cardId": "private-card",
            "clickCardJumpUrl": "private-url", "validPeriodText": "有效期说明"}],
            "couponCategories": [{"name": "到店券", "count": 2, "jumpUrl": "private-url"}]})
        self.assertEqual(result["cards"][0]["name"], "示例早餐卡")
        self.assertNotIn("private-", json.dumps(result))
        self.assertTrue(result["applicability_requires_live_menu_and_quote"])

    def test_menu_exposes_categories_and_only_supported_image_origins(self):
        raw = {"menu": [{"categoryCode": "c", "categoryName": "早餐", "productList": [
            {"productCode": "100", "productName": "汉堡", "productImage": "javascript:bad", "productType": 1}],
            "categories": [{"categoryCode": "d", "categoryName": "饮品", "productList": []}]}]}
        self.assertIsNone(public_menu(raw)[0]["image_url"])
        self.assertEqual(menu_categories(raw)[1]["names"], ["早餐", "饮品"])


if __name__ == "__main__":
    unittest.main()
