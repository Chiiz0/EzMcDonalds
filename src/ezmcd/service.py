"""Account-scoped application service shared by CLI and MCP."""
from datetime import datetime, timezone
import re
import time
import uuid

from .accounts import public_account
from .errors import EzMcdError
from .models import CheckoutInput, ItemSelection, Policy, cents, pickup_time, yuan, fingerprint
from .provider import McdProvider
from .transport import data


def quote_summary(q):
    return {k: q[k] for k in ("quote_id", "account", "store_code", "store_name", "pickup_at", "eat_type", "items", "discount_names", "expires_at", "simulation")} | {
        "amount_cny": yuan(q["amount_cents"]), "regular_cny": yuan(q["regular_cents"]),
        "discount_cny": yuan(q["discount_cents"]), "order_created": False}


def attempt_summary(a):
    return {k: a.get(k) for k in ("attempt_id", "state", "order_id", "pickup_code", "pickup_at", "store_code", "paid_verified", "stop_reason", "simulation", "status_query_failed")} | {
        "amount_cny": yuan(a["amount_cents"]), "automatic_write_retry": False,
        "wallet_balance_cny": yuan(a["wallet_after_cents"]) if "wallet_after_cents" in a else None}


class Service:
    def __init__(self, store, provider_factory=McdProvider):
        self.store, self.provider_factory = store, provider_factory

    def accounts(self):
        return [public_account(a) for a in self.store.accounts()]

    def provider(self, alias):
        account = self.store.account(alias)
        if account.get("revoked"):
            raise EzMcdError("account_revoked_reconnect_locally")
        if account["provider"] == "demo":
            from .demo import DemoProvider
            return account, DemoProvider(account, self.store)
        return account, self.provider_factory(account)

    def check_account(self, alias):
        account, provider = self.provider(alias)
        balance = provider.wallet()["balance_cents"]
        return public_account(account) | {"session_status": "verified_now", "wallet_balance_cny": yuan(balance)}

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
            q.update(quote_id=uuid.uuid4().hex, account=args.account, request=args.model_dump(),
                     expires_at=time.time() + 120, wallet_before_cents=provider.wallet()["balance_cents"],
                     simulation=account["provider"] == "demo")
            if q["wallet_before_cents"] < q["amount_cents"]:
                raise EzMcdError("insufficient_wallet_balance")
            self.store.save_quote(account["identity"], q)
        return quote_summary(q)

    def add_simple_items(self, args, items, request_key):
        return self._add_items(args, items, request_key, configured=False)

    def add_configured_items(self, args, items, request_key):
        return self._add_items(args, items, request_key, configured=True)

    def _add_items(self, args, items, request_key, *, configured):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_key):
            raise EzMcdError("invalid_idempotency_key")
        account, provider = self.provider(args.account)
        op = "cart-" + fingerprint({"identity": account["identity"], "key": request_key})
        with self.store.lock(account["identity"]):
            draft = (provider.configured_cart_draft if configured else provider.simple_cart_draft)(args, items)
            self.store.claim(op, "cart")
            result = provider.write_cart(draft)
            self.store.receipt(op, "cart", result)
            data(result)
            return provider.cart(args) | {"order_created": False}

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
            policy = self.policy(account, q)
            if time.time() >= q["expires_at"]:
                raise EzMcdError("quote_expired_get_new_quote")
            fresh = provider.quote(CheckoutInput.model_validate(q["request"]))
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
