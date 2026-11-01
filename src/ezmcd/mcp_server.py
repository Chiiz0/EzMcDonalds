"""Private stdio MCP server. Credentials and spending policy are local-only."""
from functools import wraps
import os
from pathlib import Path

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .errors import EzMcdError, error_result
from .models import CheckoutInput, ItemSelection, StoreSearch
from .preferences import MealPreferences, get_preferences, save_preferences, plan_meal
from .service import Service
from .storage import Store

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
CART = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
PAY = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)
AUTH = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def protected(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except EzMcdError as error:
            return error_result(error.code)
        except (OSError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "operation_stopped_check_local_account_or_status", "automatic_write_retry": False}
    return call


def build_server(service):
    server = MCPServer("EzMcDonald's", version=__version__, log_level="WARNING",
        instructions="Use account aliases; never request credentials in chat. Read capabilities first. "
        "Use account_diagnose for setup problems; stores_search accepts city names. Saved preferences do not authorize spending. "
        "meal_plan only drafts selections. Inspect checkout_blockers and a fresh quote before checkout_submit. "
        "To change a nonempty cart, inspect cart_get and use its short-lived cart_ref with cart_clear only when the user wants those items removed. "
        "Use coupons_list and coupon_products for explicit coupons; preserve coupon_ref in detail and cart calls. Never silently replace an unavailable coupon with regular price. "
        "Use a stable idempotency key per user purchase. "
        "Use orders_list to recover local purchase attempts after a restart or lost context; it is not the official account's full order history. "
        "After any ambiguous result query order_status; never create a replacement to retry payment. "
        "simulation=true means an offline demo, never a real order.")

    @server.tool(annotations=READ)
    def capabilities() -> dict:
        """Report implemented features and explicitly unverified or unsupported flows."""
        return {"version": __version__, "transport": "stdio", "market": "China mainland", "fulfillment": ["locker-in"],
            "accounts": "isolated official-session imports and local SMS login", "sms_login": True,
            "sms_login_scope": "local_terminal_existing_member_requires_observed_official_device_context",
            "sms_login_via_mcp": False, "session_auto_refresh": False,
            "session_refresh": "explicit account_refresh; local 24h rotation interval, no ambiguous POST retries",
            "stores": "city/name search or explicit coordinates, favorites, live pickup slots; compatible locker pickup required",
            "meal_preferences": "local saved products, components and exact-name free ingredient choices resolved against live detail; no purchase authorization or scheduling",
            "membership_assets": "card validity and coupon category counts; coupons_list queries current store/time availability and coupon_products lists matching products",
            "products": "ordinary items and combos; product_detail also shows specification variants and matching live menu offers",
            "combo_configuration": "explicit round choices; supported free toggles and single choices such as ice; inactive sold-out groups omitted, paid or unknown modifications use official cart",
            "coupon_configuration": "fresh references scoped to account/store/time; one ordinary coffee coupon with less ice passed real cart and CNY9.90 quote checks; ordinary coupon payment unverified",
            "cart_changes": "cart_get/cart_add return a 120-second member/store/time-bound reference; explicit cart_clear rechecks the complete cart and sends once; repeats only query; real clear and repeat verified, no cross-client atomic lock",
            "runtime_maintenance": "local account runtime import, or explicit same-device-context v5 GET verification; preserves observation time; one reused-context order POST/payment accepted, cross-day validity unverified",
            "runtime_diagnostics": "account_runtime_check can inspect historical runtime on one fixed GET; never renews observation or authorizes checkout",
            "local_guarded_capture": "Mac CLI sampling-capture only; requires local certificate and manual official App operation; blocks order requests before upstream, restores proxy after App closes; does not import automatically or enable spending; real no-order sampling verified",
            "local_request_import": "CLI only: copy one signed balance request as cURL/raw HTTP, then connect --clipboard on Mac or --request-file; validates signature and live balance, never executes copied commands; runtime supports the same text sources and discards the order body",
            "payment": "McWallet, requires local spending policy", "runtime_ttl_hours": 24,
            "runtime_ttl_source": "local_adapter_limit_not_verified_server_expiry",
            "real_e2e_evidence": "public core through stdio MCP: one SMS-authorized account, one store, breakfast-card combo, CNY12.60 McWallet payment; duplicate after restart returned the same paid order without new writes; other meal/store scenarios unverified",
            "payment_settlement": "successful consume followed by at most four read-only status checks, with 1/2/4 second delays; UNKNOWN is query-only, never a reason to repeat payment",
            "order_recovery": "orders_list finds local attempts by account, pickup date and state; works offline after session expiry/revocation; order_status queries the original order live",
            "automatic_retries": False, "automatic_schedule": False}

    @server.tool(annotations=READ)
    def account_connection_instructions() -> dict:
        """Explain local enrollment; do not paste session files, SMS codes, or tokens into chat."""
        from .diagnostics import credentials_guide
        return {"method": "official_device_bootstrap_then_local_sms_or_session_import", "sms_login_implemented": True,
            "sms_login": {"entry": "ezmcd account login NEW_ALIAS --device-from EXISTING_ALIAS",
                "requires": "user-owned official device context from a locally connected account",
                "input": "phone and SMS code in local terminal only, never chat or MCP",
                "checkout_runtime": "not supplied by SMS login"},
            "steps": ["User logs into the official App with their own phone number.",
                      "Copy only your own signed balance request as cURL/raw HTTP for local import, or export the authorized exchange to a private HAR/sample file.",
                      "Run ezmcd account connect ALIAS --clipboard on Mac, --request-file for a private copied request, or --har locally; never send copied requests through MCP/chat. Maintain official v5 evidence with account runtime, including explicit same-device-context read-only verification where applicable.",
                      "Use account_check; set spending limits locally only when checkout is wanted."],
            "documentation": "docs/onboarding.md", "phone_label_is_not_authentication": True,
            "local_guide": credentials_guide()}

    @server.tool(annotations=READ)
    @protected
    def account_diagnose(account: str, verify_session: bool = True) -> dict:
        """Explain client, session, device and checkout readiness with next steps. Optional live check only reads wallet balance; never refreshes, logs in, or purchases."""
        from .diagnostics import doctor
        return doctor(service, account, verify_session=verify_session)

    @server.tool(annotations=READ)
    @protected
    def account_runtime_check(account: str) -> dict:
        """Check this account's observed v5 runtime with one fixed order-list GET. Can inspect historical evidence, but does not renew it, change account files, authorize checkout, or prove order POST acceptance."""
        from .runtime_check import check_runtime
        return check_runtime(service.store, account)

    @server.tool(annotations=READ)
    @protected
    def preferences_get() -> dict:
        """Read locally saved store, meal, component, ingredient, pickup-time and budget preferences. These do not authorize spending or enable a schedule."""
        return get_preferences(service.store)

    @server.tool(annotations=LOCAL)
    @protected
    def preferences_save(preferences: MealPreferences) -> dict:
        """Save the user's requested meal preferences locally. Does not change account spending policy, create a schedule, modify a cart, or buy anything."""
        return save_preferences(service.store, preferences)

    @server.tool(annotations=READ)
    @protected
    def meal_plan(account: str | None = None, date: str | None = None) -> dict:
        """Resolve saved products, named coupons and ingredient preferences against live pickup slots and choices. Returns a draft, not an order. Never silently substitutes unavailable coupons, items or times; date is YYYY-MM-DD in Shanghai."""
        return plan_meal(service, account, date)

    @server.tool(annotations=READ)
    @protected
    def accounts_list() -> dict:
        """List connected aliases and local spending limits without exposing credentials."""
        return {"accounts": service.accounts()}

    @server.tool(annotations=READ)
    @protected
    def account_check(account: str) -> dict:
        """Verify the account session now and return wallet balance; never pays."""
        return service.check_account(account)

    @server.tool(annotations=AUTH)
    @protected
    def account_refresh(account: str) -> dict:
        """Maintain an existing session without SMS or purchases. May rotate credentials. Repeated calls within 24h only verify; an unknown refresh outcome requires local login. Does not renew checkout runtime."""
        return service.refresh_account(account)

    @server.tool(annotations=READ)
    @protected
    def membership_assets(account: str) -> dict:
        """Read membership cards and coupon counts, without card numbers or credentials. Actual discount eligibility comes from the live store/time menu and checkout_quote."""
        _, provider = service.provider(account)
        return provider.assets()

    @server.tool(annotations=READ)
    @protected
    def coupons_list(request: CheckoutInput, keyword: str = "") -> dict:
        """Read this account's coupons for the selected pickup store/time, with availability and restrictions. Coupon references are scoped to this account, store and time; listing does not claim or use a coupon."""
        _, provider = service.provider(request.account)
        return provider.coupons(request, keyword)

    @server.tool(annotations=READ)
    @protected
    def coupon_products(request: CheckoutInput, coupon_ref: str) -> dict:
        """Recheck an available coupon and list its matching products/prices. Pass coupon_ref and offer_id to product_detail and cart_add_configured. Card benefits use existing menu card offers; special bundles require official configuration."""
        _, provider = service.provider(request.account)
        return provider.coupon_products(request, coupon_ref)

    @server.tool(annotations=READ)
    @protected
    def stores_favorites(account: str) -> dict:
        """Read this account's saved pickup restaurants."""
        _, provider = service.provider(account)
        return {"stores": provider.favorites()}

    @server.tool(annotations=READ)
    @protected
    def cities_list(account: str, keyword: str = "") -> dict:
        """Find city names/codes for pickup restaurant search. Does not obtain the user's location."""
        _, provider = service.provider(account)
        return provider.cities(keyword)

    @server.tool(annotations=READ)
    @protected
    def stores_search(request: StoreSearch) -> dict:
        """Find pickup restaurants by city name/code and keyword, OR explicit coordinates. Paginated results include address and hours; check pickup_slots for the chosen store."""
        _, provider = service.provider(request.account)
        return provider.search_stores(request)

    @server.tool(annotations=READ)
    @protected
    def store_get(account: str, store_code: str) -> dict:
        """Read a seven-digit restaurant code; availability is checked again for pickup time."""
        _, provider = service.provider(account)
        return {"store": provider.store(store_code)}

    @server.tool(annotations=READ)
    @protected
    def pickup_slots(account: str, store_code: str) -> dict:
        """Get currently available pickup dates, times, and meal periods for a store."""
        _, provider = service.provider(account)
        return provider.pickup_slots(store_code)

    @server.tool(annotations=READ)
    @protected
    def menu_list(request: CheckoutInput, keyword: str = "") -> dict:
        """Browse a live store/time menu, including card offers. Displayed prices are not final quotes."""
        _, provider = service.provider(request.account)
        return provider.menu(request, keyword)

    @server.tool(annotations=READ)
    @protected
    def product_detail(request: CheckoutInput, product_code: str, offer_id: str | None = None, coupon_ref: str | None = None) -> dict:
        """Read live combo rounds, components, prices, free keep/remove modifiers and specification variants. Follow a variant's live menu_offers to its detail; modifiers use codes, not arbitrary request blobs. No cart/order mutation."""
        _, provider = service.provider(request.account)
        return provider.product_detail(request, product_code, offer_id, coupon_ref)

    @server.tool(annotations=READ)
    @protected
    def cart_get(request: CheckoutInput) -> dict:
        """Inspect products, quantities and choices in the official cart. Returns a 120-second account/store/time-bound cart_ref for an explicit clear. Does not mutate the official cart."""
        return service.cart(request)

    @server.tool(annotations=PAY)
    @protected
    def cart_clear(request: CheckoutInput, cart_ref: str, idempotency_key: str) -> dict:
        """Remove ALL items from the inspected cart only when the user wants to change/remove them. Requires a fresh cart_get/cart_add cart_ref and stable key; rechecks the complete cart before one PUT. Repeated calls only query. Never cancels orders, consumes coupons or pays. Do not edit concurrently in the official App."""
        return service.clear_cart(request, cart_ref, idempotency_key)

    @server.tool(annotations=CART)
    @protected
    def cart_add_simple(request: CheckoutInput, items: list[ItemSelection], idempotency_key: str) -> dict:
        """Add simple uncustomized menu items to an empty cart once. No order/payment; complex combos require official App configuration."""
        return service.add_simple_items(request, items, idempotency_key)

    @server.tool(annotations=CART)
    @protected
    def cart_add_configured(request: CheckoutInput, items: list[ItemSelection], idempotency_key: str) -> dict:
        """Add selected items/combos to an EMPTY cart once. Use live offer_id, optional coupon_ref, and explicit round/items choices from product_detail. Rechecks coupon availability and verifies its cart echo. Does not order or pay; call checkout_quote next."""
        return service.add_configured_items(request, items, idempotency_key)

    @server.tool(annotations=READ)
    @protected
    def checkout_quote(request: CheckoutInput) -> dict:
        """Validate the current cart, components, discounts, locker/time, wallet and price cap; create a 120-second quote."""
        return service.quote(request)

    @server.tool(annotations=PAY)
    @protected
    def checkout_submit(account: str, quote_id: str, idempotency_key: str) -> dict:
        """REAL PURCHASE: create and McWallet-pay one fresh quoted order within the locally authorized policy. Repeated calls only reconcile."""
        return service.submit(account, quote_id, idempotency_key)

    @server.tool(annotations=PAY)
    @protected
    def order_pay(account: str, attempt_id: str) -> dict:
        """Pay an existing locally CREATED order only if payment has never been attempted. All other states only query."""
        return service.pay_existing(account, attempt_id)

    @server.tool(annotations=READ)
    @protected
    def order_status(account: str, attempt_id: str) -> dict:
        """Reconcile the exact order, cashier and wallet. Never resubmits or consumes; cancellation overrides historical PAID."""
        return service.status(account, attempt_id)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    @protected
    def orders_list(account: str, pickup_date: str | None = None, state: str | None = None,
                    limit: int = 20, offset: int = 0) -> dict:
        """Recover LOCAL purchase attempts after lost context. No network or valid login needed. Optional YYYY-MM-DD pickup date (Asia/Shanghai) and exact state; limit 1..100, offset 0..10000. Newest pickup date then recorded order first. Includes unknown/cancelled attempts; missing order_id requires official review. Snapshots may be stale: query order_status for live confirmation. Does not include purchases made only in the official App."""
        return service.orders(account, pickup_date=pickup_date, state=state, limit=limit, offset=offset)

    return server


def main():
    root = Path(os.environ.get("EZMCD_DATA_DIR", "~/.local/share/ezmcdonalds")).expanduser()
    build_server(Service(Store(root))).run(transport="stdio")


if __name__ == "__main__":
    main()
