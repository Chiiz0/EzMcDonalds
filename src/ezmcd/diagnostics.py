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
            "首次接入也可尝试 sampling-connect ALIAS：先在支持的Mac官方App登录，在本机终端按步骤只进入麦钱包；全程阻断写请求，代理恢复后核验并保存，消费默认关闭且不含运行字段。需要本人受限采样证书；一个已有会员的新App采样、自动开关、代理恢复及首次导入已实测，用户仍需进入钱包。",
            "首次导入：在官方 App 登录，用本人本地调试工具复制麦钱包余额请求为 cURL 或原始 HTTP 请求。Mac 用户在私有终端运行 ezmcd account connect ALIAS --clipboard；项目只解析，不执行复制的命令。",
            "也可使用 account connect ALIAS --request-file 私有文本文件；原有 --har 入口保留。凭据不发到聊天，完成后清理复制内容及剪贴板历史。",
            "结束采样后恢复原代理；临时证书随即移除。需要重复采样的 Mac 用户可显式使用 sampling-cert 管理仅限 api.mcd.cn 的持久证书，不再需要时运行 sampling-cert remove。",
            "运行 ezmcd account check ALIAS；缺少下单运行字段时仍可找店、选餐、准备购物车与报价。",
            "已有账号只更新会话：先在官方App登录同一会员，再在原数据目录运行 sampling-connect ALIAS --update --manage-app，只进入钱包即可。保留策略和购买日志，不延长运行字段；失败可用 sampling-import ALIAS CAPTURE_DIRECTORY --connect --update 继续。",
            "重新登录或刷新后，若 runtime_recovery_available=true，可运行 account runtime ALIAS --previous-device，或让 Agent 调用 account_runtime_restore；只读核验本人原设备的近期样本，保留原采样日期。",
            "下单运行字段可用 account runtime 单独更新，支持 --clipboard / --request-file；必须来自本人同会话的请求侧断点或已有授权样本。订单正文被丢弃，不重放。",
            "已有 Mac 账号可在本机终端使用 sampling-capture ALIAS --interactive --update-account：准备好按回车后，按同一终端的实时中文提示操作。可加 --manage-app 处理已验证 App 的开关，页面仍由本人操作。复用受限证书、限时阻断订单，退出 App 并恢复代理后只读核验和导入。失败可用 sampling-import ALIAS CAPTURE_DIRECTORY 恢复；不通过 MCP 控制采样。",
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
            actions.append("restore_previous_own_runtime_readonly" if view["runtime_recovery_available"]
                           else "update_official_runtime_evidence_before_checkout")
        if not record["policy"]["allow_checkout"]:
            actions.append("set_local_spending_policy_only_when_purchase_is_wanted")
        view["next_actions"] = actions
        accounts.append(view)
    return {"version": __version__, "platform": platform.system(), "session_checks_requested": verify_session,
        "default_client_build": check_binary(binary), "accounts": accounts,
        "next_step": "connect_your_own_account" if not accounts else "review_account_readiness",
        "credentials_guide": GUIDE, "order_created": False, "payment_attempted": False}
