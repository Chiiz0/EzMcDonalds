"""Minimal refresh-response evidence bound to a verified balance request.

This is local capture evidence, not a proof that a refresh token is redeemable.
Importing it never sends a refresh request.
"""
import re

from .errors import EzMcdError
from .models import fingerprint


def valid_credential(value):
    return (isinstance(value, str) and 0 < len(value) <= 16384
            and all(ord(c) >= 32 and ord(c) != 127 for c in value))


def validate_session(evidence, profile, config, audit):
    """Reject missing, mixed or malformed bindings before any network request."""
    fields = profile["fields"]
    if (not isinstance(evidence, dict)
            or set(evidence) != {"schema_version", "identity", "source_session", "target_session", "fields"}
            or type(evidence.get("schema_version")) is not int or evidence["schema_version"] != 1
            or not isinstance(evidence.get("fields"), dict)
            or set(evidence["fields"]) != {"sid", "refreshToken"}
            or not all(valid_credential(v) for v in evidence["fields"].values())
            or not isinstance(config.get("refresh_session"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", config["refresh_session"])
            or evidence["source_session"] != config["refresh_session"]
            or evidence["identity"] != config["identity"]
            or evidence["identity"] != fingerprint({"provider": "mcd-cn", "member": fields["meddyId"]})
            or evidence["fields"]["sid"] != fields["sid"]
            or evidence["target_session"] != fingerprint({"sid": fields["sid"], "token": fields["deviceToken"]})
            or type(audit.get("allowed_session_refreshes")) is not int or audit["allowed_session_refreshes"] != 1
            or audit.get("refresh_succeeded") is not True or audit.get("session_saved") is not True):
        raise EzMcdError("sampling_session_evidence_invalid")
    return evidence["fields"]["refreshToken"]
