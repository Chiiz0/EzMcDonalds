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
        "Use a stable idempotency key per user purchase. "
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
            "meal_preferences": "local saved choices and read-only live meal_plan; no purchase authorization or scheduling",
            "membership_assets": "card validity and coupon category counts; actual eligibility from live menu and quote",
            "products": "ordinary items and combos; fresh product_detail and cart_add_configured support standard component choices",
            "combo_configuration": "explicit choices for each round; advanced modifications use the official cart",
            "payment": "McWallet, requires local spending policy", "runtime_ttl_hours": 24,
            "runtime_ttl_source": "local_adapter_limit_not_verified_server_expiry",
            "real_e2e_evidence": "one authorized account/store/breakfast combo; generalized purchase paths need acceptance",
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
                      "Export only the authorized official balance exchange to a private local HAR or sample file.",
                      "Run ezmcd account connect --har locally; same-session v5 evidence is additionally required for checkout.",
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
    def preferences_get() -> dict:
        """Read locally saved store, meal, component, pickup-time and budget preferences. These do not authorize spending or enable a schedule."""
        return get_preferences(service.store)

    @server.tool(annotations=LOCAL)
    @protected
    def preferences_save(preferences: MealPreferences) -> dict:
        """Save the user's requested meal preferences locally. Does not change account spending policy, create a schedule, modify a cart, or buy anything."""
        return save_preferences(service.store, preferences)

    @server.tool(annotations=READ)
    @protected
    def meal_plan(account: str | None = None, date: str | None = None) -> dict:
        """Resolve saved preferences against live pickup slots, menu/card offers and combo choices. Returns a draft selection, not an order. Never silently substitutes unavailable items or times; date is YYYY-MM-DD in Shanghai."""
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
    def product_detail(request: CheckoutInput, product_code: str, offer_id: str | None = None) -> dict:
        """Read live combo rounds, component choices, extra prices and supported standard configurations. No cart/order mutation."""
        _, provider = service.provider(request.account)
        return provider.product_detail(request, product_code, offer_id)

    @server.tool(annotations=READ)
    @protected
    def cart_get(request: CheckoutInput) -> dict:
        """Inspect exact products, quantities and combo choices currently selected in the official cart."""
        _, provider = service.provider(request.account)
        return provider.cart(request)

    @server.tool(annotations=CART)
    @protected
    def cart_add_simple(request: CheckoutInput, items: list[ItemSelection], idempotency_key: str) -> dict:
        """Add simple uncustomized menu items to an empty cart once. No order/payment; complex combos require official App configuration."""
        return service.add_simple_items(request, items, idempotency_key)

    @server.tool(annotations=CART)
    @protected
    def cart_add_configured(request: CheckoutInput, items: list[ItemSelection], idempotency_key: str) -> dict:
        """Add selected items/combos to an EMPTY cart once. Use live offer_id and explicit round/items choices from product_detail. Standard modifiers only. Does not order or pay; call checkout_quote next."""
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

    return server


def main():
    root = Path(os.environ.get("EZMCD_DATA_DIR", "~/.local/share/ezmcdonalds")).expanduser()
    build_server(Service(Store(root))).run(transport="stdio")


if __name__ == "__main__":
    main()
