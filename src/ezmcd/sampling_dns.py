"""Resolve the fixed API before interception, without changing system DNS."""
import ipaddress
import json
import re
import subprocess
import sys

from .errors import EzMcdError

HOST = "api.mcd.cn"


def parse_snapshot(value):
    if not isinstance(value, dict) or value.get("host") != HOST:
        raise ValueError("invalid_api_dns_snapshot")
    addresses, names = value.get("addresses"), value.get("names")
    if (not isinstance(addresses, list) or not 1 <= len(addresses) <= 64
            or not isinstance(names, list) or not 1 <= len(names) <= 64 or HOST not in names):
        raise ValueError("invalid_api_dns_snapshot")
    if any(not isinstance(a, str) for a in addresses):
        raise ValueError("invalid_api_dns_snapshot")
    ips = tuple(dict.fromkeys(ipaddress.ip_address(a) for a in addresses))
    if any(not isinstance(n, str) or len(n) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", n)
           or ".." in n for n in names):
        raise ValueError("invalid_api_dns_snapshot")
    return ips, set(names)


def api_dns_snapshot():
    program = ("import json,socket; a=socket.getaddrinfo('api.mcd.cn',443,type=socket.SOCK_STREAM,flags=socket.AI_CANONNAME); "
               "print(json.dumps({'host':'api.mcd.cn','addresses':list(dict.fromkeys(v[4][0] for v in a)),"
               "'names':list(dict.fromkeys(['api.mcd.cn']+[v[3].lower().removesuffix('.') for v in a if v[3]]))}))")
    try:
        result = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, text=True, timeout=5)
        if result.returncode or len(result.stdout) > 16384:
            raise ValueError("dns_failed")
        value = json.loads(result.stdout)
        parse_snapshot(value)
        return value
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        raise EzMcdError("sampling_api_dns_unavailable") from None
