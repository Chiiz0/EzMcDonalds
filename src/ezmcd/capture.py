"""Offline extraction of explicitly supplied, private HAR files.

This module does not install a certificate, change a proxy, capture traffic,
replay requests, or retain the rest of a user's network archive.
"""
import base64
from urllib.parse import urlsplit

from .errors import EzMcdError

BALANCE = "/bff/member/mwallet/balance"
ORDER = "/bff/order/orders"


def samples_from_har(har):
    balances, runtimes = [], []
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        url = urlsplit(request.get("url", ""))
        if url.scheme != "https" or url.netloc != "api.mcd.cn" or url.query or url.fragment:
            continue
        headers = {h["name"].lower(): h["value"] for h in request.get("headers", [])}
        selected = {"method": request.get("method"), "host": "api.mcd.cn", "path": url.path,
                    "headers": headers, "body": request.get("postData", {}).get("text", "")}
        if selected["method"] == "GET" and url.path == BALANCE and entry.get("response", {}).get("status") == 200:
            response = entry["response"]
            content = response.get("content", {})
            body = content.get("text", "")
            if content.get("encoding") == "base64":
                body = base64.b64decode(body, validate=True).decode("utf-8")
            balances.append({"request": selected, "response": {"status": 200, "body": body}})
        elif selected["method"] == "POST" and url.path == ORDER and headers.get("x-mcd-sign"):
            # Runtime decoding needs headers only, never products, amounts or order IDs.
            selected["body"] = ""
            runtimes.append({"request": selected})
    identities = {(s["request"]["headers"].get("sid"), s["request"]["headers"].get("token")) for s in balances}
    if len(identities) != 1:
        raise EzMcdError("har_requires_one_official_session_balance_sample")
    balance = balances[-1]
    matching = [s for s in runtimes if (s["request"]["headers"].get("sid"), s["request"]["headers"].get("token")) in identities]
    return balance, matching[-1] if matching else None
