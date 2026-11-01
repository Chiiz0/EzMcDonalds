from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from ezmcd.demo import setup_demo
from ezmcd.mcp_server import build_server
from ezmcd.models import SHANGHAI
from ezmcd.service import Service
from ezmcd.storage import Store


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_initialize_schema_quote_pay_and_repeated_submit(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            setup_demo(store)
            params = StdioServerParameters(command=sys.executable, args=["-m", "ezmcd.mcp_server"],
                                           env={"EZMCD_DATA_DIR": str(store.root)})
            async with Client(params) as client:
                tools = (await client.list_tools()).tools
                names = {tool.name for tool in tools}
                self.assertIn("checkout_submit", names)
                self.assertIn("product_detail", names)
                self.assertIn("cart_add_configured", names)
                self.assertIn("account_refresh", names)
                self.assertIn("account_runtime_check", names)
                self.assertTrue({"coupons_list", "coupon_products"}.issubset(names))
                self.assertTrue(next(t for t in tools if t.name == "account_runtime_check").annotations.read_only_hint)
                self.assertTrue({"cities_list", "stores_search", "membership_assets", "preferences_get", "preferences_save", "meal_plan"}.issubset(names))
                refresh = next(t for t in tools if t.name == "account_refresh")
                self.assertFalse(refresh.annotations.read_only_hint)
                self.assertFalse(refresh.annotations.idempotent_hint)
                self.assertNotIn("set_policy", names)
                self.assertNotIn("import_session", names)
                payment = next(t for t in tools if t.name == "checkout_submit")
                self.assertTrue(payment.annotations.destructive_hint)
                caps = await client.call_tool("capabilities", {})
                self.assertFalse(caps.is_error)
                self.assertIn('"sms_login": true', caps.content[0].text)
                self.assertIn('"sms_login_via_mcp": false', caps.content[0].text)
                self.assertNotIn("sms_send", names)
                check = json.loads((await client.call_tool("account_check", {"account": "demo"})).content[0].text)
                self.assertTrue(check["readiness"]["can_browse"])
                refresh_result = await client.call_tool("account_refresh", {"account": "demo"})
                self.assertIn("session_refresh_requires_real_account", refresh_result.content[0].text)
                runtime_result = await client.call_tool("account_runtime_check", {"account": "demo"})
                self.assertIn("runtime_check_requires_active_real_account", runtime_result.content[0].text)
                instructions = await client.call_tool("account_connection_instructions", {})
                self.assertIn("--device-from", instructions.content[0].text)
                pickup = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
                found = json.loads((await client.call_tool("stores_search", {"request": {
                    "account": "demo", "city": "演示城市", "keyword": "门店"}})).content[0].text)
                self.assertTrue(found["simulation"])
                self.assertEqual(len(found["stores"]), 2)
                await client.call_tool("preferences_save", {"preferences": {"account": "demo", "store_code": "9000001",
                    "items": [{"name": "演示汉堡"}], "pickup_time": "10:15", "max_amount_cny": "20.00"}})
                plan = json.loads((await client.call_tool("meal_plan", {"date": pickup.date().isoformat()})).content[0].text)
                self.assertTrue(plan["simulation"])
                self.assertTrue(plan["selection_ready"])
                self.assertFalse(plan["cart_changed"])
                request = {"account": "demo", "store_code": "9000001", "pickup_at": pickup.isoformat(), "max_amount_cny": "20.00"}
                coupons = json.loads((await client.call_tool("coupons_list", {"request": request})).content[0].text)
                self.assertTrue(coupons["simulation"])
                self.assertEqual(coupons["coupons"], [])
                detail = json.loads((await client.call_tool("product_detail", {"request": request, "product_code": "100001"})).content[0].text)
                self.assertTrue(detail["simulation"])
                added = json.loads((await client.call_tool("cart_add_configured", {"request": request,
                    "items": [{"product_code": "100001", "quantity": 1}], "idempotency_key": "mcp-cart"})).content[0].text)
                self.assertFalse(added["order_created"])
                result = await client.call_tool("checkout_quote", {"request": request})
                q = json.loads(result.content[0].text)
                self.assertTrue(q["simulation"])
                args = {"account": "demo", "quote_id": q["quote_id"], "idempotency_key": "mcp-breakfast"}
                first = json.loads((await client.call_tool("checkout_submit", args)).content[0].text)
                second = json.loads((await client.call_tool("checkout_submit", args)).content[0].text)
                self.assertTrue(first["paid_verified"])
                self.assertEqual(first["order_id"], second["order_id"])
                self.assertEqual(second["wallet_balance_cny"], "90.00")

    async def test_account_errors_do_not_expose_secrets_or_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            async with Client(build_server(Service(Store(Path(temporary))))) as client:
                result = await client.call_tool("account_check", {"account": "../../secret"})
                text = result.content[0].text
                self.assertIn("invalid_account_alias", text)
                self.assertNotIn(temporary, text)
