import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from pydantic import ValidationError

from .accounts import connect_har, connect_samples, import_session, revoke_local
from .demo import setup_demo
from .errors import EzMcdError
from .models import CheckoutInput, Policy, cents, SHANGHAI
from .service import Service
from .storage import Store


def main():
    parser = argparse.ArgumentParser(prog="ezmcd", description="EzMcDonald's local account, checkout and MCP tools")
    parser.add_argument("--data-dir", default=os.environ.get("EZMCD_DATA_DIR", "~/.local/share/ezmcdonalds"))
    commands = parser.add_subparsers(dest="command", required=True)
    account = commands.add_parser("account").add_subparsers(dest="action", required=True)
    account.add_parser("list")
    check = account.add_parser("check")
    check.add_argument("alias")
    connect = account.add_parser("import")
    connect.add_argument("alias")
    connect.add_argument("--profile", required=True, help="Local validated session JSON; never paste into chat")
    connect.add_argument("--runtime-sample", help="Local same-session v5 sample; optional for read-only use")
    connect.add_argument("--binary", default="/Applications/McdApp.app/WrappedBundle/McdApp")
    connect.add_argument("--phone-label", help="Optional display label; not authentication; stored masked")
    connect.add_argument("--update", action="store_true", help="Rotate same-account session without resetting policy or journal")
    capture = account.add_parser("connect", help="Validate your local balance sample and check the session live")
    capture.add_argument("alias")
    source = capture.add_mutually_exclusive_group(required=True)
    source.add_argument("--balance-sample")
    source.add_argument("--har", help="Private HAR export containing this official session's balance exchange")
    capture.add_argument("--runtime-sample")
    capture.add_argument("--binary", default="/Applications/McdApp.app/WrappedBundle/McdApp")
    capture.add_argument("--phone-label")
    capture.add_argument("--update", action="store_true")
    revoke = account.add_parser("revoke", help="Remove local account credentials and disable checkout; preserve the order journal")
    revoke.add_argument("alias")
    policy = account.add_parser("policy")
    policy.add_argument("alias")
    mode = policy.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enable-checkout", action="store_true")
    mode.add_argument("--disable-checkout", action="store_true")
    policy.add_argument("--max-order", default="20.00")
    policy.add_argument("--daily-budget", default="20.00")
    policy.add_argument("--max-orders-day", type=int, default=1)
    policy.add_argument("--store", action="append", default=[])
    policy.add_argument("--expires-at")
    demo = commands.add_parser("demo")
    demo.add_argument("--alias", default="demo")
    demo.add_argument("--run", action="store_true", help="Run simulated quote/order/payment; never contacts McDonald's")
    quote = commands.add_parser("quote")
    quote.add_argument("account")
    quote.add_argument("--store", required=True)
    quote.add_argument("--pickup-at", required=True)
    quote.add_argument("--max-amount", required=True)
    submit = commands.add_parser("checkout")
    submit.add_argument("account")
    submit.add_argument("--quote-id", required=True)
    submit.add_argument("--idempotency-key", required=True)
    status = commands.add_parser("status")
    status.add_argument("account")
    status.add_argument("attempt_id")
    commands.add_parser("mcp-config")
    args = parser.parse_args()
    try:
        store = Store(Path(args.data_dir))
        service = Service(store)
        if args.command == "mcp-config":
            result = {"mcpServers": {"ezmcdonalds": {"command": sys.executable,
                "args": ["-m", "ezmcd.mcp_server"], "env": {"EZMCD_DATA_DIR": str(store.root)}}}}
        elif args.command == "account":
            if args.action == "list":
                result = {"accounts": service.accounts()}
            elif args.action == "check":
                result = service.check_account(args.alias)
            elif args.action == "import":
                result = import_session(store, args.alias, args.profile, args.binary, args.runtime_sample,
                                        args.phone_label, update=args.update)
            elif args.action == "connect":
                if args.har:
                    if args.runtime_sample:
                        raise EzMcdError("har_runtime_sample_is_selected_from_same_archive")
                    result = connect_har(store, args.alias, args.har, args.binary, args.phone_label, update=args.update)
                else:
                    result = connect_samples(store, args.alias, args.balance_sample, args.binary, args.runtime_sample,
                                             args.phone_label, update=args.update)
            elif args.action == "revoke":
                result = revoke_local(store, args.alias)
            else:
                result = service.set_policy(args.alias, Policy(allow_checkout=args.enable_checkout,
                    max_order_cents=cents(args.max_order), daily_budget_cents=cents(args.daily_budget),
                    max_orders_per_day=args.max_orders_day, allowed_stores=args.store, expires_at=args.expires_at))
        elif args.command == "demo":
            try:
                record = store.account(args.alias)
                if record["provider"] != "demo":
                    raise EzMcdError("demo_alias_is_a_real_account")
                result = {"account": args.alias, "simulation": True}
            except EzMcdError as error:
                if error.code != "account_not_connected":
                    raise
                result = setup_demo(store, args.alias)
            if args.run:
                pickup = (datetime.now(SHANGHAI) + timedelta(days=1)).replace(hour=10, minute=15, second=0, microsecond=0)
                q = service.quote(CheckoutInput(account=args.alias, store_code="9000001", pickup_at=pickup.isoformat(), max_amount_cny="20.00"))
                first = service.submit(args.alias, q["quote_id"], "demo-first-purchase")
                repeated = service.submit(args.alias, q["quote_id"], "demo-first-purchase")
                result = {"simulation": True, "first": first, "repeated": repeated,
                          "same_order": first["order_id"] == repeated["order_id"], "real_payment": False}
        elif args.command == "quote":
            result = service.quote(CheckoutInput(account=args.account, store_code=args.store,
                pickup_at=args.pickup_at, max_amount_cny=args.max_amount))
        elif args.command == "checkout":
            result = service.submit(args.account, args.quote_id, args.idempotency_key)
        else:
            result = service.status(args.account, args.attempt_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except EzMcdError as error:
        print(json.dumps({"ok": False, "error": error.code, "automatic_write_retry": False}), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        print('{"ok":false,"error":"invalid_local_configuration_or_response","automatic_write_retry":false}', file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
