"""Persistent one-shot cart additions and read-only recovery."""
import re
import time

from .errors import EzMcdError
from .models import CheckoutInput, fingerprint
from .products import verify_cart_contents
from .transport import data


class CartAdditions:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def add(self, args, items, key, *, configured):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key):
            raise EzMcdError("invalid_idempotency_key")
        account = self.store.account(args.account)
        identity = account["identity"]
        operation = "cart-" + fingerprint({"identity": identity, "key": key})
        intent = fingerprint({"request": args.model_dump(), "items": [i.model_dump() for i in items], "configured": configured})
        with self.store.lock(identity):
            previous = self.store.cart_addition(identity, operation)
            if previous:
                if previous["request_digest"] != intent:
                    raise EzMcdError("cart_key_reused_for_different_request")
                return self._status(account, previous, repeated=True)
            # Old versions have a write claim without a bound draft. Never
            # re-create that operation or pretend its receipt proves this intent.
            if self.store.write_claimed(operation, "cart"):
                raise EzMcdError("legacy_cart_write_query_only")
            if self.store.pending_cart_addition(identity):
                raise EzMcdError("cart_addition_unresolved_query_status")
            _, provider = self.service.provider(args.account)
            draft = (provider.configured_cart_draft if configured else provider.simple_cart_draft)(args, items)
            record = {"operation_id": operation, "request_digest": intent, "created_at": time.time(),
                "request": args.model_dump(), "scope": self.service._cart_scope(args),
                "selections": [i.model_dump() for i in items], "draft": draft,
                "state": "UNAVAILABLE", "write_receipt_verified": False, "ever_matched": False,
                "write_error": None, "last_checked_at": None}
            self.store.claim_cart_addition(identity, record)
            try:
                result = provider.write_cart(draft)
                self.store.receipt(operation, "cart", result)
                data(result)
                record["write_receipt_verified"] = True
            except (EzMcdError, OSError, ValueError, KeyError, TypeError) as error:
                record["write_error"] = error.code if isinstance(error, EzMcdError) else "cart_write_result_unknown"
            self.store.update_cart_addition(identity, record)
            return self._status(account, record, repeated=False)

    def status(self, alias, operation_id=None):
        if operation_id is not None and not re.fullmatch(r"cart-[0-9a-f]{64}", operation_id):
            raise EzMcdError("invalid_cart_operation_reference")
        account = self.store.account(alias)
        with self.store.lock(account["identity"]):
            record = self.store.cart_addition(account["identity"], operation_id)
            if record is None:
                raise EzMcdError("cart_addition_not_found_for_account")
            return self._status(account, record, repeated=True)

    def _status(self, account, record, *, repeated):
        cart, query_error = None, None
        try:
            _, provider = self.service.provider(account["alias"])
            cart = self.service._cart_snapshot(account, provider, CheckoutInput.model_validate(record["request"]))
            if not cart["items"]:
                record["state"] = "EMPTY"
            else:
                try:
                    verify_cart_contents(record["draft"], cart)
                except EzMcdError:
                    record["state"] = "DIFFERENT"
                else:
                    record["state"] = "MATCHED"
                    record["ever_matched"] = True
            record["last_checked_at"] = time.time()
        except (EzMcdError, OSError, ValueError, KeyError, TypeError) as error:
            record["state"] = "UNAVAILABLE"
            query_error = error.code if isinstance(error, EzMcdError) else "cart_status_unavailable"
        self.store.update_cart_addition(account["identity"], record, matched=record["state"] == "MATCHED")
        # Current equality does not prove which client performed the write.
        # It is sufficient to quote the current cart, never to retry adding it.
        return (cart or {}) | {"account": account["alias"], "operation_id": record["operation_id"],
            "request": record["request"], "requested_items": record["selections"],
            "state": record["state"], "cart_matches_requested": record["state"] == "MATCHED",
            "write_receipt_verified": record["write_receipt_verified"], "ever_matched": record["ever_matched"],
            "created_at": record["created_at"], "last_checked_at": record["last_checked_at"],
            "write_error": record["write_error"], "query_error": query_error,
            "repeated": repeated, "add_attempted": not repeated, "automatic_write_retry": False,
            "next_action": "checkout_quote" if record["state"] == "MATCHED" else "review_cart_or_query_original_addition",
            "order_created": False, "payment_attempted": False, "simulation": account["provider"] == "demo"}
