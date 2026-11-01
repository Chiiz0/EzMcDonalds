"""Explicit fulfillment quote preparation; each switch is sent at most once.

The switch returns client-side confirmation state. A fresh initial GET does
not retain that selection. Keep its receipt and revalidate the unchanged cart
and price before using the selected quote; never infer success from that GET.
"""
from copy import deepcopy
import re
import time

from .catalog import checkout, pickup_methods
from .errors import EzMcdError, error_result
from .models import CheckoutInput, fingerprint
from .transport import data

SWITCH_PATH = "/bff/order/confirmation/eattype/switch"
LOCAL_METHODS = ("eat-in", "locker-in", "take-in-store")


def cart_digest(cart):
    return fingerprint({k: v for k, v in cart.items() if k != "localDateTime"})


def confirmation_digest(quote):
    # Initial GET may select the server default. All other order fields,
    # including prices, coupon IDs, unique keys and tableware, must match.
    return fingerprint({"body": {k: v for k, v in quote["body"].items() if k != "eatTypeCode"},
                        "items": quote["items"], "regular": quote["regular_cents"], "discount": quote["discount_cents"]})


def preparation(args, cart, confirmation, daypart):
    methods = pickup_methods(args, confirmation, daypart)
    current = methods["selected_method"]
    if not methods["requested_method_available"] or current not in LOCAL_METHODS:
        raise EzMcdError("pickup_method_unavailable_or_source_unsupported")
    if confirmation.get("requireLicensePlate") or confirmation.get("activityOrder"):
        raise EzMcdError("pickup_special_flow_requires_official_app")
    # Validate the actual current confirmation before preparing its replacement.
    baseline = checkout(args.model_copy(update={"eat_type": current}), cart, confirmation, daypart)
    if confirmation["productPriceInfo"].get("promotionList") or confirmation["productPriceInfo"].get("laterCouponInfo"):
        raise EzMcdError("pickup_special_flow_requires_official_app")
    body = {"cartItems": deepcopy(baseline["body"]["cartItems"]), "orderType": "1", "dayPartCode": daypart,
        "storeCode": args.store_code, "beCode": "", "beType": "1", "expectDeliveryTime": "", "addressId": "",
        "customerConfirm": True, "skipDtTimeCheck": False, "date": baseline["body"]["date"], "time": baseline["body"]["time"],
        "eatTypeCode": current, "eatTypeCodeNew": args.eat_type, "realTotalAmount": baseline["body"]["realTotalAmount"],
        "realDeliveryPrice": "0", "promotionList": [], "source": 0, "driveDuration": -1, "driveDurationNew": 0,
        "orderMode": "", "pinId": "", "activityOrder": 0, "latitude": 0, "longitude": 0}
    return {"body": body, "cart_digest": cart_digest(cart), "confirmation_digest": confirmation_digest(baseline),
            "tableware": deepcopy(confirmation["tablewareInfo"]), "daypart": daypart,
            "already_selected": current == args.eat_type}


def selected_quote(args, prepared, cart, response):
    payload = data(response)
    if not isinstance(payload, dict) or payload.get("switchResult") is not True or not isinstance(payload.get("confirmInfo"), dict):
        raise EzMcdError("pickup_switch_not_confirmed")
    if cart_digest(cart) != prepared["cart_digest"]:
        raise EzMcdError("pickup_cart_changed_get_new_quote")
    partial = deepcopy(payload["confirmInfo"])
    if partial.get("requireLicensePlate") or partial.get("activityOrder"):
        raise EzMcdError("pickup_special_flow_requires_official_app")
    # Native updateBookInfo preserves tableware from the previous page. Do not
    # fill in missing store, time, method, product or price data from old state.
    if "tablewareInfo" not in partial:
        partial["tablewareInfo"] = prepared["tableware"]
    quote = checkout(args, cart, partial, prepared["daypart"])
    if confirmation_digest(quote) != prepared["confirmation_digest"]:
        raise EzMcdError("pickup_price_or_contents_changed_review")
    return quote | {"pickup_selection": {k: deepcopy(prepared[k]) for k in ("cart_digest", "confirmation_digest")},
                    "pickup_confirmation": pickup_methods(args, partial, prepared["daypart"])}


class PickupQuotes:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def prepare(self, args, cart_ref, key):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key):
            raise EzMcdError("invalid_idempotency_key")
        if not re.fullmatch(r"cart-[0-9a-f]{32}", cart_ref):
            raise EzMcdError("invalid_cart_snapshot_reference")
        account = self.store.account(args.account)
        identity = account["identity"]
        operation = "pickup-" + fingerprint({"identity": identity, "key": key})
        digest = fingerprint({"request": args.model_dump(), "cart_ref": cart_ref})
        with self.store.lock(identity):
            old = self.store.pickup_quote(identity, operation)
            if old:
                if old["request_digest"] != digest:
                    raise EzMcdError("pickup_key_reused_for_different_request")
                return self._status(account, old, repeated=True)
            if self.store.pending_pickup_quote(identity) or self.store.pending_cart_addition(identity):
                raise EzMcdError("pickup_preparation_unresolved_review_cart")
            _, provider = self.service.provider(args.account)
            snapshot = self.store.cart_snapshot(identity, cart_ref)
            if snapshot["scope"] != self.service._cart_scope(args):
                raise EzMcdError("cart_snapshot_scope_changed")
            if time.time() >= snapshot["expires"] or snapshot["clear_key"] is not None:
                raise EzMcdError("cart_snapshot_expired_or_already_used")
            _, _, current_digest = provider.cart_state(args)
            if current_digest != snapshot["digest"]:
                raise EzMcdError("cart_changed_review_before_pickup_quote")
            prepared = provider.prepare_pickup(args)
            if prepared["snapshot_digest"] != snapshot["digest"]:
                raise EzMcdError("cart_changed_review_before_pickup_quote")
            if time.time() >= snapshot["expires"]:
                raise EzMcdError("cart_snapshot_expired_or_already_used")
            if prepared["already_selected"]:
                raise EzMcdError("pickup_already_selected_use_checkout_quote")
            record = {"operation_id": operation, "request_digest": digest, "cart_ref": cart_ref,
                "scope": self.service._cart_scope(args), "request": args.model_dump(), "prepared": prepared,
                "created_at": time.time(), "state": "UNKNOWN", "write_error": None}
            self.store.claim_pickup_quote(identity, record)
            try:
                receipt = provider.switch_pickup(prepared["body"])
                self.store.receipt(operation, "pickup_quote", receipt)
            except (EzMcdError, OSError, ValueError, KeyError, TypeError) as error:
                record["write_error"] = error.code if isinstance(error, EzMcdError) else "pickup_quote_result_unknown"
                self.store.update_pickup_quote(identity, record)
            return self._status(account, record, repeated=False)

    def status(self, alias, operation_id=None):
        if operation_id is not None and not re.fullmatch(r"pickup-[0-9a-f]{64}", operation_id):
            raise EzMcdError("invalid_pickup_operation_reference")
        account = self.store.account(alias)
        with self.store.lock(account["identity"]):
            record = self.store.pickup_quote(account["identity"], operation_id)
            if record is None:
                raise EzMcdError("pickup_quote_not_found_for_account")
            return self._status(account, record, repeated=True)

    def _status(self, account, record, *, repeated):
        from .service import quote_summary
        result, error_code = {}, None
        try:
            if time.time() >= record["created_at"] + 120:
                raise EzMcdError("pickup_quote_expired_review_cart")
            _, provider = self.service.provider(account["alias"])
            if record.get("quote_id"):
                q = self.store.quote(account["identity"], record["quote_id"])
                provider.revalidate_pickup_quote(q)
            else:
                receipt = self.store.write_receipt(record["operation_id"], "pickup_quote")
                if receipt is None:
                    raise EzMcdError("pickup_quote_response_missing_query_only")
                args = CheckoutInput.model_validate(record["request"])
                q = provider.finish_pickup(args, record["prepared"], receipt)
                q = self.service._quote_metadata(account, provider, args, q)
                q["expires_at"] = min(q["expires_at"], record["created_at"] + 120)
                # Quote and operation are linked in a single transaction.
                completed = record | {"quote_id": q["quote_id"], "state": "READY"}
                self.store.complete_pickup_quote(account["identity"], completed, q)
                record.update(completed)
            record["state"] = "READY"
            result = quote_summary(q)
        except (EzMcdError, OSError, ValueError, KeyError, TypeError) as error:
            error_code = error.code if isinstance(error, EzMcdError) else "pickup_quote_check_unavailable"
            record["state"] = "EXPIRED" if error_code == "pickup_quote_expired_review_cart" else "UNKNOWN"
        self.store.update_pickup_quote(account["identity"], record)
        guidance = error_result(error_code) if error_code else {}
        return result | {"account": account["alias"], "operation_id": record["operation_id"], "request": record["request"],
            "state": record["state"], "error": error_code, "write_error": record["write_error"],
            "message": guidance.get("message"), "recovery_action": guidance.get("next_action"),
            "repeated": repeated, "switch_attempted": not repeated, "automatic_write_retry": False,
            "next_action": "review_quote_and_policy_before_submit" if record["state"] == "READY" else "review_cart_and_original_preparation",
            "order_created": False, "payment_attempted": False, "simulation": account["provider"] == "demo"}
