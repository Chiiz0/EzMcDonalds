"""Wallet-only maintenance of an existing member, without collecting runtime."""
import http.client
import subprocess

from .accounts import _enroll, profile_from_balance_sample, public_account
from .errors import EzMcdError
from .models import fingerprint
from .protocol.signing import constants
from .provider import McdProvider
from .sampling_connect import AccountCapture
from .sampling_import import _capture_files, credential_fingerprint


def maintenance_account(store, alias):
    account = store.account(alias)
    if account.get("revoked") or account["provider"] != "mcd-cn":
        raise EzMcdError("active_official_account_required_for_session_maintenance")
    return account


class AccountMaintenanceCapture(AccountCapture):
    def run(self):
        account = maintenance_account(self.store, self.alias)
        if account["binary"] != self.binary:
            raise EzMcdError("sampling_member_mismatch")
        executable = self.executable_path()
        constants(self.binary)
        with self.store.lock(account["identity"]), self.ca.lease() as ca:
            if self.store.account(self.alias) != account:
                raise EzMcdError("sampling_account_changed_start_new_capture")
            try:
                return self._run(account, ca, executable, None)
            except subprocess.TimeoutExpired:
                raise EzMcdError("sampling_system_command_failed") from None
            except http.client.HTTPException:
                raise EzMcdError("sampling_guard_preflight_failed") from None

    def configuration(self, account, deadline, refresh_session):
        return super().configuration(account, deadline, None) | {
            "schema_version": 4, "purpose": "account_maintenance", "identity": account["identity"],
            "account_fingerprint": fingerprint(account)}

    def outcome(self, audit):
        result = super().outcome(audit) | {"capture_purpose": "account_maintenance", "account_updated": False}
        if result["sample_import_ready"]:
            result["next_action"] = "本人余额样本已核验，代理清理后可更新会话；不采集或延长下单运行字段，保留消费策略和日志。"
        return result


def import_maintenance(store, alias, directory):
    directory, files = _capture_files(store, directory, connection=True, maintenance=True)
    config = files["config"]
    if config["alias"] != alias:
        raise EzMcdError("sampling_connection_alias_mismatch")
    account = maintenance_account(store, alias)
    if config["identity"] != account["identity"] or config["binary"] != account["binary"]:
        raise EzMcdError("sampling_member_mismatch")
    profile = profile_from_balance_sample(files["balance"], account["binary"])
    identity = fingerprint({"provider": "mcd-cn", "member": profile["fields"]["meddyId"]})
    if identity != account["identity"] or files["audit"].get("captured_identity") != identity:
        raise EzMcdError("sampling_member_mismatch")
    receipt = {"purpose": "account_maintenance", "capture_id": directory.name,
               "evidence_fingerprint": fingerprint({"balance": files["balance"]})}
    previous = account.get("sampling_import", {})
    repeated = (all(previous.get(k) == v for k, v in receipt.items())
                and previous.get("credential_fingerprint") == credential_fingerprint(account))
    if not repeated and fingerprint(account) != config["account_fingerprint"]:
        raise EzMcdError("sampling_account_changed_start_new_capture")
    if repeated:
        with store.lock(identity):
            if store.account(alias) != account:
                raise EzMcdError("sampling_account_changed_start_new_capture")
            McdProvider(account).wallet()
        result = public_account(account, session_verified=True)
    else:
        result = _enroll(store, alias, profile, account["binary"], None, None, update=True,
            check_live=True, expected_account=config["account_fingerprint"], capture_receipt=receipt)
    return result | {"account_created": False, "account_updated": not repeated, "already_imported": repeated,
        "capture_directory": str(directory), "verification": ["wallet_balance_get"],
        "runtime_collected": False, "observation_time_preserved": True,
        "policy_preserved": True, "journal_preserved": True, "spending_authorized_by_connection": False,
        "order_created": False, "payment_attempted": False, "cart_changed": False,
        "cross_day_validity_verified": False,
        "next_action": "会话已核验并保留原策略和购买日志。检查账号运行字段状态：可恢复时先只读核验，缺失或过期时另行采样。本次未增加消费授权。"}
