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
        "After a login/session change, runtime_recovery_available allows account_runtime_restore to verify this member's saved recent device evidence without new sampling; it never extends observation age or spending permission. "
        "meal_plan first recovers a local purchase at the same account/store/pickup time; existing_purchase=true means review existing_orders and query the original status, not add a new cart or generate a replacement key. It works offline for that recovery and does not refresh official status. Otherwise it only drafts selections. Inspect checkout_blockers and a fresh quote before checkout_submit. "
        "required_card_offer_unavailable means the named product is listed without the requested card offer. Read card_diagnostics as current store/time API reports, not verified product/card links or a promise about tomorrow. Do not fall back to ordinary prices, alter preferences, or retry automatically. "
        "Order status labels come from the explicit orderStatus field; the detail headline may be a pickup code. official_order_state=COMPLETED is the API's order state, not proof that the user collected the food or that a wallet payment was reconciled. Read status_description or official_status_detail for current pickup instructions. collection_verified=false; review_completed_order means review those instructions, not buy again. "
        "With a nonempty cart, pickup_methods reports the initial confirmation's fulfillment options. Locker-in and take-in-store are implemented. "
        "If the requested method is not selected, use checkout_quote_pickup with a fresh cart_ref and stable key; its READY result is the quote to review and submit. "
        "The selection belongs to that quote: a new initial GET may still show a default. Recover lost preparation replies with checkout_quote_pickup_status; never resend an unresolved switch under a new key. "
        "To change a nonempty cart, inspect cart_get and use its short-lived cart_ref with cart_clear only when the user wants those items removed. "
        "After cart addition, only MATCHED suggests a fresh quote. On a lost reply or restart use cart_add_status; never switch keys to repeat an unresolved addition. "
        "Use cart_operations_list to find unresolved local cart intents. If the user abandons a meal whose pickup time has passed, cart_operation_retire closes only that local intent, retains its write claim and leaves official carts and purchases untouched. Inspect the new meal's cart before continuing; never treat retirement as cancellation or proof of failure. "
        "Use coupons_list and coupon_products for explicit coupons; preserve coupon_ref in detail and cart calls. For a supported coupon combo, item quantity counts coupon bundles and combo component quantities count the portions inside each bundle; a buy-one-get-one title alone never determines those quantities. Follow the live product detail. Never silently replace an unavailable coupon with regular price. "
        "For variants absent from the menu, use product_detail's validated parent_detail_selection and preserve variant_of plus the parent offer_id in cart_add_configured. A card variant needs matching live member eligibility, explicit eligible product lists and its own parent promotion binding; other parent discounts cannot be inferred. Related offers are not selected automatically. "
        "Before every financial write the service rechecks local spending authority, including after slow preparation or journal I/O. A new order also requires an unexpired quote; quote expiry alone does not revoke payment authority for an already created order. Interrupted attempts retain their claims and are not retried by changing policy or keys. "
        "A local policy may be bound to one approved quote's exact content via account policy --quote-id. Only identical fresh quotes remain authorized; changed items, customizations, discounts, price or pickup need new owner approval. MCP cannot create or broaden this grant. Repeated calls for an existing purchase only query it. "
        "Use a stable idempotency key per user purchase. "
        "Use orders_list to recover local purchase attempts after a restart or lost context; it is not the official account's full order history. "
        "Use official_orders_list and official_order_detail to inspect live account orders, including App-only orders or possible candidates after a lost create reply. Optional attempt comparison reports limited matching fields; it never establishes request origin, links records or authorizes payment. Sale/creation time is not pickup time. "
        "After any ambiguous result query order_status; never create a replacement to retry payment. "
        "simulation=true means an offline demo, never a real order.")

    @server.tool(annotations=READ)
    def capabilities() -> dict:
        """Report implemented features and explicitly unverified or unsupported flows."""
        return {"version": __version__, "transport": "stdio", "market": "China mainland", "fulfillment": ["locker-in", "take-in-store"],
            "accounts": "isolated official-session imports and local SMS login", "sms_login": True,
            "sms_login_scope": "local_terminal_existing_member_requires_observed_official_device_context",
            "sms_login_via_mcp": False, "session_auto_refresh": False,
            "session_refresh": "explicit account_refresh; local 24h rotation interval, no ambiguous POST retries",
            "stores": "city/name search or explicit coordinates, favorites, live pickup slots; available locker or counter pickup required",
            "pickup_methods": "read initial confirmation methods for a nonempty cart; report selected/available/supported separately; never change fulfillment or infer locker support from store tags",
            "pickup_quote": "explicit one-shot method switch, persisted response and 120-second selected quote; submit rechecks unchanged cart/prices using GETs without repeating the switch; no fallback to default fulfillment",
            "meal_preferences": "local saved products, named parent single/combo variants and variant_specifications labels, component_choices bind names and quantities to explicit live rounds and per-choice limits, optional component_modifiers.round disambiguates same-name items; components and exact-name supported ingredient choices resolved against live detail; estimates include explicit optional modifier fees and avoid counting default combo upcharges twice; no purchase authorization or scheduling",
            "meal_recovery": "meal_plan checks local purchases before login/network and again after live slot selection; any existing state at the same account/store/time returns the original attempt instead of a new draft, even after revocation; not the official account's full history or a live payment check",
            "membership_assets": "card validity and coupon category counts; coupons_list queries current store/time availability and coupon_products lists matching products",
            "meal_card_diagnostics": "When the named item or variant parent exists but its required card offer is missing, meal_plan reports menu prices as non-final and queries current store/time card status once. Reports daily limits and API rules without claiming product/card linkage or future eligibility; no regular-price fallback, cart change, preference change or automatic retry. Live missing breakfast-card offer verified a41",
            "products": "ordinary items and combos; variants follow their own live menu offers or explicit validated parent selections; supported card variants additionally bind fresh member eligibility, eligible leaf lists and the matching card promotion; other parent discounts are not inferred",
            "variant_evidence": "large hot milk absent from menu passed real parent-detail selection, single add, CNY23.00 quote, CNY20.00 limit rejection, restart replay and clear; no payment or automatic selection of related promotions",
            "card_variant_evidence": "coffee-card medium iced oat latte passed real MCP preference estimate, CNY9.90 quote, CNY9.00 refusal, ineligible-large refusal, restart without re-add and clear; member card uses, wallet and orders unchanged; no new payment, other card structures unverified",
            "ordinary_cart_evidence": "two hash browns, and a hash brown plus fried dough stick: real CNY17.00 quotes; another store's counter pickup passed explicit switch, CNY8.50 quote, process restart and clear; no new payment",
            "ordinary_related_promotion": "One observed unavailable member promotion targeting a different product does not prevent selecting the original ordinary combo when menu/detail/starting prices agree. No related product or promotion IDs added. Self-targeted, eligible, price-mismatched or unknown offers remain blocked. Real seven-item combo: 2 identical burgers, 3 medium fries, 2 iced coffees, CNY73 locker quote, CNY72.99 refusal, restart without re-add/switch and clear; no purchase or promotion redemption",
            "order_components": "verified against each parent product and quantity before payment; swapping components across distinct meal rows is rejected",
            "combo_configuration": "explicit round choices; identical mandatory singleton expanded rows may merge into exact quantities with source_entries, native ten-wing combo quote verified a38; unknown repeated shapes stop; supported free toggles and single choices such as ice; inactive sold-out groups omitted; observed optional replacements and free-default binary paid add-ons supported with bound fee evidence; defaults never add a paid option; other paid or unknown modifications use official cart",
            "optional_modifier_evidence": "regular cappuccino with optional oat milk passed real MCP add, CNY23.00 quote, CNY22.00 refusal and restart without re-add; unselected milk option passed CNY21.00 quote and restart; test carts cleared, no new payments; other paid shapes and real payment unverified",
            "coupon_configuration": "fresh references scoped to account/store/time; one ordinary coffee coupon with less ice passed real cart and CNY9.90 quote checks. One buy-one-get-one fries coupon combo passed real MCP planning, two component portions in one bundle, CNY13.50 quote, CNY13.49 budget refusal, restart without repeat add/switch and clear; coupon uses, wallet and orders unchanged. Ordinary coupon payment unverified; other bundle structures are not inferred from names",
            "cart_changes": "cart_get/cart_add return a 120-second member/store/time-bound reference; explicit cart_clear rechecks the complete cart and sends once; repeats only query; real clear and repeat verified, no cross-client atomic lock",
            "cart_recovery": "draft and input bound before sending; same-key calls only compare the current cart; cart_add_status recovers a specific or latest recorded addition after restart; real coffee cart verified with locally injected lost reply/readback; pre-a14 records cannot be reconstructed",
            "expired_cart_intents": "local list and explicit retirement of unresolved addition/pickup preparations only after their pickup time; retained request and write claim, no upstream calls or financial changes; old keys never replay, new meal still requires current-cart review",
            "runtime_maintenance": "local account runtime import, or explicit same-device-context v5 GET verification; preserves observation time; reused-context order POST/payment accepted, including one roughly 240-hour sample under explicit historical-use permission; continuous multi-day operation and automatic renewal unverified",
            "runtime_diagnostics": "account_runtime_check can inspect historical runtime on one fixed GET; never renews observation or authorizes checkout",
            "runtime_explicit_use": "Local CLI account runtime-use only: explicit maximum age 25..336 hours from the original observation, bound to exact account/session/device/sample. Grant checks one v5 GET; every historical order requires another body-bound GET check, single-use in the current process for at most 60 seconds. No MCP grant/extension, no spending authorization. A roughly 240-hour own sample completed one explicitly authorized pickup order and CNY12.60 McWallet payment with original observation time preserved; restart with the same key queried that order without another financial write. Other ages and continuous multi-day use remain unverified",
            "local_session_maintenance": "Experimental sampling-connect ALIAS --update --manage-app: same-member wallet capture, preserves policy/journal; no system proxy writes; native wallet maintenance verified in an isolated account copy in a37; original account/journal unchanged. CLI emits final result markers only after import verification and bounded native/TLS counters without credentials. Blocked official refresh without a valid wallet sample ends capture with official_refresh_blocked; finish cleanup then confirm the official wallet on normal networking before a new run. sampling-status reads network state only. Completed imports resume read-only; cross-day renewal unverified",
            "runtime_recovery": "retain this member's recent observation after login/session changes on the exact same device context; explicit account_runtime_restore uses one fixed v5 GET before activation, preserves original age and all purchase records; no independent device initialization or cross-day renewal",
            "local_first_connection": "Experimental Mac CLI sampling-connect: positive executable-path local capture, wallet only, no system proxy changes or fallback; bounded child lifetime and cleanup. First-connection native capture/import and new MCP account check verified in an empty isolated store a39; existing-account wallet maintenance verified a37. Existing certificate and extension reused; clean-machine installation unverified. Local extension may need OS approval; no test submissions; spending disabled",
            "local_guarded_capture": "Suspended in a35 after a system-wide proxy incident; sampling-capture fails before starting App/capture. Historical completed evidence still supports sampling-import; never submit test orders for capture. No MCP proxy/App control",
            "local_request_import": "CLI only: copy one signed balance request as cURL/raw HTTP, then connect --clipboard on Mac or --request-file; validates signature and live balance, never executes copied commands; runtime supports the same text sources and discards the order body",
            "payment": "McWallet, requires local spending policy",
            "quote_scoped_authorization": "Local CLI account policy --enable-checkout --quote-id binds the member, exact products/customizations, discounts, amount and pickup. Default expiry is at most 15 minutes; identical content/limits repeated without an explicit new expiry preserve the original deadline. Identical fresh quotes allowed; changed contents rejected before financial writes. Existing attempts cannot receive replacement grants. Offline CLI/MCP purchase and restart verified; real payment using this new scoped policy not yet verified. No MCP grant tool",
            "financial_deadlines": "Account snapshot and policy checked before reservation and before/after each order, preorder and consume write claim. Quote expiry is checked for new orders only. Interrupted claims remain query-only. Offline delay tests cover these boundaries; one roughly 240-hour runtime order/payment accepted; expiry failure cases remain offline tests", "runtime_ttl_hours": 24,
            "runtime_ttl_source": "default_local_limit_not_verified_server_expiry; explicit local historical-use limit may be up to 336 hours from observation",
            "real_e2e_evidence": "public core through stdio MCP: one SMS-authorized account, one store, breakfast-card combo, CNY12.60 McWallet payment; duplicate after restart returned the same paid order without new writes; counter pickup and other meal/store payments unverified",
            "payment_settlement": "successful consume followed by at most four read-only status checks, with 1/2/4 second delays; UNKNOWN is query-only, never a reason to repeat payment",
            "order_recovery": "orders_list finds local attempts by account, pickup date and state; works offline after session expiry/revocation; order_status queries the original order live",
            "official_order_history": "paged live account list and exact detail/cashier GETs; includes App-only orders, redacts payment references/contact fields; optional local comparison lists verified and unverified fields without binding or paying; real paging and original-order detail verified",
            "official_order_status": "Explicit orderStatus takes precedence over the detail headline, which can be a pickup code. Official completion and pickup instructions are exposed separately from wallet verification and physical collection. One external completed order and the existing project order passed live MCP reads; restarting and replaying the original checkout key only queried that order, with no new writes. collection_verified is false",
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
        """Explain client, session, device and checkout readiness with next steps. runtime_guidance shows original runtime expiry and, when locally eligible, an explicit user-choice reuse option; it never grants/extends permission or proves server acceptance. Expired policy guidance finds existing orders first. Optional live check only reads wallet balance; never refreshes, logs in, or purchases."""
        from .diagnostics import doctor
        return doctor(service, account, verify_session=verify_session)

    @server.tool(annotations=READ)
    @protected
    def account_runtime_check(account: str) -> dict:
        """Check this account's observed v5 runtime with one fixed order-list GET. Can inspect historical evidence, but does not renew it, change account files, authorize checkout, or prove order POST acceptance."""
        from .runtime_check import check_runtime
        return check_runtime(service.store, account)

    @server.tool(annotations=CART)
    @protected
    def account_runtime_restore(account: str) -> dict:
        """After this member logs in or refreshes on the same device, verify saved recent runtime with one fixed v5 order-list GET and activate it locally. Requires runtime_recovery_available; repeated successful recovery only verifies the same evidence again. Never renews observation time, generates device fields, refreshes a token, creates an order or changes spending permission. Expired/missing evidence still needs the official App."""
        from .runtime_recovery import restore_runtime
        return restore_runtime(service.store, account)

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
        """Recover an existing local purchase for the preferred account/store/time before querying live slots or drafting a meal. existing_purchase=true returns original attempts without a new cart draft, even after session expiry; query their status separately. Otherwise resolve saved items/coupons/ingredients without substitution. Missing required card offers include card_diagnostics: reported store/time availability, not proven product/card linkage or future eligibility; never choose ordinary prices automatically. date is YYYY-MM-DD in Shanghai; an explicit past date can recover its purchase."""
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
        """Recheck an available coupon and list its matching products/prices. Pass coupon_ref and offer_id to product_detail and cart_add_configured. For supported combos, distinguish bundle quantity from component portions using live detail. Card benefits use menu card offers; unrecognized bundle structures require official configuration."""
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
    def pickup_methods(request: CheckoutInput) -> dict:
        """With a nonempty cart, read initial confirmation methods and default selection. Locker-in and take-in-store are supported. This GET does not reflect a previously prepared selected quote. Never switches or pays. Empty-cart upstream rejection is not proof a method is unavailable."""
        account, provider = service.provider(request.account)
        with service.store.lock(account["identity"]):
            return provider.pickup_methods(request)

    @server.tool(annotations=READ)
    @protected
    def menu_list(request: CheckoutInput, keyword: str = "") -> dict:
        """Browse a live store/time menu, including card offers. Displayed prices are not final quotes."""
        _, provider = service.provider(request.account)
        return provider.menu(request, keyword)

    @server.tool(annotations=READ)
    @protected
    def product_detail(request: CheckoutInput, product_code: str, offer_id: str | None = None, coupon_ref: str | None = None,
                       variant_of: str | None = None) -> dict:
        """Read live components, prices, supported modifiers and variants. Follow live menu_offers or the validated parent_detail_selection, preserving variant_of and parent offer_id. A card variant needs fresh member eligibility, explicit eligible lists and its matching promotion; other discounts are not inferred. Related offers are not added automatically. No cart/order mutation."""
        _, provider = service.provider(request.account)
        return (provider.product_detail(request, product_code, offer_id, coupon_ref, variant_of=variant_of) if variant_of
                else provider.product_detail(request, product_code, offer_id, coupon_ref))

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

    @server.tool(annotations=READ)
    @protected
    def cart_add_status(account: str, operation_id: str | None = None) -> dict:
        """Recover a cart addition using its operation_id, or the latest recorded addition if omitted. Uses saved request and choices to compare the CURRENT cart; never rebuilds the draft or resends a write. MATCHED permits a fresh quote only. EMPTY, DIFFERENT and UNAVAILABLE require review; they do not authorize retrying. Local intent remains visible after login expiry/revocation. Pre-a14 write-only records cannot be reconstructed."""
        return service.cart_add_status(account, operation_id)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    @protected
    def cart_operations_list(account: str, unresolved_only: bool = True, limit: int = 20, offset: int = 0) -> dict:
        """Find local addition/pickup preparations, including old blockers hidden by a newer record. Default unresolved only; newest first, limit 1..100, offset 0..10000. No network or active login required. retirement_available means its pickup time passed, not that the upstream request failed. Excludes financial attempts and clear operations; use orders_list for purchases."""
        return service.cart_operations(account, unresolved_only=unresolved_only, limit=limit, offset=offset)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False))
    @protected
    def cart_operation_retire(account: str, operation_id: str) -> dict:
        """Explicitly abandon an unresolved LOCAL addition/pickup intent only after its pickup time passed and the user no longer wants that meal. Keeps original request, write claim and all order/payment records. Repeated calls are local-only; old keys return RETIRED without resending. Does not clear the official cart, cancel an order, refund, pay or prove upstream failure. Inspect the current cart for a new meal; never use retirement as a retry."""
        return service.retire_cart_operation(account, operation_id)

    @server.tool(annotations=CART)
    @protected
    def cart_add_simple(request: CheckoutInput, items: list[ItemSelection], idempotency_key: str) -> dict:
        """Add simple uncustomized menu items to an empty cart once. Same key only queries the original request; changed input with that key is rejected. Inspect state and only proceed to a fresh quote when MATCHED. Never orders or pays."""
        return service.add_simple_items(request, items, idempotency_key)

    @server.tool(annotations=CART)
    @protected
    def cart_add_configured(request: CheckoutInput, items: list[ItemSelection], idempotency_key: str) -> dict:
        """Add selected items/combos to an EMPTY cart once. Use live offer_id, optional coupon_ref, and explicit choices from product_detail. Save a stable key: repeated calls only reconcile the original draft. Check state: MATCHED permits a fresh quote; EMPTY/DIFFERENT/UNAVAILABLE require review, not replacement or retry. Never orders or pays."""
        return service.add_configured_items(request, items, idempotency_key)

    @server.tool(annotations=READ)
    @protected
    def checkout_quote(request: CheckoutInput) -> dict:
        """Read-only quote for an initial confirmation already selecting the requested locker/counter method. Validate cart, discounts, time, wallet and cap; create a 120-second quote. Otherwise use checkout_quote_pickup explicitly."""
        return service.quote(request)

    @server.tool(annotations=PAY)
    @protected
    def checkout_quote_pickup(request: CheckoutInput, cart_ref: str, idempotency_key: str) -> dict:
        """Select the explicitly requested locker/counter method and return its 120-second quote. Requires a fresh cart_get reference and nonempty unchanged cart. Sends one switch PUT, persists its reply; repeated key only validates the original result, never renews/resends. READY is the quote to review, then checkout_submit. Does not create or pay an order. Do not edit concurrently in the App."""
        return service.quote_pickup(request, cart_ref, idempotency_key)

    @server.tool(annotations=READ)
    @protected
    def checkout_quote_pickup_status(account: str, operation_id: str | None = None) -> dict:
        """Recover a specific or latest local pickup quote preparation. Reads current cart and initial confirmation, checks the saved switch response. Never resends or renews; a missing response cannot be reconstructed from the initial default selection. Only READY may be reviewed for checkout."""
        return service.pickup_quote_status(account, operation_id)

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
        """Reconcile the exact order, cashier and wallet. Never resubmits or consumes; cancellation overrides historical PAID. official_order_state is the official status, not proof of physical collection. Read official_status_detail; a completed order can still have pickup instructions. Previously verified payment is retained separately."""
        return service.status(account, attempt_id)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    @protected
    def orders_list(account: str, pickup_date: str | None = None, state: str | None = None,
                    limit: int = 20, offset: int = 0) -> dict:
        """Recover LOCAL purchase attempts after lost context. No network or valid login needed. Optional YYYY-MM-DD pickup date (Asia/Shanghai) and exact state; limit 1..100, offset 0..10000. Newest pickup date then recorded order first. Includes unknown/cancelled attempts; missing order_id requires official review. Snapshots may be stale: query order_status for live confirmation. Does not include purchases made only in the official App."""
        return service.orders(account, pickup_date=pickup_date, state=state, limit=limit, offset=offset)

    @server.tool(annotations=READ)
    @protected
    def official_orders_list(account: str, page: int = 1, limit: int = 10) -> dict:
        """Read a live page of this member's official orders, including orders not in the local journal. page 1..100, limit 1..20. A full page may have more; no total/completeness claim. Sale/creation time is not pickup time: use official_order_detail. Never creates, pays, cancels or links purchases."""
        return service.official_orders(account, page, limit)

    @server.tool(annotations=READ)
    @protected
    def official_order_detail(account: str, order_id: str, attempt_id: str | None = None) -> dict:
        """Read the exact official order and cashier status. Show meal, amount, pickup arrangement/code without payment references or contact details. Optionally compare with a local attempt's quote; matching content alone does not prove origin, link the order or authorize payment. Does not replace order_status for an already-linked purchase."""
        return service.official_order(account, order_id, attempt_id)

    return server


def main():
    root = Path(os.environ.get("EZMCD_DATA_DIR", "~/.local/share/ezmcdonalds")).expanduser()
    build_server(Service(Store(root))).run(transport="stdio")


if __name__ == "__main__":
    main()
