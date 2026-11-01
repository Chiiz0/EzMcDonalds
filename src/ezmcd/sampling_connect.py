"""Local first connection from the user's already logged-in official App.

Only a signed successful balance request is retained. All upstream mutations,
including refresh and login, remain blocked. No runtime or spending authority
is granted by this path; existing accounts must use their maintenance flow.
"""
import http.client
from pathlib import Path
import subprocess

from .accounts import _enroll, profile_from_balance_sample, public_account
from .errors import EzMcdError
from .models import fingerprint
from .protocol.signing import constants
from .provider import McdProvider
from .sampling import RuntimeCapture, capture_outcome
from .sampling_import import _capture_files, credential_fingerprint

CONNECT_LOCK = fingerprint({"purpose": "first-official-account-connection"})


def require_new_alias(store, alias):
    try:
        store.account(alias)
    except EzMcdError as error:
        if error.code == "account_not_connected":
            return
        raise
    raise EzMcdError("account_exists_use_same_member_maintenance")


class AccountCapture(RuntimeCapture):
    balance_only = True

    def __init__(self, store, alias, *, binary, **kwargs):
        if kwargs.get("refresh_from"):
            raise EzMcdError("sampling_connection_scope_invalid")
        super().__init__(store, alias, **kwargs)
        self.binary = str(Path(binary).expanduser().absolute())

    def run(self):
        require_new_alias(self.store, self.alias)
        executable = self.executable_path()
        constants(self.binary)
        with self.store.lock(CONNECT_LOCK), self.ca.lease() as ca:
            require_new_alias(self.store, self.alias)
            try:
                return self._run({"binary": self.binary}, ca, executable, None)
            except subprocess.TimeoutExpired:
                raise EzMcdError("sampling_system_command_failed") from None
            except http.client.HTTPException:
                raise EzMcdError("sampling_guard_preflight_failed") from None

    def configuration(self, account, deadline, refresh_session):
        return {"output": str(self.directory), "binary": self.binary, "identity": None,
            "deadline": deadline, "refresh_session": None, "schema_version": 3,
            "purpose": "account_connect", "alias": self.alias}

    def outcome(self, audit):
        result = capture_outcome(audit)
        if audit.get("balance_saved") is True:
            result.update(capture_state="complete", sample_import_ready=True, new_sampling_required=False,
                next_action="余额请求已核验，代理清理后可导入新账号。没有采集下单运行字段，也未授权消费。")
        return result | {"capture_purpose": "account_connect", "runtime_collected": False,
                         "account_created": False}


def import_connection(store, alias, directory):
    directory, files = _capture_files(store, directory, connection=True)
    config = files["config"]
    if alias != config["alias"]:
        raise EzMcdError("sampling_connection_alias_mismatch")
    profile = profile_from_balance_sample(files["balance"], config["binary"])
    identity = fingerprint({"provider": "mcd-cn", "member": profile["fields"]["meddyId"]})
    if files["audit"].get("captured_identity") != identity:
        raise EzMcdError("sampling_member_mismatch")
    receipt = {"purpose": "account_connect", "capture_id": directory.name,
               "evidence_fingerprint": fingerprint({"balance": files["balance"]})}
    # Registration itself checks alias and member uniqueness again atomically.
    # A repeat can only verify its unchanged original connection, never replace
    # later credentials, reactivate a revoked account or reset an old ledger.
    with store.lock(CONNECT_LOCK):
        matches = [r for r in store.accounts() if r["alias"] == alias or r["identity"] == identity]
        if matches:
            account = matches[0]
            previous = account.get("sampling_import", {})
            if (len(matches) != 1 or account["alias"] != alias or account["identity"] != identity
                    or account.get("revoked") or account.get("binary") != config["binary"]
                    or any(previous.get(k) != v for k,v in receipt.items())
                    or previous.get("credential_fingerprint") != credential_fingerprint(account)):
                raise EzMcdError("account_exists_use_same_member_maintenance")
            with store.lock(identity):
                if store.account(alias) != account:
                    raise EzMcdError("sampling_account_changed_start_new_capture")
                McdProvider(account).wallet()
            result = public_account(account, session_verified=True)
        else:
            result = _enroll(store, alias, profile, config["binary"], None, None,
                             check_live=True, capture_receipt=receipt)
    return result | {"account_created": not bool(matches), "account_updated": False,
        "already_imported": bool(matches), "capture_directory": str(directory),
        "verification": ["wallet_balance_get"], "runtime_collected": False,
        "spending_authorized_by_connection": False, "order_created": False, "payment_attempted": False,
        "cart_changed": False, "next_action": "账号会话已核验，可连接MCP浏览和报价。首次接入默认关闭消费；下单运行字段需另行采样。"}
