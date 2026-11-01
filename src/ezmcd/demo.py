"""Clearly labelled, persistent offline demo. Never creates a real HTTP client."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import uuid

from .catalog import checkout
from .errors import EzMcdError
from .models import Policy, SHANGHAI, fingerprint, pickup_time, yuan
from .discovery import pickup_choices, restaurant
from .provider import McdProvider
from .storage import read_private, write_private


def setup_demo(store, alias="demo"):
    identity = fingerprint({"provider": "demo", "alias": alias})
    store.add_account({"alias": alias, "identity": identity, "provider": "demo", "runtime": None,
        "policy": Policy(allow_checkout=True, max_order_cents=2000, daily_budget_cents=4000, max_orders_per_day=2).model_dump()})
    return {"account": alias, "simulation": True, "store_code": "9000001",
            "message": "Offline sample cart and simulated wallet only; no real purchase."}


class DemoProvider:
    validate_order = staticmethod(McdProvider.validate_order)

    def __init__(self, account, store):
        self.account = account
        self.path = store.root / "demo" / (account["identity"] + ".json")

    def read(self):
        if self.path.exists():
            return read_private(self.path)
        return {"balance": 10000, "orders": {}, "cart_quantity": 1}

    def wallet(self):
        return {"balance_cents": self.read()["balance"], "simulation": True}

    def favorites(self):
        return {"list": [self.store("9000001"), self.store("9000002")]}

    def assets(self):
        return {"simulation": True, "cards": [], "coupon_categories": [],
                "applicability_requires_live_menu_and_quote": True}

    def cities(self, keyword=""):
        rows = [{"city_code": "900000", "name": "演示城市"}]
        return {"simulation": True, "cities": [r for r in rows if keyword in r["name"] or keyword == r["city_code"]]}

    def search_stores(self, request):
        rows = [restaurant(self.store(code)) for code in ("9000001", "9000002")]
        rows = [r for r in rows if request.keyword in r["name"]]
        start = (request.page - 1) * request.limit
        return {"simulation": True, "stores": rows[start:start + request.limit], "total": len(rows),
                "page": request.page, "limit": request.limit, "has_more": start + request.limit < len(rows)}

    def pickup_slots(self, code):
        self.store(code)
        today = datetime.now(SHANGHAI).date()
        raw = {"timeInterval": 5, "reservationOptions": [{"date": (today + timedelta(days=i)).isoformat(),
               "timeOptions": [{"timeStart": "07:00", "timeEnd": "23:00", "dayPartCode": 1, "dayPartName": "演示时段"}]} for i in range(2)]}
        return pickup_choices(raw, code) | {"simulation": True}

    def store(self, code):
        if code not in ("9000001", "9000002"):
            raise EzMcdError("demo_store_not_found")
        return {"storeCode": code, "storeName": "示例门店 " + code[-1], "storeAddress": "仅供演示，无实际地址", "simulation": True}

    def reservation(self, code):
        self.store(code)
        return {"simulation": True, "message": "Demo accepts future pickup times with minute precision."}

    def menu(self, args, keyword=""):
        self.store(args.store_code)
        return {"simulation": True, "categories": [{"category_code": "demo", "names": ["演示餐品"], "product_count": 1}],
            "products": [{"product_code": "100001", "name": "演示汉堡", "offer_id": "demo-offer", "product_type": 1,
            "display_price_cny": "10.00", "requires_configuration": False, "display_price_is_final_quote": False}]}

    def product_detail(self, args, product_code, offer_id=None):
        self.store(args.store_code)
        if product_code != "100001":
            raise EzMcdError("demo_product_not_found")
        return {"simulation": True, "product_code": "100001", "name": "演示汉堡", "offer_id": "demo-offer",
                "product_type": 1, "groups": [], "max_quantity": 20, "display_price_is_final_quote": False}

    def configured_cart_draft(self, args, selections):
        if any(s.combo or s.modifiers for s in selections):
            raise EzMcdError("demo_has_no_combo_choices")
        return self.simple_cart_draft(args, selections)

    def quote(self, args):
        self.store(args.store_code)
        when = pickup_time(args.pickup_at)
        if when <= datetime.now(timezone.utc):
            raise EzMcdError("pickup_time_has_passed")
        quantity = self.read()["cart_quantity"]
        amount = quantity * 1000
        cart = {"storeCode": args.store_code, "beType": 1, "orderType": 1, "submit": 1, "daypartCode": "2",
            "realTotalPrice": amount, "totalPrice": amount, "discountAmount": 0, "realDeliveryPrice": 0,
            "products": [{"sequence": 1, "code": "100001", "name": "演示汉堡", "type": "1", "quantity": quantity,
                "saleStatus": 1, "realSubTotalPrice": amount, "subTotalPrice": amount, "comboItems": []}]}
        conf = {"submit": 1, "orderType": "1", "storeInfo": self.store(args.store_code), "dayPart": {"dayPartCode": "2"},
            "storePickUpInfo": {"pickupExpectTimeValue": when.strftime("%Y-%m-%d %H:%M"),
                "eatTypeOptions": [{"selected": True, "code": "locker-in"}]},
            "tablewareInfo": {"options": [{"selected": True, "code": "no"}]},
            "productPriceInfo": {"realTotalAmount": yuan(amount), "totalAmount": yuan(amount), "discountAmount": "0",
                "cartProductList": [{"sequence": 1, "productCode": "100001", "productName": "演示汉堡", "productType": "1",
                    "quantity": quantity, "saleStatus": 1, "uniqueKey": "demo-cart-item", "price": "10.00",
                    "realSubtotal": yuan(amount), "subtotal": yuan(amount), "productImage": "", "comboItemList": [], "couponList": []}]}}
        return checkout(args, cart, conf, "2")

    def cart(self, args):
        q = self.quote(args)
        return {"simulation": True, "items": q["items"]}

    def simple_cart_draft(self, args, selections):
        if len(selections) != 1 or selections[0].product_code != "100001":
            raise EzMcdError("demo_requires_one_demo_product")
        return {"quantity": selections[0].quantity}

    def write_cart(self, body):
        state = self.read()
        state["cart_quantity"] = body["quantity"]
        write_private(self.path, state)
        return {"http_status": 200, "response": {"success": True, "data": {}}}

    def preflight(self, quote):
        if any(o["storeCode"] == quote["store_code"] and o["expectPickUpTime"] == pickup_time(quote["pickup_at"]).strftime("%Y-%m-%d %H:%M:%S") for o in self.read()["orders"].values()):
            raise EzMcdError("existing_server_order_at_same_store_and_pickup_time")

    def create_order(self, quote):
        state = self.read()
        order_id, pay_id = "DEMO-" + uuid.uuid4().hex, "PAY-" + uuid.uuid4().hex
        order = {"orderId": order_id, "payId": pay_id, "storeCode": quote["store_code"],
            "expectPickUpTime": pickup_time(quote["pickup_at"]).strftime("%Y-%m-%d %H:%M:%S"),
            "lockerOrder": True, "realTotalAmount": yuan(quote["amount_cents"]), "realPayAmount": "", "leftPaySecond": 900,
            "orderStatusTitle": "待支付", "paymentChannel": "", "orderProductList": [
                {"productCode": x["product_code"], "quantity": x["quantity"], "comboItemList": []} for x in quote["items"]]}
        state["orders"][order_id] = order
        write_private(self.path, state)
        return {"http_status": 200, "response": {"success": True, "data": {"orderId": order_id, "payId": pay_id}}}

    def payment_ready(self, attempt, quote):
        self.validate_order(attempt, quote, self.read()["orders"][attempt["order_id"]])
        return {"order_id": attempt["order_id"]}

    def preorder(self, body):
        return {"http_status": 200, "response": {"success": True, "data": body}}

    def wallet_inputs(self, attempt, preorder):
        return attempt["order_id"]

    def consume(self, order_id):
        state = self.read()
        order = state["orders"][order_id]
        if order["orderStatusTitle"] != "待支付":
            raise EzMcdError("demo_order_already_paid")
        from .models import cents
        state["balance"] -= cents(order["realTotalAmount"])
        order.update(orderStatusTitle="预约完成", paymentChannel="ARCHCARD", realPayAmount=order["realTotalAmount"], pickupCode="DEMO-001")
        write_private(self.path, state)
        return {"http_status": 200, "response": {"success": True, "code": "0"}}

    def status(self, attempt):
        state = self.read()
        order = state["orders"][attempt["order_id"]]
        return {"order": deepcopy(order), "wallet_cents": state["balance"],
                "cashier": {"payStatus": "PAID" if order["realPayAmount"] else "PAYING"}}
