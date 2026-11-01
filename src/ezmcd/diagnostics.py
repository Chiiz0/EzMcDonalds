"""Local setup checks and account diagnosis without credential disclosure."""
import hashlib
from pathlib import Path
import platform

from . import __version__
from .accounts import public_account
from .errors import EzMcdError, error_result
from .protocol.signing import BUILD

GUIDE = "https://github.com/Chiiz0/EzMcDonalds/blob/main/docs/credentials.md"


def credentials_guide():
    return {"guide_url": GUIDE, "credentials_stay_on_your_device": True,
        "steps": [
            "运行 ezmcd doctor，检查官方客户端构建与已有账号。",
            "已有本人设备上下文：运行 ezmcd account login NEW_ALIAS --device-from EXISTING_ALIAS，在终端输入手机号和验证码。",
            "首次导入：在官方 App 登录，用本人本地调试工具复制麦钱包余额请求为 cURL 或原始 HTTP 请求。Mac 用户在私有终端运行 ezmcd account connect ALIAS --clipboard；项目只解析，不执行复制的命令。",
            "也可使用 account connect ALIAS --request-file 私有文本文件；原有 --har 入口保留。凭据不发到聊天，完成后清理复制内容及剪贴板历史。",
            "结束采样后恢复原代理；临时证书随即移除。需要重复采样的 Mac 用户可显式使用 sampling-cert 管理仅限 api.mcd.cn 的持久证书，不再需要时运行 sampling-cert remove。",
            "运行 ezmcd account check ALIAS；缺少下单运行字段时仍可找店、选餐、准备购物车与报价。",
            "下单运行字段可用 account runtime 单独更新，支持 --clipboard / --request-file；必须来自本人同会话的请求侧断点或已有授权样本。订单正文被丢弃，不重放。",
            "同设备上下文可显式进行只读验证复用，但不能从登录凭据独立生成，不能延长原采样日期。",
            "不要为获取运行样本额外创建订单；同设备复用已完成一笔真实订单，长期运行字段更新仍待完成。",
            "确认可用后由用户在本机设置消费策略，再将 ezmcd mcp-config 输出交给个人 Agent。"],
        "account_label_is_not_authentication": True, "automatic_device_initialization": False}


def doctor(service, alias=None, *, verify_session=False, binary="/Applications/McdApp.app/WrappedBundle/McdApp"):
    records = [service.store.account(alias)] if alias else service.store.accounts()
    builds = {}
    def check_binary(path):
        if path not in builds:
            try:
                source = Path(path).expanduser()
                if not source.is_file():
                    result = "not_found"
                elif source.stat().st_size > 512 * 1024 * 1024:
                    result = "unsupported"
                else:
                    with source.open("rb") as stream:
                        result = "supported" if hashlib.file_digest(stream, "sha256").hexdigest() == BUILD else "unsupported"
            except OSError:
                result = "not_readable"
            builds[path] = result
        return builds[path]
    accounts = []
    for record in records:
        view = public_account(record)
        view["client_build"] = "not_required_for_demo" if record["provider"] == "demo" else check_binary(record.get("binary", binary))
        if verify_session and not record.get("revoked"):
            try:
                view.update(service.check_account(record["alias"]))
            except EzMcdError as error:
                view["session_check"] = error_result(error.code)
                if error.code == "session_expired_reconnect_locally":
                    view["session_status"] = "expired_relogin_or_refresh"
                    view["readiness"]["checkout_blockers"] = [b for b in view["readiness"]["checkout_blockers"] if b != "session_not_checked"] + ["session_expired"]
            except (OSError, ValueError, KeyError, TypeError):
                view["session_check"] = error_result("invalid_local_configuration_or_response")
        actions = []
        if view.get("session_recovery"):
            actions.append(view["session_recovery"])
        elif view["session_status"] == "expired_relogin_or_refresh":
            actions.append("refresh_session_or_relogin_locally")
        if view["client_build"] not in ("supported", "not_required_for_demo"):
            actions.append("install_or_select_supported_official_client_build")
        if not view.get("device_context_available") and record["provider"] != "demo":
            actions.append("connect_your_own_official_device_context")
        if not view["runtime_available"] and record["provider"] != "demo":
            actions.append("update_official_runtime_evidence_before_checkout")
        if not record["policy"]["allow_checkout"]:
            actions.append("set_local_spending_policy_only_when_purchase_is_wanted")
        view["next_actions"] = actions
        accounts.append(view)
    return {"version": __version__, "platform": platform.system(), "session_checks_requested": verify_session,
        "default_client_build": check_binary(binary), "accounts": accounts,
        "next_step": "connect_your_own_account" if not accounts else "review_account_readiness",
        "credentials_guide": GUIDE, "order_created": False, "payment_attempted": False}
