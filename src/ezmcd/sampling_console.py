"""Human-readable guidance for an explicitly started local capture terminal.

This module never starts an App, touches proxies, or receives credentials.
Machine-readable final results stay on stdout; prompts and stages use stderr.
"""
import sys

from .errors import EzMcdError


class SamplingConsole:
    def __init__(self, *, stdin=None, stderr=None, manage_app=False, balance_only=False, update_account=False):
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stderr = stderr if stderr is not None else sys.stderr
        self.manage_app = manage_app
        self.balance_only = balance_only
        self.update_account = update_account

    def say(self, text):
        print(text, file=self.stderr, flush=True)

    def finished(self, result):
        flags = [name for name in ("account_created", "account_updated", "already_imported")
                 if result.get(name) is True]
        if result.get("ok") is False or len(flags) != 1:
            self.say("\n[ACCOUNT_NOT_CONFIRMED] 本轮未确认接入或更新成功。请查看最终结果；不要把采样已就绪或余额样本已校验当作账号更新完成。")
        elif flags[0] == "account_updated":
            self.say("\n[ACCOUNT_UPDATED] 本轮会话更新已完成并通过核验；消费策略和购买日志保留。")
        elif flags[0] == "account_created":
            self.say("\n[ACCOUNT_CONNECTED] 本轮账号接入已完成并通过核验；消费默认关闭。")
        else:
            self.say("\n[ACCOUNT_REVERIFIED] 原导入已核验，没有重写凭据。")

    def ready(self, alias, seconds):
        if not self.stdin.isatty() or not self.stderr.isatty():
            raise EzMcdError("sampling_interactive_terminal_required")
        self.say("EzMcDonalds 本机凭据采样 · 账号 " + alias)
        if not self.balance_only:
            raise EzMcdError("sampling_runtime_capture_suspended")
        self.say("本轮不修改系统代理。先在 Mac 官方 App 登录本人会员，确认麦钱包能正常显示。")
        self.say("请登录原账号的同一会员。本轮只更新会话，保留原消费策略和购买日志，不采集或延长下单运行字段。" if self.update_account else
                 "这里只连接当前登录的账号：不接收手机号或验证码，不创建订单，不启用消费。")
        self.say("关闭其他麦当劳窗口。" + ("开始后工具会尝试退出和重新打开已验证版本的 App。" if self.manage_app else "然后完全退出 App。"))
        self.say("仅匹配麦当劳进程的采样仍待实机验收，可能出现 macOS 网络扩展授权；失败会停止，不切换到系统代理。")
        self.say("开始后最多 " + str(seconds) + " 秒，只进入麦钱包；收到签名余额样本就收尾，无需准备购物车。")
        self.say("不要提交订单或付款，也不要测试拦截。若 App 要求登录，先结束采样，再在官方入口登录。")
        self.say("按回车开始；输入 q 或按 Ctrl-C 取消。整个窗口内直接看本终端提示，不必返回聊天逐步确认：")
        try:
            value = self.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            raise EzMcdError("sampling_cancelled_before_start") from None
        if not value or value.strip():
            raise EzMcdError("sampling_cancelled_before_start")

    def __call__(self, event):
        stage = event.get("stage")
        if stage == "APP_PREPARING":
            self.say("\n[APP_PREPARING] 正在核对并退出本人官方 App；不会修改系统代理。")
        elif stage == "GUARD_ACTIVE":
            action = "工具正在打开官方 App" if self.manage_app else "现在打开官方 App"
            self.say("\n[GUARD_ACTIVE] 本机采样已就绪，阻断自检已通过；系统代理未修改。" + action + "，先进入麦钱包余额页；暂时不要提交订单。")
        elif stage == "LOCAL_CAPTURE_STARTING":
            self.say("\n[LOCAL_CAPTURE_STARTING] 等待麦当劳进程采样就绪，系统代理保持原样。可能需要 macOS 网络扩展授权；未就绪前不要打开 App。")
        elif stage == "APP_RUNNING":
            self.say("\n[APP_RUNNING] 已观察到麦当劳进程。请在 App 窗口进入麦钱包，等待本终端的最终结果。")
        elif stage == "BALANCE_CAPTURED":
            self.say("\n[BALANCE_CAPTURED] 余额样本已校验，本轮正在收尾。不要再提交，请退出官方 App。")
        elif stage == "CLOSE_OFFICIAL_APP_NOW":
            action = "工具正在请求退出麦当劳。" if self.manage_app else "请立即退出麦当劳。"
            self.say("\n[CLOSE_OFFICIAL_APP_NOW] 窗口结束。" + action + "退出失败也会限时停止采样；不要提交订单或付款。")
        elif stage == "APP_MANUAL_CLOSE_REQUIRED":
            self.say("\n[APP_MANUAL_CLOSE_REQUIRED] 未能确认 App 和启动器均已退出。请用 ⌥⌘Esc 只退出麦当劳。本轮不再保证阻断，不要提交订单或付款；系统代理未修改。")
        elif stage == "CLEANUP_RESULT":
            if event.get("system_proxy_modified") is False and event.get("system_proxy_unchanged") is True and event.get("errors") == []:
                self.say("\n[CLEANUP_RESULT] 采样已结束，系统代理未修改且核对一致，持久证书保留。不能再沿用此前的采样提示。")
            elif event.get("proxy_restored") is True and event.get("errors") == []:
                self.say("\n[CLEANUP_RESULT] 代理已恢复，持久证书保留。本轮窗口已关闭，不能再沿用此前的提交提示。")
            else:
                self.say("\n[CLEANUP_RESULT] 采样清理尚未全部通过。保持麦当劳关闭，查看命令最终错误及本轮清理记录；不要提交订单。")
            directory = event.get("capture_directory")
            if isinstance(directory, str) and 0 < len(directory) <= 4096 and all(ord(c) >= 32 and ord(c) != 127 for c in directory):
                self.say("本轮私有采样目录：" + directory)
        elif stage == "CAPTURE_FINISHED":
            if event.get("sample_import_ready") is True and event.get("capture_state") == "complete":
                if self.balance_only:
                    self.say("\n[CAPTURE_FINISHED] 余额样本已校验，账号导入核验尚未完成。请继续等待最终结果。本次没有采集下单运行字段。")
                else:
                    self.say("\n[CAPTURE_FINISHED] 两个样本均已校验。若使用了 --update-account，请继续等待最终结果；只有 account_updated=true 或 already_imported=true 才表示凭据导入完成。")
            else:
                self.say("\n[CAPTURE_FINISHED] 本轮未取得完整样本，未更新账号。根据最终 capture_state 和 cleanup.json 处理；不要导入部分文件。")
