"""Bounded HTTPS transport: fixed origins, verified TLS, no retries/redirects."""
import json
import re
from urllib.parse import quote, urlencode
import urllib.error
import urllib.request

from .errors import EzMcdError
from .protocol.signing import Signer

API = "https://api.mcd.cn"
WALLET = "https://spurcard.chinaums.com/v1/"
CONSUME = "h5/front/consume/transConsume"
READS = {
    "/bff/member/mwallet/balance": ("1006", "501"),
    "/bff/member/mwallet/token": ("1005", "501"),
    "/bff/promotion/coupons/rightCards": ("1073", "300"),
    "/bff/store/stores/collection/list": None,
    "/bff/store/cities/group": None,
    "/bff/store/stores": ("1013", "103"),
    "/bff/store/stores/vicinity": ("1003", "102"),
    "/bff/spc/menu": ("1006", "101"),
    "/bff/cart/carts": ("1007", "101"),
    "/bff/order/confirmation/info": ("1011", "202"),
    "/bff/order/orders": ("1001", "202"),
    "/bff/cashier/channels": ("1001", "501"),
}
WRITES = {("PUT", "/bff/cart/carts"): None,
          ("POST", "/bff/passport/login/session/refresh"): ("1006", "500"),
          ("POST", "/bff/order/orders"): ("1012", "202"),
          ("POST", "/bff/cashier/preorder"): ("1002", "501")}


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Transport:
    def __init__(self, account):
        self.signer = Signer(account)

    def send(self, method, path, params=None, body=None):
        if method == "GET":
            if path in READS:
                business = READS[path]
            elif re.fullmatch(r"/bff/store/stores/[0-9]{7}", path):
                business = ("1004", "101")
            elif re.fullmatch(r"/bff/store/stores/[0-9]{7}/menu/reservationTimeOption", path):
                business = None
            elif re.fullmatch(r"/bff/order/orders/[A-Za-z0-9_-]{1,80}", path):
                business = ("1023", "202")
            elif re.fullmatch(r"/bff/cashier/[A-Za-z0-9_-]{1,80}/status", path):
                business = ("1003", "501")
            else:
                raise EzMcdError("endpoint_not_supported")
        elif method == "POST" and re.fullmatch(r"/bff/spc/products/detail/[A-Za-z0-9]{1,20}", path):
            # Official product-detail query uses POST but does not mutate the cart.
            business = ("1001", "101")
        elif (method, path) in WRITES:
            business = WRITES[method, path]
        else:
            raise EzMcdError("endpoint_not_supported")
        query = urlencode(sorted((params or {}).items()), quote_via=quote, doseq=True)
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        headers = self.signer.headers(method, path, raw, query, business,
            v5=(method == "POST" and path == "/bff/order/orders"))
        return self._open(method, API + path + ("?" + query if query else ""), headers, raw if body is not None else None)

    def consume(self, sequence, token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sequence) or not isinstance(token, str) or not token:
            raise EzMcdError("invalid_wallet_payment_inputs")
        raw = json.dumps({"bizSeqNo": sequence}, separators=(",", ":")).encode("ascii")
        query = urlencode(self.signer.wallet_query(token, raw), quote_via=quote)
        return self._open("POST", WALLET + CONSUME + "?" + query,
                          {"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"}, raw)

    @staticmethod
    def _open(method, url, headers, raw):
        request = urllib.request.Request(url, data=raw, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
        try:
            try:
                response = opener.open(request, timeout=25)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise EzMcdError("response_too_large")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise EzMcdError("unexpected_response_type")
                return {"http_status": response.status, "response": payload}
        except (OSError, urllib.error.URLError, ValueError):
            # Never echo URLs, headers, response bodies or credential-bearing exceptions.
            raise EzMcdError("transport_unknown_do_not_retry_writes") from None


def data(result):
    payload = result["response"]
    if result["http_status"] == 401 or payload.get("success") is not True and str(payload.get("code")) == "401":
        raise EzMcdError("session_expired_reconnect_locally")
    if result["http_status"] != 200 or payload.get("success") is not True:
        raise EzMcdError("upstream_rejected_query_status_or_reconnect")
    return payload.get("data")
