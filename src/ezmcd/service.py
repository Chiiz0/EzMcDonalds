"""Account-scoped application service shared by CLI and MCP."""
from datetime import datetime, timezone
import re
import time
import uuid

from .accounts import public_account
from .errors import EzMcdError
from .models import CheckoutInput, ItemSelection, Policy, cents, pickup_time, yuan
from .provider import McdProvider
from .transport import data


def quote_summary(q):
    return {k: q[k] for k in ("quote_id", "account", "store_code", "store_name", "pickup_at", "eat_type", "items", "discount_names", "expires_at", "simulation")} | {
        "amount_cny": yuan(q["amount_cents"]), "regular_cny": yuan(q["regular_cents"]),
        "discount_cny": yuan(q["discount_cents"]), "order_created": False,
        "wallet_balance_cny": yuan(q["wallet_before_cents"]),
        "wallet_sufficient": q["wallet_before_cents"] >= q["amount_cents"],
        "wallet_shortfall_cny": yuan(max(0, q["amount_cents"] - q["wallet_before_cents"])),
        "checkout_blockers": q.get("checkout_blockers", []),
        "pickup_confirmation": q.get("pickup_confirmation"),
        "fresh_order_checks_still_required": True}


def attempt_summary(a):
    paid = a.get("state") == "PAID" and a.get("paid_verified")
    pickup_pending = bool(paid and not a.get("pickup_code"))
    next_action = ("query_order_status" if a.get("status_query_failed") else
                   "review_cancelled_order" if a.get("state") == "CANCELLED" else
                   "query_pickup_code_later" if pickup_pending else "collect_at_pickup_time" if paid else
                   "query_order_status" if a.get("order_id") else "review_in_official_app")
    return {k: a.get(k) for k in ("attempt_id", "state", "order_id", "pickup_code", "pickup_at", "store_code", "paid_verified", "stop_reason", "simulation", "status_query_failed", "status_checked_at")} | {
        "amount_cny": yuan(a["amount_cents"]), "automatic_write_retry": False,
        "pickup_code_pending": pickup_pending, "next_action": next_action,
        "wallet_balance_cny": yuan(a["wallet_after_cents"]) if "wallet_after_cents" in a else None}


class Service:
    def __init__(self, store, provider_factory=McdProvider):
        self.store, self.provider_factory = store, provider_factory

    def accounts(self):
        return [public_account(a) for a in self.store.accounts()]

    def orders(self, alias, *, pickup_date=None, state=None, limit=20, offset=0):
        # Local records remain available after revocation or an interrupted login.
        # Deliberately do not construct a provider or make a live account check.
        account = self.store.account(alias)
        page = self.store.list_attempts(account["identity"], pickup_date=pickup_date, state=state, limit=limit, offset=offset)
        orders = []
        for attempt in page.pop("attempts"):
            row = attempt_summary(attempt) | {"store_name": None, "items": None, "discount_names": None,
                                               "meal_details_available": False,
                "next_action": "query_order_status" if attempt.get("order_id") else "review_in_official_app"}
            try:
                quote = self.store.quote(account["identity"], attempt["quote_id"])
            except EzMcdError as error:
                if error.code != "quote_not_found_for_account":
                    raise
            else:
                row.update({k: quote[k] for k in ("store_name", "items", "discount_names")})
                row["meal_details_available"] = True
            orders.append(row)
        return {"account": alias, "orders": orders, **page, "pickup_timezone": "Asia/Shanghai",
                "sort": "pickup_date_desc_then_recorded_order_desc", "source": "local_project_journal",
                "live_status_checked": False, "includes_official_app_orders": False,
                "next_step": "use_order_status_for_live_confirmation", "order_created": False, "payment_attempted": False}

    def provider(self, alias):
        account = self.store.account(alias)
        if account.get("revoked"):
            raise EzMcdError("account_revoked_reconnect_locally")
        refresh_state = account.get("session_refresh", {}).get("state")
        if refresh_state == "CANDIDATE":
            raise EzMcdError("session_refresh_received_resume_verification")
        if refresh_state in ("CLAIMED", "STOPPED"):
            raise EzMcdError("session_refresh_already_attempted_relogin_locally")
        if account["provider"] == "demo":
            from .demo import DemoProvider
            return account, DemoProvider(account, self.store)
        return account, self.provider_factory(account)

    def check_account(self, alias):
        account, provider = self.provider(alias)
        balance = provider.wallet()["balance_cents"]
        return public_account(account, session_verified=True) | {"session_status": "verified_now", "wallet_balance_cny": yuan(balance)}

    def refresh_account(self, alias):
        from .sessions import refresh_account
        return refresh_account(self.store, alias, provider_factory=self.provider_factory)

    def set_policy(self, alias, policy):
        # Local CLI only. This method is intentionally not registered as an MCP tool.
        def update(account):
            if account.get("revoked") and policy.allow_checkout:
                raise EzMcdError("account_revoked_reconnect_locally")
            account["policy"] = policy.model_dump()
            return account
        self.store.modify_account(alias, update)
        return {"account": alias, "policy": policy.model_dump()}

    def policy(self, account, q):
        policy = Policy.model_validate(self.store.account(account["alias"])["policy"])
        if not policy.allow_checkout:
            raise EzMcdError("local_checkout_policy_required")
        if policy.expires_at and pickup_time(policy.expires_at) <= datetime.now(timezone.utc):
            raise EzMcdError("checkout_policy_expired")
        if (q["amount_cents"] > policy.max_order_cents or
                policy.allowed_stores and q["store_code"] not in policy.allowed_stores):
            raise EzMcdError("order_outside_authorized_policy")
        return policy

    def quote(self, args):
        account, provider = self.provider(args.account)
        with self.store.lock(account["identity"]):
            q = provider.quote(args)
            q = self._quote_metadata(account, provider, args, q)
            self.store.save_quote(account["identity"], q)
        return quote_summary(q)

    def _quote_metadata(self, account, provider, args, q):
        q.update(quote_id=uuid.uuid4().hex, account=args.account, request=args.model_dump(),
                 expires_at=time.time() + 120, wallet_before_cents=provider.wallet()["balance_cents"],
                 simulation=account["provider"] == "demo")
        q["checkout_blockers"] = public_account(account, session_verified=True)["readiness"]["checkout_blockers"]
        if q["wallet_before_cents"] < q["amount_cents"]:
            q["checkout_blockers"].append("insufficient_wallet_balance")
        try:
            self.policy(account, q)
        except EzMcdError as error:
            if error.code not in q["checkout_blockers"] and not (error.code == "local_checkout_policy_required" and "checkout_disabled" in q["checkout_blockers"]):
                q["checkout_blockers"].append(error.code)
        return q

    def quote_pickup(self, args, cart_ref, key):
        from .pickup import PickupQuotes
        return PickupQuotes(self).prepare(args, cart_ref, key)

    def pickup_quote_status(self, alias, operation_id=None):
        from .pickup import PickupQuotes
        return PickupQuotes(self).status(alias, operation_id)

    def add_simple_items(self, args, items, request_key):
        return self._add_items(args, items, request_key, configured=False)

    @staticmethod
    def _cart_scope(args):
        return {"store_code": args.store_code, "pickup_at": args.pickup_at, "eat_type": args.eat_type}

    def _cart_snapshot(self, account, provider, args):
        cart, _, digest = provider.cart_state(args)
        ref, expires = "cart-" + uuid.uuid4().hex, time.time() + 120
        self.store.save_cart_snapshot(account["identity"], ref, self._cart_scope(args), digest, expires)
        return cart | {"cart_ref": ref, "cart_ref_expires_at": expires,
                       "order_created": False, "payment_attempted": False}

    def cart(self, args):
        account, provider = self.provider(args.account)
        with self.store.lock(account["identity"]):
            return self._cart_snapshot(account, provider, args)

    def clear_cart(self, args, cart_ref, request_key):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_key):
            raise EzMcdError("invalid_idempotency_key")
        if not re.fullmatch(r"cart-[0-9a-f]{32}", cart_ref):
            raise EzMcdError("invalid_cart_snapshot_reference")
        account, provider = self.provider(args.account)
        identity = account["identity"]
        with self.store.lock(identity):
            snapshot = self.store.cart_snapshot(identity, cart_ref)
            if snapshot["scope"] != self._cart_scope(args):
                raise EzMcdError("cart_snapshot_scope_changed")
            if snapshot["clear_key"] is not None:
                if snapshot["clear_key"] != request_key:
                    raise EzMcdError("cart_snapshot_expired_or_already_used")
                # Even after restart, a repeated call only reads today's cart.
                # It must never clear items added since the original operation.
                cart = self._cart_snapshot(account, provider, args)
                return cart | {"cart_empty": not cart["items"], "clear_attempted": False,
                    "repeated": True, "original_clear_verified": bool(snapshot["confirmed"]),
                    "automatic_write_retry": False}
            if time.time() >= snapshot["expires"]:
                raise EzMcdError("cart_snapshot_expired_or_already_used")
            cart, body, digest = provider.cart_state(args)
            if digest != snapshot["digest"]:
                raise EzMcdError("cart_changed_review_before_clear")
            will_write = bool(cart["items"])
            self.store.claim_cart_clear(identity, cart_ref, request_key, will_write=will_write)
            if will_write:
                result = provider.clear_cart(body)
                self.store.receipt(cart_ref, "cart_clear", result)
                data(result)
            current = self._cart_snapshot(account, provider, args)
            if current["items"]:
                raise EzMcdError("cart_clear_not_confirmed_query_only")
            self.store.confirm_cart_clear(identity, cart_ref)
            return current | {"cart_empty": True, "clear_attempted": will_write,
                "repeated": False, "original_clear_verified": True, "automatic_write_retry": False}

    def add_configured_items(self, args, items, request_key):
        return self._add_items(args, items, request_key, configured=True)

    def _add_items(self, args, items, request_key, *, configured):
        from .cart import CartAdditions
        return CartAdditions(self).add(args, items, request_key, configured=configured)

    def cart_add_status(self, alias, operation_id=None):
        from .cart import CartAdditions
        return CartAdditions(self).status(alias, operation_id)

    def submit(self, alias, quote_id, request_key):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_key):
            raise EzMcdError("invalid_idempotency_key")
        account, provider = self.provider(alias)
        identity = account["identity"]
        with self.store.lock(identity):
            q = self.store.quote(identity, quote_id)
            existing = self.store.existing(identity, request_key, q["intent"])
            if existing:
                return self._status(account, provider, existing)
            if self.store.pending_pickup_quote(identity):
                raise EzMcdError("pickup_preparation_unresolved_review_cart")
            policy = self.policy(account, q)
            if time.time() >= q["expires_at"]:
                raise EzMcdError("quote_expired_get_new_quote")
            fresh = (provider.revalidate_pickup_quote(q) if q.get("pickup_selection") else
                     provider.quote(CheckoutInput.model_validate(q["request"])))
            if fresh["checkout_digest"] != q["checkout_digest"] or fresh["intent"] != q["intent"]:
                raise EzMcdError("checkout_changed_get_new_quote")
            provider.preflight(q)
            balance = provider.wallet()["balance_cents"]
            if balance < q["amount_cents"]:
                raise EzMcdError("insufficient_wallet_balance")
            attempt = {"attempt_id": uuid.uuid4().hex, "state": "RESERVED", "quote_id": quote_id,
                "store_code": q["store_code"], "pickup_at": q["pickup_at"], "amount_cents": q["amount_cents"],
                "wallet_before_cents": balance, "paid_verified": False, "simulation": account["provider"] == "demo"}
            self.store.reserve(identity, request_key, q, policy, attempt)
            try:
                result = self._write(attempt, "order", lambda: provider.create_order(q))
                created = data(result)
                if not isinstance(created, dict) or any(not isinstance(created.get(k), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", created[k]) for k in ("orderId", "payId")):
                    raise EzMcdError("order_result_unknown_query_only")
                attempt.update(state="CREATED", order_id=created["orderId"], pay_id=created["payId"])
                self.store.update_attempt(identity, attempt)
                return self._pay(account, provider, attempt, q)
            except (EzMcdError, OSError, ValueError, KeyError, TypeError) as error:
                attempt.update(state="UNKNOWN", stop_reason=error.code if isinstance(error, EzMcdError) else "unexpected_response_query_only")
                self.store.update_attempt(identity, attempt)
                return self._status(account, provider, attempt)

    def _write(self, attempt, stage, callback):
        # Committed before network I/O; a crash leaves a permanent unknown claim.
        self.store.claim(attempt["attempt_id"], stage)
        result = callback()
        self.store.receipt(attempt["attempt_id"], stage, result)
        return result

    def pay_existing(self, alias, attempt_id):
        account, provider = self.provider(alias)
        with self.store.lock(account["identity"]):
            attempt = self.store.attempt(account["identity"], attempt_id)
            q = self.store.quote(account["identity"], attempt["quote_id"])
            self.policy(account, q)
            if attempt["state"] != "CREATED":
                return self._status(account, provider, attempt)
            try:
                return self._pay(account, provider, attempt, q)
            except (EzMcdError, OSError, ValueError, KeyError, TypeError) as error:
                attempt.update(state="UNKNOWN", stop_reason=error.code if isinstance(error, EzMcdError) else "unexpected_response_query_only")
                self.store.update_attempt(account["identity"], attempt)
                return self._status(account, provider, attempt)

    def _pay(self, account, provider, attempt, quote):
        body = provider.payment_ready(attempt, quote)
        attempt["state"] = "PAYMENT_ATTEMPTED"
        self.store.update_attempt(account["identity"], attempt)
        preorder = self._write(attempt, "preorder", lambda: provider.preorder(body))
        inputs = provider.wallet_inputs(attempt, preorder)
        result = self._write(attempt, "consume", lambda: provider.consume(inputs))
        payload = result["response"]
        if result["http_status"] != 200 or str(payload.get("code")) != "0":
            attempt["stop_reason"] = "payment_requires_official_app_or_status_query"
            return self._status(account, provider, attempt)
        # A successful consume can precede the order/cashier's settled view.
        # Only query the existing order, with bounded delays; never resend money.
        result = self._status(account, provider, attempt)
        for delay in (1, 2, 4):
            if result["state"] != "UNKNOWN" or result["status_query_failed"]:
                break
            time.sleep(delay)
            result = self._status(account, provider, attempt)
        return result

    def status(self, alias, attempt_id):
        account, provider = self.provider(alias)
        with self.store.lock(account["identity"]):
            return self._status(account, provider, self.store.attempt(account["identity"], attempt_id))

    def _status(self, account, provider, attempt):
        if not attempt.get("order_id"):
            # Unknown order response: do not guess an identity or create a replacement.
            return attempt_summary(attempt)
        try:
            q = self.store.quote(account["identity"], attempt["quote_id"])
            state = provider.status(attempt)
            attempt["status_query_failed"] = False
            order = state["order"]
            provider.validate_order(attempt, q, order)
            attempt["status_checked_at"] = time.time()
            attempt["wallet_after_cents"] = state["wallet_cents"]
            attempt["pickup_code"] = order.get("pickupCode") or None
            title = order.get("orderStatusTitle", "")
            if title == "已取消":
                attempt.update(state="CANCELLED", paid_verified=False)
            elif (state["cashier"].get("payStatus") == "PAID" and title == "预约完成"
                    and order.get("paymentChannel") == "ARCHCARD"
                    and cents(order.get("realPayAmount") or 0) == q["amount_cents"]
                    and attempt["wallet_before_cents"] - state["wallet_cents"] == q["amount_cents"]):
                attempt.update(state="PAID", paid_verified=True, stop_reason=None)
            elif not attempt.get("paid_verified"):
                attempt["state"] = "UNKNOWN"  # No status may authorize a write retry.
            self.store.update_attempt(account["identity"], attempt)
        except (EzMcdError, OSError, ValueError, KeyError, TypeError):
            attempt["status_query_failed"] = True
        return attempt_summary(attempt)
