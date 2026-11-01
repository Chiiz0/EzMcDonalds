"""Local review and explicit retirement of obsolete, unresolved cart intents.

Retirement says nothing about the upstream outcome. It only stops an expired
meal intent blocking a new one; its write claim and every purchase record stay.
"""
from datetime import datetime, timezone
import re

from .errors import EzMcdError
from .models import pickup_time


def public_operation(row, now):
    record = row["record"]
    return {"operation_id": record["operation_id"], "kind": row["kind"],
        "request": record["request"], "requested_items": record.get("selections"),
        "state": record["state"], "previous_state": record.get("previous_state"),
        "created_at": record["created_at"], "retired_at": record.get("retired_at"),
        "unresolved": not row["closed"],
        "retirement_available": not row["closed"] and pickup_time(record["request"]["pickup_at"]) <= now}


def retired_status(account, record, *, addition):
    result = {"account": account["alias"], "operation_id": record["operation_id"],
        "request": record["request"], "state": "RETIRED", "previous_state": record["previous_state"],
        "retired_at": record["retired_at"], "local_only": True, "official_cart_checked": False,
        "retirement_confirms_upstream_outcome": False, "repeated": True, "automatic_write_retry": False,
        "write_error": record.get("write_error"), "order_created": False, "payment_attempted": False,
        "next_action": "review_current_cart_for_new_meal", "simulation": account["provider"] == "demo"}
    if addition:
        result.update(requested_items=record["selections"], add_attempted=False, cart_matches_requested=False,
            write_receipt_verified=record["write_receipt_verified"], ever_matched=record["ever_matched"],
            created_at=record["created_at"], last_checked_at=record["last_checked_at"], query_error=None)
    else:
        result.update(switch_attempted=False, error=None)
    return result


class CartRecovery:
    def __init__(self, store):
        self.store = store

    def list(self, alias, *, unresolved_only=True, limit=20, offset=0):
        if (type(unresolved_only) is not bool or type(limit) is not int or not 1 <= limit <= 100
                or type(offset) is not int or not 0 <= offset <= 10000):
            raise EzMcdError("invalid_cart_operations_filter")
        account = self.store.account(alias)
        with self.store.lock(account["identity"]):
            rows, total = self.store.cart_operations(account["identity"], unresolved_only, limit, offset)
            now = datetime.now(timezone.utc)
            return {"account": alias, "operations": [public_operation(row, now) for row in rows],
                "total": total, "limit": limit, "offset": offset,
                "next_offset": offset + len(rows) if offset + len(rows) < total else None,
                "unresolved_only": unresolved_only, "local_only": True, "official_cart_checked": False,
                "order_created": False, "payment_attempted": False, "simulation": account["provider"] == "demo"}

    def retire(self, alias, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r"(?:cart|pickup)-[0-9a-f]{64}", operation_id):
            raise EzMcdError("invalid_cart_recovery_reference")
        account = self.store.account(alias)
        with self.store.lock(account["identity"]):
            row = self.store.cart_operation(account["identity"], operation_id)
            if row is None:
                raise EzMcdError("cart_operation_not_found_for_account")
            record = row["record"]
            repeated = record.get("retired_at") is not None
            if not repeated:
                if row["closed"]:
                    raise EzMcdError("cart_operation_already_resolved")
                now = datetime.now(timezone.utc)
                if pickup_time(record["request"]["pickup_at"]) > now:
                    raise EzMcdError("cart_operation_pickup_time_not_passed")
                record = record | {"state": "RETIRED", "previous_state": record["state"], "retired_at": now.timestamp()}
                self.store.retire_cart_operation(account["identity"], row["kind"], record)
            return retired_status(account, record, addition=row["kind"] == "addition") | {
                "kind": row["kind"], "repeated": repeated, "retired": True,
                "official_cart_changed": False, "financial_records_changed": False}
