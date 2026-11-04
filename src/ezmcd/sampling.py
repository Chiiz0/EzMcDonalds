"""Bounded app-scoped wallet capture; no system proxy mutation or runtime capture."""
import http.client
import json
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
from .sampling_local import TRANSPORT, LOCAL_MODE, PROBE_MODE
from .sampling_diagnostics import routing_diagnostics
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
        diagnostics = routing_diagnostics(audit)
        if diagnostics.get("native_connections", 0) > 0:
            state = "native_traffic_without_api"
            action = "已观察到本机连接，但没有可用的官方 API 请求。查看连接与 TLS 计数定位问题；不能仅凭监听器就绪判断原生采样成功，也不要通过提交订单测试。"
    elif audit.get("balance_saved") is not True:
        state = "balance_rejected" if audit.get("balance_error") else "balance_missing"
        action = "没有可用的本人余额样本。不要导入部分文件或继续提交；核对账号和余额诊断，下次必须先看到 BALANCE_CAPTURED。"
    else:
        state = "runtime_rejected" if audit.get("last_result") == "runtime_rejected" else "runtime_missing"
        action = "余额已校验，但未取得有效运行样本。旧凭据保持不变；不要单独导入余额来覆盖账号，需要在新的保护窗口内完成运行字段采样。"
    return {"capture_state": state, "sample_import_ready": state == "complete",
        "new_sampling_required": state != "complete", "capture_window_active": False,
        "routing_diagnostics": routing_diagnostics(audit), "next_action": action}


class RuntimeCapture:
    balance_only = False

    def __init__(self, store, alias, *, service="Wi-Fi", seconds=120, mitmdump=None, refresh_from=None, report=None, manage_app=False):
        if (not isinstance(service, str) or not service.strip() or service.startswith("-")
                or len(service) > 120 or any(ord(c) < 32 for c in service)
                or type(seconds) is not int or not 30 <= seconds <= 120 or type(manage_app) is not bool):
            raise EzMcdError("sampling_options_invalid")
        self.store, self.alias = store, alias
        self.service, self.seconds = service, seconds
        self.executable = mitmdump
        self.refresh_from = refresh_from
        self.report = report or (lambda value: print(json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True))
        self.directory = store.root / "captures" / uuid.uuid4().hex
        self.ca = SamplingCertificate(store)
        self.manage_app = manage_app
        self.app = None
        self.app_close_failed = False

    def close_managed_app(self):
        if self.app is None or self.app_close_failed:
            return
        try:
            self.app.request_close()
        except EzMcdError:
            self.app_close_failed = True
            self.report({"stage": "APP_MANUAL_CLOSE_REQUIRED",
                "next": "未能安全退出官方 App，请手动退出；本轮会限时停止，请不要在 App 内提交或付款。"})

    def launch_pending(self):
        return self.app is not None and self.app.launch_pending()

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

    def require_scope(self):
        if not self.balance_only:
            raise EzMcdError("sampling_runtime_capture_suspended")

    def executable_path(self):
        if platform.system() != "Darwin":
            raise EzMcdError("sampling_certificate_macos_required")
        sibling = Path(sys.executable).with_name("mitmdump")
        executable = self.executable or (str(sibling) if sibling.is_file() else shutil.which("mitmdump"))
        if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise EzMcdError("sampling_mitmproxy_required")
        return str(Path(executable).absolute())

    def run(self):
        self.require_scope()
        executable = self.executable_path()
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
                return self._run(account, ca, executable,
                    fingerprint({"sid": fields["sid"], "token": fields["deviceToken"]}))
            except subprocess.TimeoutExpired:
                raise EzMcdError("sampling_system_command_failed") from None
            except http.client.HTTPException:
                raise EzMcdError("sampling_guard_preflight_failed") from None

    def configuration(self, account, deadline, refresh_session):
        return {"output": str(self.directory), "binary": account["binary"],
            "identity": account["identity"], "deadline": deadline, "refresh_session": refresh_session,
            "schema_version": 2, "account_fingerprint": fingerprint(account)}

    def completed(self, audit):
        return audit["balance_saved"] if self.balance_only else audit["runtime_saved"]

    def outcome(self, audit):
        return capture_outcome(audit)

    def _run(self, account, ca, executable, refresh_session):
        self.require_scope()
        before, effective_before = self.proxy_state(), self.effective_proxy()
        if (before.get("Enabled") != "No" or before.get("Authenticated Proxy Enabled") != "0"
                or any(effective_before.get(k, "0") != "0" for k in
                       ("HTTPEnable", "HTTPSEnable", "SOCKSEnable", "ProxyAutoConfigEnable", "ProxyAutoDiscoveryEnable"))):
            raise EzMcdError("sampling_existing_proxy_requires_review")
        from .sampling_app import SamplingApp
        verified_app = SamplingApp(account["binary"])
        verified_app.validate()
        if self.manage_app:
            self.app = verified_app
            self.report({"stage": "APP_PREPARING", "next": "正在核对并退出本人官方 App；不会修改系统代理。"})
            self.app.request_close()
            for _ in range(20):
                if not self.app_running():
                    break
                time.sleep(.25)
        if self.app_running():
            raise EzMcdError("sampling_close_official_app_first")
        secure_directory(self.directory)
        deadline = time.time() + self.seconds
        stop_at = time.monotonic() + self.seconds
        config = self.configuration(account, deadline, refresh_session) | {
            "transport": TRANSPORT, "local_mode": LOCAL_MODE, "parent_pid": os.getpid()}
        write_private(self.directory / "config.json", config)
        # Read-only snapshots: never restore over a network setting the user
        # changed while collection was running.
        write_private(self.directory / "proxy-before.json", {"service": self.service, "proxy": before})
        loader = self.directory / "addon.py"
        with loader.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(Path(__file__).with_name("_mitm_runtime_guard.py").read_bytes())
        process = None
        seen_app = False
        stop = False
        old_signals = {}
        errors = []

        def interrupted(*_):
            nonlocal stop
            stop = True

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_signals[sig] = signal.signal(sig, interrupted)
            self.report({"stage": "LOCAL_CAPTURE_STARTING", "next":
                "正在启动仅匹配麦当劳进程的本机采样；可能需要 macOS 网络扩展授权。未就绪前不要打开 App。"})
            process = subprocess.Popen([executable, "--mode", LOCAL_MODE, "--mode", PROBE_MODE,
                "--set", f"confdir={ca}", "--set", "connection_strategy=lazy",
                "--set", "upstream_cert=false", "--set", "rawtcp=false", "--set", "flow_detail=0",
                "--set", "termlog_verbosity=error", "--set", "body_size_limit=256k", "-s", str(loader)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, umask=0o077,
                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent),
                         EZMCD_RUNTIME_GUARD_CONFIG=str(self.directory / "config.json")))
            # Allow a bounded startup window for the native extension. No
            # fallback to system HTTPS, PAC, SOCKS, DNS, routes or firewall.
            for _ in range(100):
                if stop:
                    raise EzMcdError("sampling_cancelled_before_start")
                if process.poll() is not None:
                    raise EzMcdError("sampling_local_capture_unavailable")
                ready_path = self.directory / "transport-ready.json"
                if ready_path.exists() and (self.directory / "audit.json").exists():
                    if read_private(ready_path) != {"transport": TRANSPORT, "local_mode": LOCAL_MODE,
                                                   "system_proxy_modified": False}:
                        raise EzMcdError("sampling_local_scope_invalid")
                    break
                time.sleep(.1)
            else:
                raise EzMcdError("sampling_local_capture_unavailable")
            self.probe(ca / "mitmproxy-ca-cert.pem")
            if self.app_running() or self.proxy_state() != before or self.effective_proxy() != effective_before:
                raise EzMcdError("sampling_environment_changed")
            if stop:
                raise EzMcdError("sampling_cancelled_before_start")
            self.report({"stage": "GUARD_ACTIVE", "seconds": max(0, int(stop_at - time.monotonic())),
                "transport": TRANSPORT, "system_proxy_modified": False, "next":
                "只进入麦钱包。不要提交订单或付款；本模式不用于订单运行字段采样。"})
            if self.app is not None and time.monotonic() < stop_at:
                self.app.start()
            while not stop and time.monotonic() < stop_at:
                if process.poll() is not None:
                    raise EzMcdError("sampling_guard_listener_failed")
                running = self.app_running()
                if running and not seen_app:
                    seen_app = True
                    self.report({"stage": "APP_RUNNING", "next": "已观察到麦当劳进程，请进入麦钱包。"})
                state = read_private(self.directory / "audit.json")
                if state.get("balance_saved"):
                    self.report({"stage": "BALANCE_CAPTURED", "seconds_remaining": 0,
                        "capture_window_active": False, "next": "余额样本已校验，正在结束本轮。"})
                    break
                if self.app is not None and self.app.launch_failed():
                    raise EzMcdError("sampling_managed_app_launch_failed")
                if not running and not self.launch_pending() and (seen_app or self.app is not None):
                    break
                time.sleep(.1)
        finally:
            try:
                closed, pending = False, True
                try:
                    self.report({"stage": "CLOSE_OFFICIAL_APP_NOW", "next": "采样结束，请退出麦当劳。系统代理未被修改。"})
                    # A stuck App or failed state query must not prevent the
                    # owned capture child from being stopped.
                    for _ in range(20):
                        if not self.app_running() and not self.launch_pending():
                            break
                        self.close_managed_app()
                        time.sleep(.25)
                    closed, pending = not self.app_running(), self.launch_pending()
                    if not closed or pending:
                        errors.append("official_app_still_running")
                        self.report({"stage": "APP_MANUAL_CLOSE_REQUIRED", "next":
                            "请手动退出麦当劳。本轮采样停止，不再提供阻断；不要提交订单或付款。"})
                except (EzMcdError, OSError, subprocess.TimeoutExpired):
                    errors.append("app_cleanup_unconfirmed")
                finally:
                    if process and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                try:
                    after, effective_after = self.proxy_state(), self.effective_proxy()
                except (EzMcdError, OSError, subprocess.TimeoutExpired):
                    after, effective_after = None, None
                    errors.append("system_proxy_state_unreadable")
                unchanged = after == before and effective_after == effective_before
                if not unchanged:
                    errors.append("system_proxy_changed_externally")
                write_private(self.directory / "cleanup.json", {"transport": TRANSPORT,
                    "system_proxy_modified": False, "system_proxy_unchanged": unchanged,
                    "proxy_before": before, "proxy_after": after,
                    "effective_proxy_before": effective_before, "effective_proxy_after": effective_after,
                    "cleanup_errors": sorted(set(errors)), "official_app_closed": closed,
                    "managed_app": self.manage_app, "app_launch_pending": pending,
                    "official_app_observed": seen_app,
                    "capture_process_stopped": process is None or process.poll() is not None,
                    "persistent_certificate_retained": True})
                self.report({"stage": "CLEANUP_RESULT", "proxy_restored": unchanged,
                    "system_proxy_modified": False, "system_proxy_unchanged": unchanged,
                    "persistent_certificate_retained": True, "errors": sorted(set(errors)),
                    "capture_directory": str(self.directory)})
                if errors:
                    raise EzMcdError("sampling_cleanup_needs_attention")
            finally:
                for sig, handler in old_signals.items():
                    signal.signal(sig, handler)
        audit = read_private(self.directory / "audit.json")
        outcome = self.outcome(audit)
        self.report({"stage": "CAPTURE_FINISHED", **outcome})
        return {"account": self.alias, "capture_directory": str(self.directory),
            "balance_saved": audit["balance_saved"], "runtime_saved": False,
            "balance_sample": str(self.directory / "balance.json") if audit["balance_saved"] else None,
            "runtime_sample": None, "account_updated": False, "certificate_retained": True,
            "proxy_restored": True, "system_proxy_modified": False, "transport": TRANSPORT,
            "managed_app": self.manage_app, "native_acceptance": "pending",
            "official_app_observed": seen_app,
            "order_requests_forwarded": 0, "payment_requests_forwarded": 0,
            "balance_error": audit.get("balance_error"), "balance_header_checks": audit.get("balance_header_checks"),
            **outcome}
