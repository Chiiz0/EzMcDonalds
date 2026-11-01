from datetime import datetime, timezone
import json
import re

from .catalog import checkout, menu_products, public_menu, reservation_daypart, cart_items, menu_categories, membership_assets
from .errors import EzMcdError
from .discovery import cities, pickup_choices, resolve_city, store_results
from .models import pickup_time, cents, yuan
from .protocol.signing import device_update_metadata
from .products import choose_offer, configured_product, public_detail
from .transport import Transport, data


class McdProvider:
    def __init__(self, account, transport=None):
        self.account = account
        self.http = transport or Transport(account)

    def get(self, path, params=None):
        return data(self.http.send("GET", path, params))

    def wallet(self):
        return {"balance_cents": cents(self.get("/bff/member/mwallet/balance")["balance"])}

    def assets(self):
        return membership_assets(self.get("/bff/promotion/coupons/rightCards", {"scene": "3"}))

    def favorites(self):
        return self.get("/bff/store/stores/collection/list", {"beType": "1"})

    def cities(self, keyword=""):
        if not isinstance(keyword, str) or len(keyword) > 60:
            raise EzMcdError("invalid_city_keyword")
        return {"cities": cities(self.get("/bff/store/cities/group"), keyword)}

    def search_stores(self, request):
        if request.city:
            code = request.city if re.fullmatch(r"[0-9]{6}", request.city) else resolve_city(
                self.get("/bff/store/cities/group"), request.city)
            raw = self.get("/bff/store/stores", {"pageNo": str(request.page), "pageSize": str(request.limit),
                "cityCode": code, "keyword": request.keyword, "hotTagCode": "", "beType": "1", "dayPartCodes": ""})
            return store_results(raw, request) | {"city_code": code}
        raw = self.get("/bff/store/stores/vicinity", {"latitude": str(request.latitude),
            "longitude": str(request.longitude), "showType": "2", "beType": "1", "orderType": "1"})
        return store_results(raw, request, nearby=True)

    def pickup_slots(self, code):
        return pickup_choices(self.reservation(code), code)

    def store(self, code):
        if not re.fullmatch(r"[0-9]{7}", code):
            raise EzMcdError("invalid_store_code")
        return self.get("/bff/store/stores/" + code)

    def reservation(self, code):
        self.store(code)
        return self.get(f"/bff/store/stores/{code}/menu/reservationTimeOption", {
            "beCode": "", "beType": "1", "dayPartCode": "", "dayPartCodes": "",
            "isGroupMeal": "0", "type": "pickup", "fixedOptDate": ""})

    def context(self, args):
        when = pickup_time(args.pickup_at)
        if when <= datetime.now(timezone.utc):
            raise EzMcdError("pickup_time_has_passed")
        daypart = reservation_daypart(self.reservation(args.store_code), when)
        params = {"storeCode": args.store_code, "orderType": "1", "orderMode": "", "beCode": "",
                  "date": when.date().isoformat(), "time": when.strftime("%H:%M")}
        return daypart, params

    def menu(self, args, keyword=""):
        daypart, params = self.context(args)
        raw = self.get("/bff/spc/menu", params | {"isPromoter": "0", "daypartCodes": daypart})
        return {"store_code": args.store_code, "pickup_at": args.pickup_at,
                "categories": menu_categories(raw), "products": public_menu(raw, keyword)}

    def cart(self, args):
        daypart, params = self.context(args)
        raw = self.get("/bff/cart/carts", params | {"cartType": "1", "channelCode": "03", "pinId": "", "daypartCode": daypart})
        return {"items": cart_items(raw), "store_code": args.store_code, "pickup_at": args.pickup_at}

    def _detail(self, offer, daypart, params):
        code = str(offer["productCode"])
        return data(self.http.send("POST", "/bff/spc/products/detail/" + code, body=params | {
            "channelCode": "03", "productCode": code, "daypartCode": daypart,
            "cardId": offer.get("cardId") or "", "pinId": "", "date": params["date"].replace("-", ""),
            "fromCombo": False, "customization": False}))

    def product_detail(self, args, product_code, offer_id=None):
        daypart, params = self.context(args)
        menu = menu_products(self.get("/bff/spc/menu", params | {"isPromoter": "0", "daypartCodes": daypart}))
        offer = choose_offer(menu, product_code, offer_id)
        result = public_detail(self._detail(offer, daypart, params), offer)
        for variant in result["variants"]:
            variant["menu_offers"] = [{"offer_id": p["offer_id"], "card_name": p.get("cardName"),
                "display_price_cny": yuan(p["priceInfo"]["eatInPrice"]) if type(p.get("priceInfo", {}).get("eatInPrice")) is int else None}
                for p in menu if p["productCode"] == variant["product_code"] and str(p.get("productType")) in ("1", "7")]
            variant["next_step"] = "product_detail_with_variant_code_and_menu_offer" if variant["menu_offers"] else "variant_not_in_current_menu_use_official_app"
        return result

    def configured_cart_draft(self, args, selections):
        if not 1 <= len(selections) <= 20:
            raise EzMcdError("select_between_1_and_20_products")
        daypart, params = self.context(args)
        cart = self.get("/bff/cart/carts", params | {"cartType": "1", "channelCode": "03", "pinId": "", "daypartCode": daypart})
        if (str(cart.get("storeCode")) != args.store_code or cart.get("beType") != 1 or cart.get("orderType") != 1):
            raise EzMcdError("cart_store_or_fulfillment_mismatch")
        if cart.get("products"):
            raise EzMcdError("cart_not_empty_use_existing_cart_or_clear_in_official_app")
        menu = menu_products(self.get("/bff/spc/menu", params | {"isPromoter": "0", "daypartCodes": daypart}))
        products = []
        for index, selection in enumerate(selections):
            offer = choose_offer(menu, selection.product_code, selection.offer_id)
            products.append(configured_product(selection, offer, self._detail(offer, daypart, params), index + 1))
        return params | {"cartType": "1", "channelCode": "03", "pinId": "", "dataSource": 1,
                         "daypartCode": daypart, "scenes": "", "products": products}

    def quote(self, args):
        daypart, params = self.context(args)
        cart = self.get("/bff/cart/carts", params | {"cartType": "1", "channelCode": "03", "pinId": "", "daypartCode": daypart})
        confirmation = self.get("/bff/order/confirmation/info", {"mealTypeId": "1", "orderType": "1",
            "storeCode": args.store_code, "beCode": "", "dayPartCode": daypart, "expectDeliveryTime": "",
            "date": params["date"], "time": params["time"], "addressId": ""})
        return checkout(args, cart, confirmation, daypart)

    def simple_cart_draft(self, args, selections):
        if not 1 <= len(selections) <= 20:
            raise EzMcdError("select_between_1_and_20_products")
        daypart, params = self.context(args)
        cart = self.get("/bff/cart/carts", params | {"cartType": "1", "channelCode": "03", "pinId": "", "daypartCode": daypart})
        if cart.get("products"):
            raise EzMcdError("cart_not_empty_use_existing_cart_or_clear_in_official_app")
        menu = menu_products(self.get("/bff/spc/menu", params | {"isPromoter": "0", "daypartCodes": daypart}))
        products = []
        for index, selection in enumerate(selections):
            if selection.combo or selection.modifiers is not None:
                raise EzMcdError("use_cart_add_configured_for_combo_choices")
            matches = {p["offer_id"]: p for p in menu if str(p.get("productCode")) == selection.product_code
                       and (selection.offer_id is None or p["offer_id"] == selection.offer_id)}
            if len(matches) != 1:
                raise EzMcdError("select_exact_live_menu_offer")
            p = next(iter(matches.values()))
            if (str(p.get("productType")) != "1" or p.get("isChoices") or p.get("needDetail")
                    or p.get("cardId") or p.get("productPromotions")):
                raise EzMcdError("configuration_required_select_in_official_app_then_quote_cart")
            products.append({"code": selection.product_code, "type": "1", "quantity": selection.quantity,
                             "sequence": index + 1, "operationType": 0, "useRightCard": False})
        return params | {"cartType": "1", "channelCode": "03", "pinId": "", "dataSource": 1,
                         "daypartCode": daypart, "scenes": "", "products": products}

    def write_cart(self, body):
        return self.http.send("PUT", "/bff/cart/carts", body=body)

    def preflight(self, quote):
        self.http.signer.runtime()  # Fail before order creation if signing state is stale.
        rows = []
        finished = False
        for page in range(1, 11):
            result = self.get("/bff/order/orders", {"orderCategoryId": "0", "page": str(page), "size": "10"})
            batch = result.get("list")
            if not isinstance(batch, list):
                raise EzMcdError("order_list_schema_changed")
            rows.extend(batch)
            if len(batch) < 10:
                finished = True
                break
        if not finished:
            raise EzMcdError("order_history_requires_manual_duplicate_review")
        # Read matching details instead of interpreting PAID as fulfilment status.
        for row in rows:
            if str(row.get("storeCode")) != quote["store_code"] or row.get("orderStatus") == "已取消":
                continue
            detail = self.get("/bff/order/orders/" + row["orderId"])
            if (detail.get("orderStatusTitle") != "已取消"
                    and detail.get("expectPickUpTime") == pickup_time(quote["pickup_at"]).strftime("%Y-%m-%d %H:%M:%S")):
                raise EzMcdError("existing_server_order_at_same_store_and_pickup_time")

    def create_order(self, quote):
        return self.http.send("POST", "/bff/order/orders", body=quote["body"])

    def payment_ready(self, attempt, quote):
        state = self.status(attempt)
        order = state["order"]
        self.validate_order(attempt, quote, order)
        if state["cashier"].get("payStatus") == "PAID":
            raise EzMcdError("already_paid_query_only")
        if (order.get("orderStatusTitle") != "待支付" or order.get("leftPaySecond", 0) <= 30
                or state["cashier"].get("payStatus") != "PAYING" or order.get("realPayAmount")):
            raise EzMcdError("order_not_payable")
        if state["wallet_cents"] != attempt["wallet_before_cents"]:
            raise EzMcdError("wallet_changed_before_payment_query_only")
        if state["wallet_cents"] < quote["amount_cents"]:
            raise EzMcdError("insufficient_wallet_balance")
        channels = self.get("/bff/cashier/channels", {"payId": attempt["pay_id"], "orderId": attempt["order_id"],
            "mcdId": self.account["profile"]["fields"]["meddyId"], "source": "0"})
        eligible = [x for x in channels.get("channelInfos", []) if x.get("code") == "ARCHCARD"]
        if channels.get("realTotalAmount") != quote["amount_cents"] or len(eligible) != 1:
            raise EzMcdError("cashier_amount_or_channel_changed")
        channel = eligible[0]
        if str(channel.get("freePayStatus")) not in ("1", "4"):
            raise EzMcdError("wallet_payment_requires_user_action")
        return {"payId": attempt["pay_id"], "payChannel": "ARCHCARD",
            "freePayFlg": str(channel["freePayStatus"]) == "4", "devSysUpdt": device_update_metadata(), "source": "0"}

    def preorder(self, body):
        return self.http.send("POST", "/bff/cashier/preorder", body=body)

    def wallet_inputs(self, attempt, preorder):
        result = data(preorder)
        if result.get("securityContext"):
            raise EzMcdError("official_payment_verification_required")
        if (result.get("responseCode") != "SUCCESS" or result.get("payStatus") != "PAYING"
                or result.get("channelPayDestination") != "ARCHCARD"
                or result.get("orderId") != attempt["order_id"] or result.get("payId") != attempt["pay_id"]):
            raise EzMcdError("preorder_requires_status_reconciliation")
        channel = json.loads(result["channelPayData"])
        sequence = channel.get("bizSeqNo")
        if not isinstance(sequence, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sequence):
            raise EzMcdError("invalid_wallet_business_sequence")
        cashier = self.get("/bff/cashier/" + attempt["pay_id"] + "/status", {"source": "0"})
        if cashier.get("payStatus") != "PAYING":
            raise EzMcdError("cashier_changed_query_only")
        # Verified ordering: fetch AFTER preorder. Earlier tokens can be invalidated.
        token = self.get("/bff/member/mwallet/token")
        if not isinstance(token, str) or not token:
            raise EzMcdError("wallet_token_unavailable")
        return sequence, token

    def consume(self, inputs):
        return self.http.consume(*inputs)

    def status(self, attempt):
        order = self.get("/bff/order/orders/" + attempt["order_id"])
        if order.get("orderId") != attempt["order_id"] or order.get("payId") != attempt["pay_id"]:
            raise EzMcdError("order_identity_changed")
        cashier = self.get("/bff/cashier/" + attempt["pay_id"] + "/status", {"source": "0"})
        return {"order": order, "cashier": cashier, "wallet_cents": self.wallet()["balance_cents"]}

    @staticmethod
    def validate_order(attempt, quote, order):
        expected = sorted((x["product_code"], x["quantity"]) for x in quote["items"])
        actual = sorted((str(x["productCode"]), x["quantity"]) for x in order.get("orderProductList", []))
        if (order.get("orderId") != attempt["order_id"] or order.get("payId") != attempt["pay_id"]
                or str(order.get("storeCode")) != quote["store_code"] or order.get("lockerOrder") is not True
                or order.get("expectPickUpTime") != pickup_time(quote["pickup_at"]).strftime("%Y-%m-%d %H:%M:%S")
                or cents(order.get("realTotalAmount")) != quote["amount_cents"] or expected != actual):
            raise EzMcdError("created_order_does_not_match_quote")
        expected_components = sorted((i["code"], i["quantity"]) for x in quote["items"] for g in x["components"] for i in g["comboProducts"])
        actual_components = sorted((i["productCode"], i["quantity"]) for x in order.get("orderProductList", []) for i in x.get("comboItemList", []))
        if expected_components != actual_components:
            raise EzMcdError("created_order_components_changed")
