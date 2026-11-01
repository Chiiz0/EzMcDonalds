"""Human-readable guidance for an explicitly started local capture terminal.

This module never starts an App, touches proxies, or receives credentials.
Machine-readable final results stay on stdout; prompts and stages use stderr.
"""
import sys

from .errors import EzMcdError


class SamplingConsole:
    def __init__(self, *, stdin=None, stderr=None):
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stderr = stderr if stderr is not None else sys.stderr

    def say(self, text):
        print(text, file=self.stderr, flush=True)

    def ready(self, alias, seconds):
        if not self.stdin.isatty() or not self.stderr.isatty():
            raise EzMcdError("sampling_interactive_terminal_required")
        self.say("EzMcDonalds 本机凭据采样 · 账号 " + alias)
        self.say("现在尚未启用代理。先在官方 App 登录同一会员、准备测试购物车，再完全退出 App。")
        self.say("准备时先核对门店和取餐时间，确认 App 内看得到测试商品；重新打开后沿用同一门店和时间。")
        self.say("开始后最多 " + str(seconds) + " 秒：看到保护已启动才打开 App，先进入麦钱包；看到余额已校验才进入受保护的提交步骤。")
        self.say("订单会在发送前被阻断；不要付款或重试。窗口结束后退出 App，等待代理恢复。")
        self.say("按回车开始；输入 q 或按 Ctrl-C 取消。整个窗口内直接看本终端提示，不必返回聊天逐步确认：")
        try:
            value = self.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            raise EzMcdError("sampling_cancelled_before_start") from None
        if not value or value.strip():
            raise EzMcdError("sampling_cancelled_before_start")

    def __call__(self, event):
        stage = event.get("stage")
        if stage == "GUARD_ACTIVE":
            self.say("\n[GUARD_ACTIVE] 保护已启动，发送前阻断自检已通过。现在打开官方 App，先进入麦钱包余额页；暂时不要提交订单。")
        elif stage == "BALANCE_CAPTURED":
            seconds = event.get("seconds_remaining")
            if event.get("capture_window_active") is True and type(seconds) is int and seconds > 0:
                self.say("\n[BALANCE_CAPTURED] 余额已校验，窗口剩余约 " + str(seconds) + " 秒。")
                self.say("现在可进入已准备购物车的确认页，在当前保护窗口内提交一次；若再确认餐厅，只确认一次。请求会被阻断，不要重试或付款。")
            else:
                self.say("\n[BALANCE_CAPTURED] 余额样本已校验，但本轮正在收尾。不要再提交，请退出官方 App。")
        elif stage == "CLOSE_OFFICIAL_APP_NOW":
            self.say("\n[CLOSE_OFFICIAL_APP_NOW] 窗口结束：不要继续提交。请立即退出麦当劳；若卡住，用 ⌥⌘Esc 只强制退出麦当劳。退出前阻断继续保留。")
        elif stage == "CLEANUP_RESULT":
            if event.get("proxy_restored") is True and event.get("errors") == []:
                self.say("\n[CLEANUP_RESULT] 代理已恢复，持久证书保留。本轮窗口已关闭，不能再沿用此前的提交提示。")
            else:
                self.say("\n[CLEANUP_RESULT] 代理清理尚未全部通过。保持麦当劳关闭，查看命令最终错误及本轮清理记录；不要提交订单。")
        elif stage == "CAPTURE_FINISHED":
            if event.get("sample_import_ready") is True and event.get("capture_state") == "complete":
                self.say("\n[CAPTURE_FINISHED] 两个样本均已校验。若使用了 --update-account，请继续等待最终结果；只有 account_updated=true 或 already_imported=true 才表示凭据导入完成。")
            else:
                self.say("\n[CAPTURE_FINISHED] 本轮未取得完整样本，未更新账号。先确认代理已恢复，再根据最终 capture_state 处理；不要导入部分文件。")
