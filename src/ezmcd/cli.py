import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from pydantic import ValidationError

from .accounts import connect_har, connect_samples, import_session, revoke_local, update_runtime, update_runtime_from_device
from .demo import setup_demo
from .errors import EzMcdError, error_result
from .models import CheckoutInput, Policy, cents, SHANGHAI
from .service import Service
from .storage import Store


def main():
    parser = argparse.ArgumentParser(prog="ezmcd", description="EzMcDonald's local account, checkout and MCP tools")
    parser.add_argument("--data-dir", default=os.environ.get("EZMCD_DATA_DIR", "~/.local/share/ezmcdonalds"))
    commands = parser.add_subparsers(dest="command", required=True)
    certificate = commands.add_parser("sampling-cert", help="Manage an explicit local macOS sampling certificate; never enables a proxy")
    certificate.add_argument("certificate_action", choices=("prepare", "install", "status", "remove"))
    status_parser = commands.add_parser("sampling-status", help="Read-only proxy/sampling diagnosis; never starts capture or changes settings")
    status_parser.add_argument("--network-service", default="Wi-Fi")
    sampling = commands.add_parser("sampling-capture", help="Suspended: legacy order-side capture is disabled; use existing sample import")
    sampling.add_argument("alias")
    sampling.add_argument("--network-service", default="Wi-Fi")
    sampling.add_argument("--seconds", type=int, default=120)
    sampling.add_argument("--mitmdump", help="Path to your installed mitmdump; otherwise uses PATH")
    sampling.add_argument("--refresh-from", help="Private recent signed balance sample for one matching native session refresh")
    sampling.add_argument("--update-account", action="store_true", help="Legacy option retained for compatibility; capture is suspended")
    sampling.add_argument("--interactive", action="store_true", help="Wait for Enter in a local terminal, then show live human-readable steps on stderr; final JSON stays on stdout")
    sampling.add_argument("--manage-app", action="store_true", help="Legacy option; order-side capture is suspended")
    first_connection = commands.add_parser("sampling-connect", help="Experimental app-scoped wallet connection/maintenance; never changes system proxy settings")
    first_connection.add_argument("alias")
    first_connection.add_argument("--binary", help="Supported official binary; maintenance defaults to the existing account's binary")
    first_connection.add_argument("--update", action="store_true", help="Update the same existing member using only the wallet; preserves policy and purchase journal")
    first_connection.add_argument("--network-service", default="Wi-Fi", help="Network service to inspect read-only; never changes proxy settings")
    first_connection.add_argument("--seconds", type=int, default=120)
    first_connection.add_argument("--mitmdump")
    first_connection.add_argument("--manage-app", action="store_true")
    sampling_import = commands.add_parser("sampling-import", help="Resume a completed local capture import; only reads upstream, never starts a proxy")
    sampling_import.add_argument("alias")
    sampling_import.add_argument("capture_directory")
    sampling_import.add_argument("--connect", action="store_true", help="Resume a first-connection balance capture; never replaces an existing account")
    sampling_import.add_argument("--update", action="store_true", help="With --connect, resume a wallet-only same-member maintenance capture")
    account = commands.add_parser("account").add_subparsers(dest="action", required=True)
    account.add_parser("list")
    account.add_parser("guide", help="Show the local credentials setup guide without asking for secrets")
    check = account.add_parser("check")
    check.add_argument("alias")
    runtime_check = account.add_parser("runtime-check", help="Check v5 runtime with one read-only request, including historical evidence; never renews it")
    runtime_check.add_argument("alias")
    refresh = account.add_parser("refresh", help="Refresh an existing session once, or resume its read-only verification")
    refresh.add_argument("alias")
    runtime = account.add_parser("runtime", help="Update official runtime evidence without replacing login or purchase records")
    runtime.add_argument("alias")
    runtime_source = runtime.add_mutually_exclusive_group(required=True)
    runtime_source.add_argument("--sample", help="Private same-session official v5 request JSON")
    runtime_source.add_argument("--har", help="Private HAR with same-session official v5 evidence; no balance entry required")
    runtime_source.add_argument("--device-from", help="Explicitly reuse recent evidence from the same official device; validates only a v5 GET, not an order")
    runtime_source.add_argument("--previous-device", action="store_true", help="Verify this member's recent saved device evidence after a session change; never renews its observation time")
    runtime_source.add_argument("--paste-headers", action="store_true", help="Paste sid, token and x-mcd-sign privately from your official request; never sends an order")
    runtime_source.add_argument("--clipboard", action="store_true", help="Read one copied cURL/raw request from macOS clipboard; never executes it")
    runtime_source.add_argument("--request-file", help="Private text file containing one copied cURL/raw request")
    login = account.add_parser("login", help="Experimental direct SMS login; phone and code stay in the local terminal")
    login.add_argument("alias", nargs="?", default="personal")
    login.add_argument("--binary", default="/Applications/McdApp.app/WrappedBundle/McdApp")
    login.add_argument("--device-from", help="Reuse only the observed device fields of a locally imported account, never its session")
    login.add_argument("--resume", help="Resume one existing attempt without sending another SMS")
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
    source.add_argument("--clipboard", action="store_true", help="Read one copied balance request from macOS clipboard; never executes it")
    source.add_argument("--request-file", help="Private text file containing one copied balance cURL/raw request")
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
    cart_status = commands.add_parser("cart-status", help="Query a recorded cart addition without sending it again; defaults to the latest")
    cart_status.add_argument("account")
    cart_status.add_argument("--operation-id")
    cart_operations = commands.add_parser("cart-operations", help="List local cart/pickup intents without network access; unresolved by default")
    cart_operations.add_argument("account")
    cart_operations.add_argument("--all", action="store_true", dest="all_operations")
    cart_operations.add_argument("--limit", type=int, default=20)
    cart_operations.add_argument("--offset", type=int, default=0)
    retire = commands.add_parser("cart-retire", help="Abandon one unresolved local intent after pickup time; never clears carts or cancels/pays orders")
    retire.add_argument("account")
    retire.add_argument("--operation-id", required=True)
    history = commands.add_parser("orders", help="Find this account's local purchase records without a login or network call")
    history.add_argument("account")
    history.add_argument("--date", dest="pickup_date", help="Pickup date YYYY-MM-DD in Asia/Shanghai")
    history.add_argument("--state", choices=("RESERVED", "CREATED", "PAYMENT_ATTEMPTED", "UNKNOWN", "PAID", "CANCELLED"))
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--offset", type=int, default=0)
    live_history = commands.add_parser("orders-live", help="Read a page of official account orders; requires a valid session")
    live_history.add_argument("account")
    live_history.add_argument("--page", type=int, default=1)
    live_history.add_argument("--limit", type=int, default=10)
    order_view = commands.add_parser("order-view", help="Read official detail/cashier; optional local comparison never binds or pays")
    order_view.add_argument("account")
    order_view.add_argument("order_id")
    order_view.add_argument("--attempt-id")
    commands.add_parser("mcp-config")
    diagnose = commands.add_parser("doctor", help="Check local setup; optionally verify existing account sessions without purchases")
    diagnose.add_argument("--account")
    diagnose.add_argument("--verify-session", action="store_true")
    diagnose.add_argument("--binary", default="/Applications/McdApp.app/WrappedBundle/McdApp")
    preferences = commands.add_parser("preferences").add_subparsers(dest="preference_action", required=True)
    preferences.add_parser("show")
    pref_save = preferences.add_parser("save")
    pref_save.add_argument("--file", required=True, help="Local JSON preferences file; does not authorize spending")
    pref_plan = preferences.add_parser("plan")
    pref_plan.add_argument("--account")
    pref_plan.add_argument("--date")
    args = parser.parse_args()
    if args.command == "sampling-import" and args.update and not args.connect:
        parser.error("sampling-import --update requires --connect")
    try:
        store = Store(Path(args.data_dir))
        service = Service(store)
        if args.command == "sampling-cert":
            from .sampling_cert import SamplingCertificate
            result = getattr(SamplingCertificate(store), args.certificate_action)()
        elif args.command == "sampling-status":
            from .sampling_status import sampling_status
            result = sampling_status(args.network_service)
        elif args.command == "sampling-connect":
            from .sampling_connect import AccountCapture, import_connection, require_new_alias
            from .sampling_console import SamplingConsole
            if args.update:
                from .sampling_maintenance import AccountMaintenanceCapture, import_maintenance, maintenance_account
                record = maintenance_account(store, args.alias)
                capture_class, importer = AccountMaintenanceCapture, import_maintenance
                binary = args.binary or record["binary"]
            else:
                require_new_alias(store, args.alias)
                capture_class, importer = AccountCapture, import_connection
                binary = args.binary or "/Applications/McdApp.app/WrappedBundle/McdApp"
            console = SamplingConsole(manage_app=args.manage_app, balance_only=True, update_account=args.update)
            capture = capture_class(store, args.alias, binary=binary, service=args.network_service,
                seconds=args.seconds, mitmdump=args.mitmdump, report=console, manage_app=args.manage_app)
            console.ready(args.alias, args.seconds)
            result = capture.run()
            if result.get("sample_import_ready") is True:
                try:
                    result |= importer(store, args.alias, result["capture_directory"])
                except (EzMcdError, OSError, ValueError, KeyError, TypeError) as error:
                    result.update(ok=False, account_created=False, account_updated=False,
                        import_error=error.code if isinstance(error, EzMcdError) else "invalid_local_configuration_or_response",
                        resume_command=[sys.executable, "-m", "ezmcd.cli", "--data-dir", str(store.root),
                            "sampling-import", args.alias, result["capture_directory"], "--connect"] + (["--update"] if args.update else []),
                        next_action="采样已结束，但本轮导入尚未完成。保留本目录，解决错误后可用 resume_command 只读重试；不会重新启动代理。")
            console.finished(result)
        elif args.command == "sampling-capture":
            raise EzMcdError("sampling_runtime_capture_suspended")
        elif args.command == "sampling-import":
            if args.update:
                from .sampling_maintenance import import_maintenance
                result = import_maintenance(store, args.alias, args.capture_directory)
            elif args.connect:
                from .sampling_connect import import_connection
                result = import_connection(store, args.alias, args.capture_directory)
            else:
                from .sampling_import import import_capture
                result = import_capture(store, args.alias, args.capture_directory)
        elif args.command == "mcp-config":
            result = {"mcpServers": {"ezmcdonalds": {"command": sys.executable,
                "args": ["-m", "ezmcd.mcp_server"], "env": {"EZMCD_DATA_DIR": str(store.root)}}}}
        elif args.command == "doctor":
            from .diagnostics import doctor
            result = doctor(service, args.account, verify_session=args.verify_session, binary=args.binary)
        elif args.command == "preferences":
            from .preferences import MealPreferences, get_preferences, save_preferences, plan_meal
            from .storage import read_private
            if args.preference_action == "show":
                result = get_preferences(store)
            elif args.preference_action == "save":
                result = save_preferences(store, MealPreferences.model_validate(read_private(Path(args.file).expanduser())))
            else:
                result = plan_meal(service, args.account, args.date)
        elif args.command == "account":
            if args.action == "list":
                result = {"accounts": service.accounts()}
            elif args.action == "guide":
                from .diagnostics import credentials_guide
                result = credentials_guide()
            elif args.action == "check":
                result = service.check_account(args.alias)
            elif args.action == "runtime-check":
                from .runtime_check import check_runtime
                result = check_runtime(store, args.alias)
            elif args.action == "refresh":
                result = service.refresh_account(args.alias)
            elif args.action == "runtime":
                if args.clipboard or args.request_file:
                    from .request_import import read_request_file, read_request_clipboard
                    from .accounts import update_runtime_request_text
                    text = read_request_clipboard() if args.clipboard else read_request_file(args.request_file)
                    try:
                        result = update_runtime_request_text(store, args.alias, text)
                    finally:
                        text = None
                    if args.clipboard:
                        result["clipboard_clear_reminder"] = "请在本机清理复制的请求及剪贴板历史；项目没有修改剪贴板。"
                elif args.paste_headers:
                    from .accounts import interactive_runtime
                    result = interactive_runtime(store, args.alias)
                elif args.previous_device:
                    from .runtime_recovery import restore_runtime
                    result = restore_runtime(store, args.alias)
                else:
                    result = (update_runtime_from_device(store, args.alias, args.device_from) if args.device_from
                              else update_runtime(store, args.alias, args.har or args.sample, har=bool(args.har)))
            elif args.action == "login":
                from .sms import interactive_login
                result = interactive_login(store, args.alias, args.binary, args.device_from, args.resume)
            elif args.action == "import":
                result = import_session(store, args.alias, args.profile, args.binary, args.runtime_sample,
                                        args.phone_label, update=args.update)
            elif args.action == "connect":
                if args.clipboard or args.request_file:
                    if args.runtime_sample:
                        raise EzMcdError("import_runtime_separately_after_request_connection")
                    from .request_import import read_request_file, read_request_clipboard
                    from .accounts import connect_request_text
                    text = read_request_clipboard() if args.clipboard else read_request_file(args.request_file)
                    try:
                        result = connect_request_text(store, args.alias, text, args.binary, args.phone_label, update=args.update)
                    finally:
                        text = None
                    if args.clipboard:
                        result["clipboard_clear_reminder"] = "请在本机清理复制的请求及剪贴板历史；项目没有修改剪贴板。"
                elif args.har:
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
        elif args.command == "orders":
            result = service.orders(args.account, pickup_date=args.pickup_date, state=args.state, limit=args.limit, offset=args.offset)
        elif args.command == "orders-live":
            result = service.official_orders(args.account, args.page, args.limit)
        elif args.command == "order-view":
            result = service.official_order(args.account, args.order_id, args.attempt_id)
        elif args.command == "cart-status":
            result = service.cart_add_status(args.account, args.operation_id)
        elif args.command == "cart-operations":
            result = service.cart_operations(args.account, unresolved_only=not args.all_operations, limit=args.limit, offset=args.offset)
        elif args.command == "cart-retire":
            result = service.retire_cart_operation(args.account, args.operation_id)
        else:
            result = service.status(args.account, args.attempt_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "sampling-capture" and not (result.get("runtime_saved") and result.get("balance_saved")):
            return 2
        if args.command == "sampling-connect" and not (result.get("account_created") or result.get("account_updated") or result.get("already_imported")):
            return 2
        if args.command == "account" and args.action == "login" and result.get("state") != "CONNECTED":
            return 2
    except EzMcdError as error:
        print(json.dumps(error_result(error.code), ensure_ascii=False), file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print('{"ok":false,"error":"local_login_input_cancelled","automatic_write_retry":false}', file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        print('{"ok":false,"error":"invalid_local_configuration_or_response","automatic_write_retry":false}', file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
