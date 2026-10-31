"""Sign the verified protocol using the user's own pinned application binary."""
import base64
from datetime import datetime, timezone
from email.utils import format_datetime
from functools import lru_cache
import hashlib
import hmac
from pathlib import Path
import secrets
import struct
import time

from ..errors import EzMcdError
from .macho import MachO
from .v5 import ObservedRuntime, encode

BUILD = "745da50c09c44a62eb222f72fda2571b9fa3acec55a2a17a4e5fdee5b7a85d85"
SIGNED = frozenset(("ct", "language", "p", "sid", "token", "v", "sv", "x-mcd-gw-v"))


@lru_cache(maxsize=4)
def _constants(path, size, modified):
    m = MachO(path)
    if hashlib.sha256(m.blob).hexdigest() != BUILD:
        raise EzMcdError("official_client_build_not_supported")
    def decode(address, length, delta):
        return bytes(v ^ ((i - delta) & 255) for i, v in enumerate(m.read(address, length))).decode("ascii")
    digest = list(struct.unpack("<64I", m.read(0x103279890, 256)))
    digest[16] &= 0xff00ff00
    digest[19] &= 0xff0011ff
    digest[29] &= 0xff110011
    ums = tuple(m.describe(x) for x in (0x102b7e410, 0x102b7e430, 0x102b7e450))
    if not all(x and x.isascii() for x in ums):
        raise EzMcdError("invalid_wallet_client_constants")
    return decode(0x10237DA39, 20, 5), decode(0x10237DA4E, 40, 29), tuple(digest), ums


def constants(binary):
    path = Path(binary).expanduser().absolute()
    info = path.stat()
    return _constants(str(path), info.st_size, info.st_mtime_ns)


def sign_v4(headers, ak, secret, moment, method, path, query="", body=b""):
    names = sorted(k for k in SIGNED if headers.get(k))
    header_text = "\n".join(f"{k}:{headers[k]}" for k in names)
    timestamp = format_datetime(moment, usegmt=True)
    raw = f"{method}\n{path}\n{query}\n{ak}\n{timestamp}\n{header_text}\n".encode()
    def digest(value):
        return base64.b64encode(hmac.new(secret.encode("ascii"), value, hashlib.sha256).digest()).decode("ascii")
    return {"authorization": "#".join(("hmac-auth-v1", ak, digest(raw), "hmac-sha256", timestamp, ";".join(names))),
            "x-hmac-digest": digest(body)}


class Signer:
    def __init__(self, account):
        self.account = account

    def runtime(self):
        runtime = self.account.get("runtime")
        if not runtime or not 0 <= time.time() - runtime["observed_at"] <= 24 * 3600:
            raise EzMcdError("runtime_expired_reconnect_official_session")
        return ObservedRuntime(runtime["device_hex"], runtime["environment_hex"])

    def headers(self, method, path, body=b"", query="", business=None, v5=False):
        profile = self.account["profile"]
        fields = profile["fields"]
        now = time.time()
        headers = {"User-Agent": "McdApp (iOS)", "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8", "sid": fields["sid"],
            "token": fields["deviceToken"], "sv": "v5" if v5 else "v4",
            "st": f"{now:.0f}", "nonce": str(int(now * 1000)) + str(secrets.randbelow(1000000))}
        headers.update({k: profile["client_headers"][k] for k in ("ct", "v", "language")})
        for field, name in (("meddyId", "meddyid"), ("routingId", "routingid"), ("tid", "tid"), ("riskDeviceId", "d")):
            if fields.get(field):
                headers[name] = fields[field]
        if business:
            headers.update(biz_from=business[0], biz_scenario=business[1])
        if method == "GET" and path == "/bff/order/orders":
            headers["apiVersion"] = "2.0"
        ak, secret, digest, _ = constants(self.account["binary"])
        if v5:
            headers["x-mcd-sign"] = encode(path, self.runtime(), milliseconds=int(now * 1000),
                random_value=1 + secrets.randbelow(4095), counter=1, constants=digest)
        else:
            headers["x-mcd-gw-v"] = "1"
            if path == "/bff/promotion/coupons/rightCards" and "biz_from" in headers:
                headers["biz_form"] = headers.pop("biz_from")
            headers.update(sign_v4(headers, ak, secret, datetime.fromtimestamp(now, timezone.utc), method, path, query, body))
        return headers

    def wallet_query(self, token, body):
        app_id, version, key = constants(self.account["binary"])[3]
        timestamp = str(int(time.time()) * 1000)
        signature = hashlib.sha256((version + timestamp + app_id + body.decode("ascii") + key).encode("ascii")).hexdigest()
        return {"appId": app_id, "version": version, "signKey": key, "timestamp": timestamp, "sign": signature, "token": token}


def device_update_metadata():
    values = []
    for path in ("/System/Library/CoreServices/SystemVersion.plist", "/var/mobile/Library/UserConfigurationProfiles/PublicInfo/MCMeta.plist"):
        try:
            info = Path(path).stat()
        except FileNotFoundError:
            values.append("null")
            continue
        m, c = divmod(info.st_mtime_ns, 10**9), divmod(info.st_ctime_ns, 10**9)
        values.append(f"{m[0]}.{m[1]},{c[0]}.{c[1]}")
    if all(v == "null" for v in values):
        raise EzMcdError("current_adapter_requires_official_macos_client_environment")
    return "|".join(values)
