"""Field encryption used by the pinned official mobile-login implementation.

The legacy wire format is AES-128-ECB + PKCS7, lower-case hex. The key is read
locally from the supported client, never embedded in source or returned to MCP.
"""
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..errors import EzMcdError
from .macho import MachO
from .signing import constants


def login_key(binary):
    constants(binary)  # Validate the entire pinned build before reading its data.
    key = MachO(binary).describe(0x102b009f0).encode("utf-8")
    if len(key) != 16:
        raise EzMcdError("unsupported_login_encryption_key")
    return key


def encrypt_field(value, key):
    if not isinstance(value, str) or not value or len(value) > 128 or len(key) != 16:
        raise EzMcdError("invalid_login_field")
    pad = padding.PKCS7(128).padder()
    padded = pad.update(value.encode("utf-8")) + pad.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return (cipher.update(padded) + cipher.finalize()).hex()
