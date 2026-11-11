"""Offline extraction of explicitly supplied, private HAR files.

This module does not install a certificate, change a proxy, capture traffic,
replay requests, or retain the rest of a user's network archive.
"""
import base64
import re
from urllib.parse import urlsplit

from .errors import EzMcdError

BALANCE = "/bff/member/mwallet/balance"
ORDER = "/bff/order/orders"


def sample_headers(value, *, har=False):
    """Normalize names only after rejecting ambiguous or malformed headers.

    A HAR's list and a JSON sample's mapping both allow case variants; neither
    may choose a session or signature by silently keeping the last value.
    """
    if not isinstance(value, list if har else dict) or len(value) > 100:
        raise EzMcdError("official_sample_headers_invalid_or_repeated")
    if har:
        if any(not isinstance(h, dict) for h in value):
            raise EzMcdError("official_sample_headers_invalid_or_repeated")
        pairs = ((h.get("name"), h.get("value")) for h in value)
    else:
        pairs = value.items()
    result = {}
    for name, content in pairs:
        if (not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or len(name) > 256 or name.lower() in result or not isinstance(content, str)
                or len(content) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in content)):
            raise EzMcdError("official_sample_headers_invalid_or_repeated")
        result[name.lower()] = content
    if "host" in result and result["host"] not in ("api.mcd.cn", "api.mcd.cn:443"):
        raise EzMcdError("official_sample_host_mismatch")
    return result


def runtime_sample_from_har(har):
    samples = []
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        url = urlsplit(request.get("url", ""))
        if (url.scheme, url.netloc, url.path, url.query, url.fragment, request.get("method")) != (
                "https", "api.mcd.cn", ORDER, "", "", "POST"):
            continue
        headers = sample_headers(request.get("headers", []), har=True)
        if headers.get("x-mcd-sign"):
            samples.append({"request": {"method": "POST", "host": "api.mcd.cn", "path": ORDER,
                                        "headers": headers, "body": ""}})
    identities = {(s["request"]["headers"].get("sid"), s["request"]["headers"].get("token")) for s in samples}
    if len(identities) != 1 or any(not sid or not token for sid, token in identities):
        raise EzMcdError("har_requires_one_official_runtime_session")
    return samples[-1]


def samples_from_har(har):
    balances, runtimes = [], []
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        url = urlsplit(request.get("url", ""))
        if url.scheme != "https" or url.netloc != "api.mcd.cn" or url.query or url.fragment:
            continue
        balance_request = (request.get("method") == "GET" and url.path == BALANCE
                           and entry.get("response", {}).get("status") == 200)
        runtime_request = request.get("method") == "POST" and url.path == ORDER
        if not balance_request and not runtime_request:
            continue
        headers = sample_headers(request.get("headers", []), har=True)
        selected = {"method": request.get("method"), "host": "api.mcd.cn", "path": url.path,
                    "headers": headers, "body": request.get("postData", {}).get("text", "") if balance_request else ""}
        if balance_request:
            response = entry["response"]
            content = response.get("content", {})
            body = content.get("text", "")
            if content.get("encoding") == "base64":
                body = base64.b64decode(body, validate=True).decode("utf-8")
            balances.append({"request": selected, "response": {"status": 200, "body": body}})
        elif headers.get("x-mcd-sign"):
            # Runtime decoding needs headers only, never products, amounts or order IDs.
            selected["body"] = ""
            runtimes.append({"request": selected})
    identities = {(s["request"]["headers"].get("sid"), s["request"]["headers"].get("token")) for s in balances}
    if len(identities) != 1:
        raise EzMcdError("har_requires_one_official_session_balance_sample")
    balance = balances[-1]
    matching = [s for s in runtimes if (s["request"]["headers"].get("sid"), s["request"]["headers"].get("token")) in identities]
    return balance, matching[-1] if matching else None
