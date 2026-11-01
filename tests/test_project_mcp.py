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
                self.assertTrue(next(t for t in tools if t.name == "pickup_methods").annotations.read_only_hint)
                self.assertIn("cart_add_configured", names)
                self.assertTrue(next(t for t in tools if t.name == "cart_add_status").annotations.read_only_hint)
                self.assertTrue(next(t for t in tools if t.name == "cart_clear").annotations.destructive_hint)
                self.assertIn("account_refresh", names)
                self.assertIn("account_runtime_check", names)
                history = next(t for t in tools if t.name == "orders_list")
                self.assertTrue(history.annotations.read_only_hint)
                self.assertFalse(history.annotations.open_world_hint)
                self.assertTrue({"coupons_list", "coupon_products"}.issubset(names))
                self.assertTrue(next(t for t in tools if t.name == "account_runtime_check").annotations.read_only_hint)
                self.assertTrue({"cities_list", "stores_search", "membership_assets", "preferences_get", "preferences_save", "meal_plan"}.issubset(names))
                refresh = next(t for t in tools if t.name == "account_refresh")
                self.assertFalse(refresh.annotations.read_only_hint)
                self.assertFalse(refresh.annotations.idempotent_hint)
                self.assertNotIn("set_policy", names)
                self.assertNotIn("import_session", names)
                self.assertNotIn("sampling_capture", names)
                self.assertNotIn("sampling_cert", names)
                payment = next(t for t in tools if t.name == "checkout_submit")
                self.assertTrue(payment.annotations.destructive_hint)
                caps = await client.call_tool("capabilities", {})
                self.assertFalse(caps.is_error)
                self.assertIn('"sms_login": true', caps.content[0].text)
                self.assertIn('"sms_login_via_mcp": false', caps.content[0].text)
                self.assertIn('"local_guarded_capture"', caps.content[0].text)
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
                viewed = json.loads((await client.call_tool("cart_get", {"request": request})).content[0].text)
                clear_args = {"request": request, "cart_ref": viewed["cart_ref"], "idempotency_key": "mcp-clear"}
                cleared = json.loads((await client.call_tool("cart_clear", clear_args)).content[0].text)
                self.assertTrue(cleared["cart_empty"])
                added = json.loads((await client.call_tool("cart_add_configured", {"request": request,
                    "items": [{"product_code": "100001", "quantity": 1}], "idempotency_key": "mcp-cart"})).content[0].text)
                self.assertFalse(added["order_created"])
                self.assertEqual(added["state"], "MATCHED")
                methods = json.loads((await client.call_tool("pickup_methods", {"request": request})).content[0].text)
                self.assertEqual(methods["selected_method"], "locker-in")
                self.assertTrue(methods["requested_method_selected"])
                self.assertFalse(methods["cart_changed"])
                recovered_cart = json.loads((await client.call_tool("cart_add_status", {"account": "demo"})).content[0].text)
                self.assertEqual(recovered_cart["operation_id"], added["operation_id"])
                self.assertTrue(recovered_cart["cart_matches_requested"])
                repeated_clear = json.loads((await client.call_tool("cart_clear", clear_args)).content[0].text)
                self.assertFalse(repeated_clear["cart_empty"])
                self.assertFalse(repeated_clear["clear_attempted"])
                result = await client.call_tool("checkout_quote", {"request": request})
                q = json.loads(result.content[0].text)
                self.assertTrue(q["simulation"])
                args = {"account": "demo", "quote_id": q["quote_id"], "idempotency_key": "mcp-breakfast"}
                first = json.loads((await client.call_tool("checkout_submit", args)).content[0].text)
                second = json.loads((await client.call_tool("checkout_submit", args)).content[0].text)
                self.assertTrue(first["paid_verified"])
                self.assertEqual(first["order_id"], second["order_id"])
                self.assertEqual(second["wallet_balance_cny"], "90.00")
            # A new MCP process can recover the attempt without its previous
            # conversation, quote ID, idempotency key or payment permission.
            from ezmcd.models import Policy
            Service(store).set_policy("demo", Policy())
            async with Client(params) as client:
                recovered_cart = json.loads((await client.call_tool("cart_add_status", {"account": "demo"})).content[0].text)
                self.assertEqual(recovered_cart["operation_id"], added["operation_id"])
                self.assertFalse(recovered_cart["add_attempted"])
                found = json.loads((await client.call_tool("orders_list", {
                    "account": "demo", "pickup_date": pickup.date().isoformat()})).content[0].text)
                self.assertEqual(found["total"], 1)
                self.assertFalse(found["live_status_checked"])
                recovered = json.loads((await client.call_tool("order_status", {
                    "account": "demo", "attempt_id": found["orders"][0]["attempt_id"]})).content[0].text)
                self.assertTrue(recovered["paid_verified"])
                self.assertEqual(recovered["order_id"], first["order_id"])
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM writes").fetchone()[0], 5)  # clear, add, three checkout stages

    async def test_stdio_expired_cart_retirement_and_next_day_simulated_purchase(self):
        from unittest.mock import patch
        from ezmcd.demo import DemoProvider
        from ezmcd.errors import EzMcdError
        from ezmcd.models import CheckoutInput, ItemSelection
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            setup_demo(store)
            service = Service(store)
            _, provider = service.provider("demo")
            provider.write_cart({"quantity": 0})
            past = (datetime.now(SHANGHAI) - timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
            request = CheckoutInput(account="demo", store_code="9000001", pickup_at=past.isoformat(), max_amount_cny="20")
            with patch("ezmcd.demo.datetime", wraps=datetime) as clock, \
                    patch.object(DemoProvider, "write_cart", side_effect=EzMcdError("lost_response")):
                clock.now.return_value = past - timedelta(hours=1)
                original = service.add_simple_items(request, [ItemSelection(product_code="100001")], "old-meal")
            params = StdioServerParameters(command=sys.executable, args=["-m", "ezmcd.mcp_server"],
                                           env={"EZMCD_DATA_DIR": str(store.root)})
            async with Client(params) as client:
                tool_map = {t.name: t for t in (await client.list_tools()).tools}
                self.assertEqual(len(tool_map), 35)
                self.assertTrue(tool_map["cart_operations_list"].annotations.read_only_hint)
                self.assertFalse(tool_map["cart_operations_list"].annotations.open_world_hint)
                self.assertTrue(tool_map["cart_operation_retire"].annotations.destructive_hint)
                self.assertFalse(tool_map["cart_operation_retire"].annotations.open_world_hint)
                listed = json.loads((await client.call_tool("cart_operations_list", {"account": "demo"})).content[0].text)
                self.assertTrue(listed["operations"][0]["retirement_available"])
                retired = json.loads((await client.call_tool("cart_operation_retire", {
                    "account": "demo", "operation_id": original["operation_id"]})).content[0].text)
                self.assertTrue(retired["retired"])
                self.assertFalse(retired["official_cart_changed"])
            async with Client(params) as client:
                repeated = json.loads((await client.call_tool("cart_add_simple", {"request": request.model_dump(),
                    "items": [{"product_code": "100001"}], "idempotency_key": "old-meal"})).content[0].text)
                self.assertEqual(repeated["state"], "RETIRED")
                self.assertFalse(repeated["add_attempted"])
                current = request.model_dump() | {"pickup_at": (past + timedelta(days=2)).isoformat()}
                viewed = json.loads((await client.call_tool("cart_get", {"request": current})).content[0].text)
                self.assertEqual(viewed["items"], [])
                added = json.loads((await client.call_tool("cart_add_simple", {"request": current,
                    "items": [{"product_code": "100001"}], "idempotency_key": "new-meal"})).content[0].text)
                self.assertEqual(added["state"], "MATCHED")
                quote = json.loads((await client.call_tool("checkout_quote", {"request": current})).content[0].text)
                paid = json.loads((await client.call_tool("checkout_submit", {"account": "demo",
                    "quote_id": quote["quote_id"], "idempotency_key": "new-purchase"})).content[0].text)
                self.assertTrue(paid["paid_verified"])
                self.assertTrue(paid["simulation"])
            with store.connect() as db:
                self.assertEqual(dict(db.execute("SELECT stage,count(*) FROM writes GROUP BY stage")),
                                 {"cart": 2, "order": 1, "preorder": 1, "consume": 1})

    async def test_account_errors_do_not_expose_secrets_or_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            async with Client(build_server(Service(Store(Path(temporary))))) as client:
                result = await client.call_tool("account_check", {"account": "../../secret"})
                text = result.content[0].text
                self.assertIn("invalid_account_alias", text)
                self.assertNotIn(temporary, text)

    async def test_counter_quote_survives_stdio_restart_and_can_pay_in_simulation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)); setup_demo(store)
            params = StdioServerParameters(command=sys.executable, args=["-m", "ezmcd.mcp_server"], env={"EZMCD_DATA_DIR": folder})
            when = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
            request = {"account": "demo", "store_code": "9000001", "pickup_at": when.isoformat(),
                       "max_amount_cny": "20", "eat_type": "take-in-store"}
            async with Client(params) as client:
                definitions = {t.name: t for t in (await client.list_tools()).tools}
                self.assertFalse(definitions["checkout_quote_pickup"].annotations.read_only_hint)
                self.assertTrue(definitions["checkout_quote_pickup"].annotations.destructive_hint)
                self.assertTrue(definitions["checkout_quote_pickup_status"].annotations.read_only_hint)
                cart = json.loads((await client.call_tool("cart_get", {"request": request})).content[0].text)
                arguments = {"request": request, "cart_ref": cart["cart_ref"], "idempotency_key": "counter-quote"}
                first = json.loads((await client.call_tool("checkout_quote_pickup", arguments)).content[0].text)
                self.assertEqual(first["state"], "READY")
            async with Client(params) as client:
                recovered = json.loads((await client.call_tool("checkout_quote_pickup_status", {"account": "demo"})).content[0].text)
                repeated = json.loads((await client.call_tool("checkout_quote_pickup", arguments)).content[0].text)
                self.assertEqual({first["quote_id"], recovered["quote_id"], repeated["quote_id"]}, {first["quote_id"]})
                self.assertFalse(repeated["switch_attempted"])
                paid = json.loads((await client.call_tool("checkout_submit", {
                    "account": "demo", "quote_id": recovered["quote_id"], "idempotency_key": "counter-buy"})).content[0].text)
                self.assertTrue(paid["paid_verified"])
                self.assertTrue(paid["simulation"])
