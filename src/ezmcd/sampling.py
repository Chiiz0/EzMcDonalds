"""Explicit, bounded macOS capture. Never exposes OS proxy control to MCP."""
import http.client
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import ssl
import subprocess
import sys
import time
import uuid

from .accounts import profile_from_balance_sample
from .errors import EzMcdError
from .models import fingerprint
from .sampling_cert import SamplingCertificate
from .storage import read_private, write_private, secure_directory

PORT = 18089


def capture_outcome(audit):
    """Explain a finished capture; the synthetic blocked probe is not App traffic."""
    if audit.get("balance_saved") is True and audit.get("runtime_saved") is True:
        state = "complete"
        action = "两个样本已校验。核对官方订单、余额和测试购物车后，可用 sampling-import 导入此采样目录；本轮未授权消费。"
    elif (audit.get("balance_saved") is not True and audit.get("allowed_gets", 0) == 0 and audit.get("blocked_requests", 0) <= 1
          and audit.get("allowed_session_refreshes", 0) == 0):
        state = "no_api_requests"
        action = "窗口内未观察到 App 的 API 请求，只有本地阻断自检。不要导入或继续点提交；确认 Mac App 和联网服务准备好后，再开始新的采样窗口。"
    elif audit.get("balance_saved") is not True:
        state = "balance_rejected" if audit.get("balance_error") else "balance_missing"
        action = "没有可用的本人余额样本。不要导入部分文件或继续提交；核对账号和余额诊断，下次必须先看到 BALANCE_CAPTURED。"
    else:
        state = "runtime_rejected" if audit.get("last_result") == "runtime_rejected" else "runtime_missing"
        action = "余额已校验，但未取得有效运行样本。旧凭据保持不变；不要单独导入余额来覆盖账号，需要在新的保护窗口内完成运行字段采样。"
    return {"capture_state": state, "sample_import_ready": state == "complete",
        "new_sampling_required": state != "complete", "capture_window_active": False,
        "next_action": action}


class RuntimeCapture:
    def __init__(self, store, alias, *, service="Wi-Fi", seconds=120, mitmdump=None, refresh_from=None, report=None):
        if (not isinstance(service, str) or not service.strip() or service.startswith("-")
                or len(service) > 120 or any(ord(c) < 32 for c in service)
                or type(seconds) is not int or not 30 <= seconds <= 120):
            raise EzMcdError("sampling_options_invalid")
        self.store, self.alias = store, alias
        self.service, self.seconds = service, seconds
        self.executable = mitmdump
        self.refresh_from = refresh_from
        self.report = report or (lambda value: print(json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True))
        self.directory = store.root / "captures" / uuid.uuid4().hex
        self.ca = SamplingCertificate(store)

    @staticmethod
    def command(args, *, check=True):
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
        if check and result.returncode:
            raise EzMcdError("sampling_system_command_failed")
        return result

    def proxy_state(self):
        result = self.command(["/usr/sbin/networksetup", "-getsecurewebproxy", self.service])
        return dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)

    def effective_proxy(self):
        result = self.command(["/usr/sbin/scutil", "--proxy"])
        return dict(line.strip().split(" : ", 1) for line in result.stdout.splitlines() if " : " in line)

    def app_running(self):
        result = self.command(["/usr/bin/pgrep", "-u", str(os.getuid()), "-x", "McdApp"], check=False)
        if result.returncode not in (0, 1):
            raise EzMcdError("sampling_app_state_unreadable")
        return result.returncode == 0

    def probe(self, certificate):
        client = http.client.HTTPSConnection("127.0.0.1", PORT,
            context=ssl.create_default_context(cafile=str(certificate)), timeout=5)
        client.set_tunnel("api.mcd.cn", 443)
        try:
            client.request("POST", "/bff/order/orders", body=b'{"synthetic":"guard-preflight"}',
                headers={"Content-Type": "application/json"})
            try:
                response = client.getresponse()
                response.read()
                if response.status != 409 or response.getheader("x-ezmcd-local-block") != "1":
                    raise EzMcdError("sampling_guard_preflight_failed")
            except http.client.RemoteDisconnected:
                pass  # Header-side kill: no request body reaches upstream.
        finally:
            client.close()
        audit = read_private(self.directory / "audit.json")
        if audit["blocked_requests"] != 1 or audit["upstream_connections"] != 0:
            raise EzMcdError("sampling_guard_preflight_failed")

    def run(self):
        if platform.system() != "Darwin":
            raise EzMcdError("sampling_certificate_macos_required")
        sibling = Path(sys.executable).with_name("mitmdump")
        executable = self.executable or (str(sibling) if sibling.is_file() else shutil.which("mitmdump"))
        if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise EzMcdError("sampling_mitmproxy_required")
        account = self.store.account(self.alias)
        if account.get("revoked") or account["provider"] != "mcd-cn":
            raise EzMcdError("active_official_account_required_for_runtime_update")
        with self.store.lock(account["identity"]), self.ca.lease() as ca:
            account = self.store.account(self.alias)
            profile = account["profile"]
            if self.refresh_from:
                profile = profile_from_balance_sample(read_private(Path(self.refresh_from).expanduser()), account["binary"])
            fields = profile["fields"]
            if fingerprint({"provider": "mcd-cn", "member": fields["meddyId"]}) != account["identity"]:
                raise EzMcdError("sampling_member_mismatch")
            try:
                return self._run(account, ca, str(Path(executable).absolute()),
                    fingerprint({"sid": fields["sid"], "token": fields["deviceToken"]}))
            except subprocess.TimeoutExpired:
                raise EzMcdError("sampling_system_command_failed") from None
            except http.client.HTTPException:
                raise EzMcdError("sampling_guard_preflight_failed") from None

    def _run(self, account, ca, executable, refresh_session):
        before, effective_before = self.proxy_state(), self.effective_proxy()
        if (before.get("Enabled") != "No" or before.get("Authenticated Proxy Enabled") != "0"
                or not before.get("Server") or not before.get("Port", "").isdigit()
                or any(effective_before.get(k, "0") != "0" for k in
                       ("HTTPEnable", "HTTPSEnable", "SOCKSEnable", "ProxyAutoConfigEnable", "ProxyAutoDiscoveryEnable"))):
            raise EzMcdError("sampling_existing_proxy_requires_review")
        if self.app_running():
            raise EzMcdError("sampling_close_official_app_first")
        secure_directory(self.directory)
        deadline = time.time() + self.seconds
        write_private(self.directory / "config.json", {"output": str(self.directory), "binary": account["binary"],
            "identity": account["identity"], "deadline": deadline, "refresh_session": refresh_session,
            "schema_version": 2, "account_fingerprint": fingerprint(account)})
        write_private(self.directory / "proxy-before.json", {"service": self.service, "proxy": before})
        # mitmdump watches its script for changes. Pin a private loader for this
        # run so an editable install or package upgrade cannot unload the guard.
        loader = self.directory / "addon.py"
        with loader.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(Path(__file__).with_name("_mitm_runtime_guard.py").read_bytes())
        process = None
        touched, stop = False, False
        old_signals = {}
        errors = []

        def interrupted(*_):
            nonlocal stop
            stop = True

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_signals[sig] = signal.signal(sig, interrupted)
            # Discard proxy logs rather than retaining arbitrary upstream error
            # strings. Sanitized audit.json is the diagnostic surface.
            process = subprocess.Popen([executable, "--listen-host", "127.0.0.1", "--listen-port", str(PORT),
                "--set", f"confdir={ca}", "--allow-hosts", r"^api\.mcd\.cn(?::443)?$",
                "--set", "connection_strategy=lazy", "--set", "upstream_cert=false", "--set", "flow_detail=0",
                "--set", "termlog_verbosity=error", "--set", "body_size_limit=256k",
                "-s", str(loader)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, umask=0o077,
                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent),
                         EZMCD_RUNTIME_GUARD_CONFIG=str(self.directory / "config.json")))
            for _ in range(100):
                if process.poll() is not None:
                    raise EzMcdError("sampling_guard_listener_failed")
                if (self.directory / "audit.json").exists():
                    break
                time.sleep(.05)
            else:
                raise EzMcdError("sampling_guard_listener_failed")
            self.probe(ca / "mitmproxy-ca-cert.pem")
            if self.app_running() or self.proxy_state() != before or self.effective_proxy() != effective_before:
                raise EzMcdError("sampling_environment_changed")
            if stop:
                raise EzMcdError("sampling_cancelled_before_start")
            touched = True
            self.command(["/usr/sbin/networksetup", "-setsecurewebproxy", self.service, "127.0.0.1", str(PORT)])
            self.command(["/usr/sbin/networksetup", "-setsecurewebproxystate", self.service, "on"])
            effective = self.effective_proxy()
            if (effective.get("HTTPSEnable"), effective.get("HTTPSProxy"), effective.get("HTTPSPort")) != ("1", "127.0.0.1", str(PORT)):
                raise EzMcdError("sampling_effective_proxy_mismatch")
            self.report({"stage": "GUARD_ACTIVE", "seconds": self.seconds,
                "next": "现在启动官方 App，先打开麦钱包余额，再操作已准备的购物车。订单会被本地阻断；见无订单采样指引。"})
            while not self.app_running() and not stop and time.time() <= deadline:
                if process.poll() is not None:
                    raise EzMcdError("sampling_guard_listener_failed")
                time.sleep(.25)
            warned = balance_announced = False
            while self.app_running():
                state = read_private(self.directory / "audit.json")
                if state["balance_saved"] and not balance_announced:
                    seconds_remaining = max(0, math.ceil(deadline - time.time()))
                    active = bool(seconds_remaining and not stop and not state["runtime_saved"] and process.poll() is None)
                    self.report({"stage": "BALANCE_CAPTURED", "seconds_remaining": seconds_remaining,
                        "capture_window_active": active,
                        "next": "本人余额样本已校验。可在当前保护窗口内进入确认页；提交请求会被阻断。" if active
                                else "余额样本已校验，本轮正在收尾。不要继续提交；请退出官方 App。"})
                    balance_announced = True
                if (stop or time.time() > deadline or state["runtime_saved"] or process.poll() is not None) and not warned:
                    self.report({"stage": "CLOSE_OFFICIAL_APP_NOW", "next": "请退出麦当劳；若卡住，只强制退出麦当劳。关闭后才恢复代理。"})
                    warned = True
                time.sleep(.25)
        finally:
            try:
                if touched:
                    if self.app_running():
                        self.report({"stage": "CLOSE_OFFICIAL_APP_NOW", "next": "代理仍在阻断。请先退出麦当劳，再等待清理完成。"})
                    while self.app_running():
                        time.sleep(.25)
                    for args in (["-setsecurewebproxystate", self.service, "off"],
                                 ["-setsecurewebproxy", self.service, before["Server"], before["Port"]],
                                 ["-setsecurewebproxystate", self.service, "off"]):
                        try:
                            if self.command(["/usr/sbin/networksetup", *args], check=False).returncode:
                                errors.append("proxy_restoration")
                        except (OSError, subprocess.TimeoutExpired):
                            errors.append("proxy_restoration")
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                after = self.proxy_state()
                if after != before:
                    errors.append("proxy_verification")
                write_private(self.directory / "cleanup.json", {"proxy_before": before, "proxy_after": after,
                    "cleanup_errors": sorted(set(errors)), "official_app_closed": not self.app_running(),
                    "persistent_certificate_retained": True})
                self.report({"stage": "CLEANUP_RESULT", "proxy_restored": after == before,
                    "persistent_certificate_retained": True, "errors": sorted(set(errors)),
                    "capture_directory": str(self.directory)})
                if errors:
                    raise EzMcdError("sampling_cleanup_needs_attention")
            finally:
                for sig, handler in old_signals.items():
                    signal.signal(sig, handler)
        audit = read_private(self.directory / "audit.json")
        outcome = capture_outcome(audit)
        self.report({"stage": "CAPTURE_FINISHED", **outcome})
        return {"account": self.alias, "capture_directory": str(self.directory),
            "balance_saved": audit["balance_saved"], "runtime_saved": audit["runtime_saved"],
            "balance_sample": str(self.directory / "balance.json") if audit["balance_saved"] else None,
            "runtime_sample": str(self.directory / "runtime.json") if audit["runtime_saved"] else None,
            "account_updated": False, "certificate_retained": True, "proxy_restored": True,
            "order_requests_forwarded": 0, "payment_requests_forwarded": 0,
            "balance_error": audit.get("balance_error"), "balance_header_checks": audit.get("balance_header_checks"),
            **outcome}
