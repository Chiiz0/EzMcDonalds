"""Read-only diagnostics; never starts an interceptor or repairs settings."""
from importlib.metadata import PackageNotFoundError, version
import platform
import subprocess

from .errors import EzMcdError
from .sampling_local import TRANSPORT, LOCAL_MODE


def sampling_status(service="Wi-Fi"):
    if (not isinstance(service, str) or not service.strip() or service.startswith("-")
            or len(service) > 120 or any(ord(c) < 32 for c in service)):
        raise EzMcdError("sampling_options_invalid")
    try:
        installed = version("mitmproxy")
    except PackageNotFoundError:
        installed = None
    result = {"transport": TRANSPORT, "process_selector": LOCAL_MODE,
        "system_proxy_modified": False, "sampler_started": False,
        "runtime_capture_enabled": False, "native_acceptance": "wallet_connection_and_maintenance_verified_a39",
        "mitmproxy_version": installed, "network_service": service,
        "proxy_status": "unsupported_platform", "order_created": False, "payment_attempted": False}
    if platform.system() != "Darwin":
        return result
    try:
        def query(args, delimiter):
            output = subprocess.run(args, capture_output=True, text=True, timeout=5)
            if output.returncode:
                raise ValueError("query_failed")
            return dict(line.strip().split(delimiter, 1) for line in output.stdout.splitlines() if delimiter in line)
        effective = query(["/usr/sbin/scutil", "--proxy"], " : ")
        https = query(["/usr/sbin/networksetup", "-getsecurewebproxy", service], ": ")
        if https.get("Enabled") not in ("Yes", "No") or "HTTPSEnable" not in effective:
            raise ValueError("query_unrecognized")
        flags = ("HTTPEnable", "HTTPSEnable", "SOCKSEnable", "ProxyAutoConfigEnable", "ProxyAutoDiscoveryEnable")
        active = [k for k in flags if effective.get(k, "0") != "0"]
        result.update(proxy_status="active" if active or https["Enabled"] == "Yes" else "disabled",
            active_proxy_types=active,
            possible_legacy_ezmcd_proxy=(https["Enabled"] == "Yes" and https.get("Server") == "127.0.0.1"
                                        and https.get("Port") == "18089"),
            next_action="仅报告状态，不改写配置。旧版本机代理仍启用时，可在系统网络设置关闭；保留自己的其他代理。")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result.update(proxy_status="unreadable", next_action="无法确认系统代理状态，请在系统网络设置查看；本命令没有修改网络。")
    return result
