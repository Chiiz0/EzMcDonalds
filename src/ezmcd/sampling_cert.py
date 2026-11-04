"""Explicit, local macOS sampling CA lifecycle. Never changes a proxy.

Each installation owns a different key. User trust is limited to SSL for one
hostname; no root or key is distributed with the project or exposed over MCP.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .errors import EzMcdError
from .models import fingerprint
from .storage import read_private, write_private

HOST = "api.mcd.cn"
LOCK = fingerprint({"purpose": "local-sampling-certificate"})
CERT = "mitmproxy-ca-cert.pem"
KEY = "mitmproxy-ca.pem"


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _read(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise EzMcdError("symlink_private_file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.getuid() or info.st_size > 65536):
            raise EzMcdError("private_file_permissions_required")
        return stream.read()


class SamplingCertificate:
    def __init__(self, store):
        self.store = store
        self.directory = store.root / "sampling-ca"
        self.keychain = Path.home() / "Library/Keychains/login.keychain-db"

    @contextmanager
    def _lock(self):
        try:
            with self.store.lock(LOCK):
                yield
        except EzMcdError as error:
            if error.code == "account_busy_try_status_only":
                raise EzMcdError("sampling_certificate_in_use") from None
            raise

    def _load(self):
        metadata = read_private(self.directory / "metadata.json")
        cert = x509.load_pem_x509_certificate(_read(self.directory / CERT))
        if (metadata.get("host") != HOST
                or cert.fingerprint(hashes.SHA256()).hex() != metadata.get("sha256")
                or cert.fingerprint(hashes.SHA1()).hex() != metadata.get("sha1")
                or cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != metadata.get("common_name")
                or cert.not_valid_after_utc.isoformat() != metadata.get("expires_at")):
            raise EzMcdError("sampling_certificate_changed")
        bundle = _read(self.directory / KEY)
        key = serialization.load_pem_private_key(bundle, password=None)
        embedded = x509.load_pem_x509_certificate(bundle)
        if (key.public_key().public_numbers() != cert.public_key().public_numbers()
                or embedded != cert):
            raise EzMcdError("sampling_certificate_changed")
        return metadata, cert, key

    def prepare(self):
        with self._lock():
            if self.directory.exists():
                metadata, _, _ = self._load()
                return self._summary(metadata) | {"created": False}
            temporary = Path(tempfile.mkdtemp(prefix=".sampling-ca-", dir=self.store.root))
            try:
                now = datetime.now(timezone.utc)
                key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
                label = "EzMcDonalds Local Sampling " + os.urandom(6).hex()
                subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
                cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1825))
                    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                    .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                        key_encipherment=False, data_encipherment=False, key_agreement=False,
                        key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                    .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(HOST)],
                        excluded_subtrees=[x509.IPAddress(ipaddress.ip_network("0.0.0.0/0")),
                                           x509.IPAddress(ipaddress.ip_network("::/0"))]), critical=True)
                    .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                    .sign(key, hashes.SHA256()))
                pem = cert.public_bytes(serialization.Encoding.PEM)
                _write(temporary / CERT, pem)
                _write(temporary / KEY, key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()) + pem)
                metadata = {"host": HOST, "common_name": label,
                    "sha256": cert.fingerprint(hashes.SHA256()).hex(),
                    "sha1": cert.fingerprint(hashes.SHA1()).hex(),
                    "expires_at": cert.not_valid_after_utc.isoformat(), "created_at": now.isoformat()}
                write_private(temporary / "metadata.json", metadata)
                temporary.rename(self.directory)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            return self._summary(metadata) | {"created": True}

    def _summary(self, metadata):
        return {"host": HOST, "certificate_sha256": metadata["sha256"],
            "expires_at": metadata["expires_at"], "ca_directory": str(self.directory),
            "trust_scope": "current macOS user; SSL server api.mcd.cn only",
            "proxy_changed": False, "login_validity_changed": False}

    @staticmethod
    def _command(args):
        if sys.platform != "darwin":
            raise EzMcdError("sampling_certificate_macos_required")
        # Never print keychain output or put private key contents in argv.
        return subprocess.run(["/usr/bin/security", *args], capture_output=True, text=True)

    def _present(self, metadata):
        result = self._command(["find-certificate", "-a", "-Z", "-c", metadata["common_name"], str(self.keychain)])
        if result.returncode not in (0, 44):  # errSecItemNotFound is -25300, shell exit 44.
            raise EzMcdError("sampling_certificate_keychain_unreadable")
        return metadata["sha1"].upper() in re.findall(r"SHA-1 hash: ([0-9A-Fa-f]+)", result.stdout.upper(), re.I)

    def _probe(self, cert, key):
        """Evaluate actual user trust offline; never pass an explicit trust root."""
        results = {}
        with tempfile.TemporaryDirectory(prefix=".sampling-probe-", dir=self.store.root) as temporary:
            folder = Path(temporary)
            leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            for host in (HOST, "outside.example", "child." + HOST):
                now = datetime.now(timezone.utc)
                leaf = (x509.CertificateBuilder().subject_name(x509.Name([
                        x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                    .issuer_name(cert.subject).public_key(leaf_key.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                    .not_valid_after(now + timedelta(days=1))
                    .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                    .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                    .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
                    .sign(key, hashes.SHA256()))
                path = folder / (host + ".pem")
                _write(path, leaf.public_bytes(serialization.Encoding.PEM))
                result = self._command(["verify-cert", "-c", str(path), "-c", str(self.directory / CERT),
                    "-p", "ssl", "-n", host, "-k", str(self.keychain), "-L", "-q"])
                results[host] = result.returncode == 0
        return results

    def status(self):
        with self._lock():
            if not self.directory.exists():
                return {"prepared": False, "installed": False, "proxy_changed": False}
            metadata, cert, key = self._load()
            probes = self._probe(cert, key)
            restricted = probes == {HOST: True, "outside.example": False, "child." + HOST: False}
            return self._summary(metadata) | {"prepared": True, "installed": self._present(metadata),
                "trusted_for_sampling": restricted, "offline_trust_checks": probes}

    def install(self):
        with self._lock():
            metadata, cert, key = self._load()
            if cert.not_valid_after_utc <= datetime.now(timezone.utc) + timedelta(days=1):
                raise EzMcdError("sampling_certificate_expired")
            probes = self._probe(cert, key)
            expected = {HOST: True, "outside.example": False, "child." + HOST: False}
            if probes == expected:
                return self._summary(metadata) | {"installed": True, "reused": True, "offline_trust_checks": probes}
            if probes["outside.example"] or probes["child." + HOST]:
                raise EzMcdError("sampling_certificate_trust_too_broad")
            result = self._command(["add-trusted-cert", "-r", "trustRoot", "-p", "ssl", "-s", HOST,
                "-k", str(self.keychain), str(self.directory / CERT)])
            if result.returncode:
                raise EzMcdError("sampling_certificate_authorization_not_completed")
            probes = self._probe(cert, key)
            if probes != expected:
                # Revoke trust added by this operation; keep local material for
                # inspection/removal if the OS cannot verify the narrow scope.
                if self._command(["remove-trusted-cert", str(self.directory / CERT)]).returncode:
                    raise EzMcdError("sampling_certificate_removal_not_completed")
                raise EzMcdError("sampling_certificate_scope_verification_failed")
            return self._summary(metadata) | {"installed": True, "reused": False, "offline_trust_checks": probes}

    def remove(self):
        with self._lock():
            if not self.directory.exists():
                return {"removed": True, "already_absent": True, "proxy_changed": False}
            metadata, cert, key = self._load()
            if self._present(metadata):
                result = self._command(["remove-trusted-cert", str(self.directory / CERT)])
                if result.returncode and any(self._probe(cert, key).values()):
                    raise EzMcdError("sampling_certificate_removal_not_completed")
                result = self._command(["delete-certificate", "-Z", metadata["sha1"], str(self.keychain)])
                if result.returncode or self._present(metadata):
                    raise EzMcdError("sampling_certificate_removal_not_completed")
            if any(self._probe(cert, key).values()):
                raise EzMcdError("sampling_certificate_removal_not_completed")
            # Never delete unknown files or user-provided symlinks recursively.
            expected = {CERT, KEY, "metadata.json", "mitmproxy-dhparam.pem"}
            if any(p.name not in expected or not p.is_file() or p.is_symlink() for p in self.directory.iterdir()):
                raise EzMcdError("sampling_certificate_directory_contains_unknown_files")
            for path in self.directory.iterdir():
                path.unlink()
            self.directory.rmdir()
            return {"removed": True, "private_key_removed": True, "proxy_changed": False}

    @contextmanager
    def server_certificate(self):
        """One DNS-only leaf for local interception; never widens CA trust.

        Local-mode destinations are IP addresses. mitmproxy's default leaf
        includes that address as a SAN, which our CA intentionally excludes.
        An exact-host certificate avoids adding IP authority to the CA.
        The caller holds lease() until capture and key cleanup both finish.
        """
        _, ca, ca_key = self._load()
        now = datetime.now(timezone.utc)
        if ca.not_valid_after_utc <= now + timedelta(days=1):
            raise EzMcdError("sampling_certificate_expired")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOST)]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
        with tempfile.TemporaryDirectory(prefix=".sampling-leaf-", dir=self.store.root) as temporary:
            path = Path(temporary) / "api.pem"
            _write(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()) + cert.public_bytes(serialization.Encoding.PEM)
                + ca.public_bytes(serialization.Encoding.PEM))
            yield path

    @contextmanager
    def lease(self):
        """Hold across a local capture so revoke/replace cannot race its cleanup."""
        with self._lock():
            _, cert, key = self._load()
            if self._probe(cert, key) != {HOST: True, "outside.example": False, "child." + HOST: False}:
                raise EzMcdError("sampling_certificate_scope_verification_failed")
            yield self.directory
