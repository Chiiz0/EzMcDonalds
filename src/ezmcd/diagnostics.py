"""Local setup checks and account diagnosis without credential disclosure."""
import hashlib
from pathlib import Path
import platform
import time

from . import __version__
from .accounts import public_account
from .errors import EzMcdError, error_result
from .protocol.signing import BUILD

GUIDE = "https://github.com/Chiiz0/EzMcDonalds/blob/main/docs/credentials.md"


def runtime_guidance(record, now, *, account_recovery_required=False):
    """Explain local choices, without granting permission or validating upstream."""
    from .runtime_recovery import device_context, recovery_available
    from .runtime_use import DEFAULT_HOURS, MAX_HOURS, active, evidence_age
    age = evidence_age(record, now)
    result = {"state": None, "checked_at": now, "observed_at": None,
        "usable_until": None, "remaining_seconds": None, "explicit_reuse_option": None,
        "local_conditions_only": True, "readonly_validation_performed": False,
        "observation_time_changed": False, "permission_changed": False,
        "spending_authorized": False, "use_same_data_directory": True}
    if record["provider"] == "demo":
        result["state"] = "not_required_for_demo"
    elif account_recovery_required or record.get("revoked") or record.get("session_refresh", {}).get("state") in ("CLAIMED", "CANDIDATE", "STOPPED"):
        result["state"] = "complete_account_recovery_first"
    elif device_context(record) is None or not record.get("profile", {}).get("fields", {}).get("sid"):
        result["state"] = "connect_own_device_context_first"
    elif recovery_available(record, now) and (age is None or age > DEFAULT_HOURS * 3600 and not active(record, now)):
        result["state"] = "restore_previous_own_runtime_readonly"
    elif age is None:
        result["state"] = "new_official_evidence_required"
    else:
        observed = record["runtime"]["observed_at"]
        result["observed_at"] = observed
        if active(record, now):
            result["state"] = "available_under_explicit_permission"
            result["usable_until"] = record["runtime_use"]["expires_at"]
        elif age <= DEFAULT_HOURS * 3600:
            result["state"] = "available_under_default_limit"
            result["usable_until"] = observed + DEFAULT_HOURS * 3600
        elif age < MAX_HOURS * 3600:
            # Integer hours must end strictly after this snapshot. Choosing a
            # duration is left to the owner; do not suggest silently using max.
            result["state"] = "explicit_reuse_choice_available"
            result["explicit_reuse_option"] = {
                "minimum_max_age_hours": max(DEFAULT_HOURS + 1, int(age // 3600) + 1),
                "maximum_max_age_hours": MAX_HOURS,
                "latest_possible_expiry": observed + MAX_HOURS * 3600,
                "maximum_remaining_seconds": max(0, int(MAX_HOURS * 3600 - age)),
                "requires_local_user_decision": True, "requires_readonly_validation": True,
                "cli_argv_template": ["ezmcd", "account", "runtime-use", record["alias"],
                                      "--max-age-hours", "<chosen_hours>"],
                "guide_url": "https://github.com/Chiiz0/EzMcDonalds/blob/main/docs/runtime-use.md"}
        else:
            result["state"] = "new_official_evidence_required"
        if result["usable_until"] is not None:
            result["remaining_seconds"] = max(0, int(result["usable_until"] - now))
    result["message"] = {
        "not_required_for_demo": "离线演示不使用官方运行字段。",
        "complete_account_recovery_first": "先完成当前账号恢复，再检查运行样本；刷新结果未知时不要重复刷新。",
        "connect_own_device_context_first": "先在本机接入本人的完整官方设备上下文；不能仅凭手机号生成运行字段。",
        "restore_previous_own_runtime_readonly": "可只读核验并恢复本人原设备的近期样本，原采样日期不变。",
        "new_official_evidence_required": "需要新的本人官方运行请求证据；登录或进入钱包不能更新运行样本，不要为取样额外提交订单。",
        "available_under_default_limit": "运行样本仍在默认期限内；报价、会话和消费授权仍需分别核对。",
        "available_under_explicit_permission": "原运行样本仍在已授权期限内，每单仍会重新只读核验；到期后不会自动延长。",
        "explicit_reuse_choice_available": "原运行样本已超过当前许可；可由本人选择有限复用期限并只读核验，也可导入新的本人证据。这里没有授予复用或消费权限。",
    }[result["state"]]
    return result


def credentials_guide():
    return {"guide_url": GUIDE, "credentials_stay_on_your_device": True,
        "steps": [
            "运行 ezmcd doctor，检查官方客户端构建与已有账号。",
            "已有本人设备上下文：运行 ezmcd account login NEW_ALIAS --device-from EXISTING_ALIAS，在终端输入手机号和验证码。",
            "首次接入可实验性使用 sampling-connect ALIAS：仅匹配本人Mac麦当劳可执行文件，只进入钱包，不修改系统代理；首次接入和已有账号维护已实机通过，空白机器安装未验收。不要测试提交订单。需要受限证书，可能需要首次网络扩展授权。",
            "首次导入：在官方 App 登录，用本人本地调试工具复制麦钱包余额请求为 cURL 或原始 HTTP 请求。Mac 用户在私有终端运行 ezmcd account connect ALIAS --clipboard；项目只解析，不执行复制的命令。",
            "也可使用 account connect ALIAS --request-file 私有文本文件；原有 --har 入口保留。凭据不发到聊天，完成后清理复制内容及剪贴板历史。",
            "结束采样后恢复原代理；临时证书随即移除。需要重复采样的 Mac 用户可显式使用 sampling-cert 管理仅限 api.mcd.cn 的持久证书，不再需要时运行 sampling-cert remove。",
            "运行 ezmcd account check ALIAS；缺少下单运行字段时仍可找店、选餐、准备购物车与报价。",
            "已有账号只更新会话：先在官方App登录同一会员，再在原数据目录运行 sampling-connect ALIAS --update --manage-app，只进入钱包即可。保留策略和购买日志，不延长运行字段；失败可用 sampling-import ALIAS CAPTURE_DIRECTORY --connect --update 继续。",
            "重新登录或刷新后，若 runtime_recovery_available=true，可运行 account runtime ALIAS --previous-device，或让 Agent 调用 account_runtime_restore；只读核验本人原设备的近期样本，保留原采样日期。",
            "下单运行字段可用 account runtime 单独更新，支持 --clipboard / --request-file；必须来自本人同会话的请求侧断点或已有授权样本。订单正文被丢弃，不重放。",
            "旧版 sampling-capture 因系统代理影响整机网络已停用。已有完整历史样本仍可sampling-import；缺少清理证据的失败目录不能导入。sampling-status只读报告网络状态，不修复或启动代理。",
            "同设备上下文可显式进行只读验证复用，但不能从登录凭据独立生成，不能延长原采样日期。",
            "已有当前账号运行样本时，本机用户可选择 account runtime-use ALIAS --max-age-hours HOURS（25至336，从原采样时刻计算）。先做只读核验，历史字段每次下单前再核验；MCP不能开启或延长许可，消费仍需单独授权。--disable撤销复用，不联网。约10天的本人样本已完成一笔真实下单和麦钱包付款；其他年龄、设备及连续多日消费仍未验证。",
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
        view["runtime_guidance"] = runtime_guidance(record, time.time(),
            account_recovery_required=view["session_status"] == "expired_relogin_or_refresh")
        if view.get("session_recovery"):
            actions.append(view["session_recovery"])
        elif view["session_status"] == "expired_relogin_or_refresh":
            actions.append("refresh_session_or_relogin_locally")
        if view["client_build"] not in ("supported", "not_required_for_demo"):
            actions.append("install_or_select_supported_official_client_build")
        runtime_state = view["runtime_guidance"]["state"]
        if (not view.get("device_context_available") or runtime_state == "connect_own_device_context_first") and record["provider"] != "demo":
            actions.append("connect_your_own_official_device_context")
        if not view["runtime_available"] and record["provider"] != "demo" and runtime_state not in (
                "complete_account_recovery_first", "connect_own_device_context_first"):
            actions.append("restore_previous_own_runtime_readonly" if view["runtime_recovery_available"]
                           else "choose_explicit_runtime_reuse_or_update_evidence_locally"
                           if runtime_state == "explicit_reuse_choice_available"
                           else "update_official_runtime_evidence_before_checkout")
        if not record["policy"]["allow_checkout"]:
            actions.append("set_local_spending_policy_only_when_purchase_is_wanted")
        if "checkout_policy_expired" in view["readiness"]["checkout_blockers"]:
            actions.append("review_existing_orders_before_new_local_spending_authorization")
            help_text = error_result("checkout_policy_expired")
            view["expired_policy_guidance"] = {"message": help_text["message"],
                "next_action": help_text["next_action"], "permission_changed": False,
                "local_orders_tool": {"name": "orders_list", "arguments": {"account": record["alias"]}}}
        view["next_actions"] = actions
        accounts.append(view)
    return {"version": __version__, "platform": platform.system(), "session_checks_requested": verify_session,
        "default_client_build": check_binary(binary), "accounts": accounts,
        "next_step": "connect_your_own_account" if not accounts else "review_account_readiness",
        "credentials_guide": GUIDE, "order_created": False, "payment_attempted": False}
