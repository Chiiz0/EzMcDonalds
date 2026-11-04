from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from ezmcd.errors import EzMcdError
from ezmcd.sampling_cert import SamplingCertificate, HOST, CERT, KEY
from ezmcd.storage import Store, write_private


class SamplingCertificateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name).resolve() / "data")
        self.ca = SamplingCertificate(self.store)
        self.ca.prepare()
        self.none = {HOST: False, "outside.example": False, "child." + HOST: False}
        self.scoped = self.none | {HOST: True}

    def test_private_unique_scoped_ca_and_idempotent_prepare(self):
        first = (self.ca.directory / KEY).read_bytes()
        result = self.ca.prepare()
        self.assertFalse(result["created"])
        self.assertEqual(first, (self.ca.directory / KEY).read_bytes())
        self.assertEqual(self.ca.directory.stat().st_mode & 0o777, 0o700)
        for file in self.ca.directory.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        cert = x509.load_pem_x509_certificate((self.ca.directory / CERT).read_bytes())
        self.assertGreater(cert.not_valid_after_utc, datetime.now(timezone.utc))
        constraints = cert.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertTrue(constraints.critical)
        self.assertEqual(constraints.value.permitted_subtrees, [x509.DNSName(HOST)])
        other = SamplingCertificate(Store(self.store.root / "other"))
        self.assertNotEqual(result["certificate_sha256"], other.prepare()["certificate_sha256"])

    def test_tampered_metadata_and_symlinks_are_rejected(self):
        metadata, _, _ = self.ca._load()
        write_private(self.ca.directory / "metadata.json", metadata | {"host": "outside.example"})
        with self.assertRaisesRegex(EzMcdError, "sampling_certificate_changed"):
            self.ca.prepare()
        write_private(self.ca.directory / "metadata.json", metadata)
        original = self.ca.directory / KEY
        original.rename(self.ca.directory / "other-key")
        original.symlink_to("other-key")
        with self.assertRaisesRegex(EzMcdError, "symlink_private_file"):
            self.ca.prepare()

    def test_install_trusts_only_current_user_ssl_hostname_then_verifies(self):
        command = Mock(return_value=SimpleNamespace(returncode=0))
        with patch.object(self.ca, "_probe", side_effect=[self.none, self.scoped]), patch.object(self.ca, "_command", command):
            result = self.ca.install()
        args = command.call_args.args[0]
        self.assertEqual(args[:7], ["add-trusted-cert", "-r", "trustRoot", "-p", "ssl", "-s", HOST])
        self.assertNotIn("-d", args)
        self.assertNotIn("-e", args)
        self.assertTrue(result["installed"])
        self.assertFalse(result["reused"])

    def test_reuse_checks_real_trust_without_another_authorization(self):
        with patch.object(self.ca, "_probe", return_value=self.scoped), patch.object(self.ca, "_command") as command:
            self.assertTrue(self.ca.install()["reused"])
            command.assert_not_called()

    def test_broad_existing_trust_is_never_silently_accepted(self):
        with patch.object(self.ca, "_probe", return_value=self.scoped | {"outside.example": True}), patch.object(self.ca, "_command") as command:
            with self.assertRaisesRegex(EzMcdError, "sampling_certificate_trust_too_broad"):
                self.ca.install()
            command.assert_not_called()

    def test_denied_install_preserves_material_and_does_not_claim_success(self):
        with patch.object(self.ca, "_probe", return_value=self.none), patch.object(self.ca, "_command", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(EzMcdError, "sampling_certificate_authorization_not_completed"):
                self.ca.install()
        self.assertTrue((self.ca.directory / KEY).exists())

    def test_failed_scope_check_is_visible(self):
        with patch.object(self.ca, "_probe", return_value=self.none), patch.object(self.ca, "_command", return_value=SimpleNamespace(returncode=0)):
            with self.assertRaisesRegex(EzMcdError, "sampling_certificate_scope_verification_failed"):
                self.ca.install()

    def test_remove_uses_exact_fingerprint_then_removes_private_key(self):
        metadata, _, _ = self.ca._load()
        command = Mock(return_value=SimpleNamespace(returncode=0))
        with patch.object(self.ca, "_present", side_effect=[True, False]), patch.object(self.ca, "_probe", return_value=self.none), patch.object(self.ca, "_command", command):
            self.assertTrue(self.ca.remove()["private_key_removed"])
        self.assertEqual(command.call_args_list[1].args[0][:3], ["delete-certificate", "-Z", metadata["sha1"]])
        self.assertFalse(self.ca.directory.exists())
        self.assertTrue(self.ca.remove()["already_absent"])

    def test_failed_removal_preserves_certificate_and_private_key_for_retry(self):
        with patch.object(self.ca, "_present", return_value=True), patch.object(self.ca, "_probe", return_value=self.scoped), patch.object(self.ca, "_command", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(EzMcdError, "sampling_certificate_removal_not_completed"):
                self.ca.remove()
        self.assertTrue((self.ca.directory / KEY).exists())

    def test_unknown_files_are_not_deleted(self):
        extra = self.ca.directory / "keep-me.txt"
        extra.write_text("user data")
        with patch.object(self.ca, "_present", return_value=False), patch.object(self.ca, "_probe", return_value=self.none):
            with self.assertRaisesRegex(EzMcdError, "sampling_certificate_directory_contains_unknown_files"):
                self.ca.remove()
        self.assertTrue(extra.exists())

    def test_probe_uses_local_os_trust_without_forcing_the_root(self):
        _, cert, key = self.ca._load()
        checked = []
        def command(args):
            self.assertIn("-L", args)
            self.assertNotIn("-r", args)
            host = args[args.index("-n") + 1]
            leaf = x509.load_pem_x509_certificate(Path(args[args.index("-c") + 1]).read_bytes())
            self.assertEqual(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName), [host])
            checked.append(host)
            return SimpleNamespace(returncode=0 if host == HOST else 1)
        with patch.object(self.ca, "_command", side_effect=command):
            self.assertEqual(self.ca._probe(cert, key), self.scoped)
        self.assertEqual(checked, [HOST, "outside.example", "child." + HOST])

    def test_keychain_presence_matches_fingerprint_not_display_name(self):
        metadata, _, _ = self.ca._load()
        with patch.object(self.ca, "_command", return_value=SimpleNamespace(returncode=0, stdout="SHA-1 hash: " + metadata["sha1"].upper())):
            self.assertTrue(self.ca._present(metadata))
        with patch.object(self.ca, "_command", return_value=SimpleNamespace(returncode=0, stdout="SHA-1 hash: " + "A" * 40)):
            self.assertFalse(self.ca._present(metadata))

    def test_keychain_read_failure_never_deletes_local_material(self):
        with patch.object(self.ca, "_command", return_value=SimpleNamespace(returncode=1, stdout="")):
            with self.assertRaisesRegex(EzMcdError, "sampling_certificate_keychain_unreadable"):
                self.ca.remove()
        self.assertTrue((self.ca.directory / KEY).exists())

    def test_active_capture_blocks_concurrent_certificate_removal(self):
        with patch.object(self.ca, "_probe", return_value=self.scoped), self.ca.lease() as directory:
            self.assertEqual(directory, self.ca.directory)
            with self.assertRaisesRegex(EzMcdError, "sampling_certificate_in_use"):
                SamplingCertificate(self.store).remove()

    def test_local_leaf_has_only_exact_dns_name_and_independent_ephemeral_key(self):
        original = (self.ca.directory / KEY).read_bytes()
        _, root, root_key = self.ca._load()
        with patch.object(self.ca, '_command') as command, self.ca.server_certificate() as path:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            material = path.read_bytes()
            leaf, chain = x509.load_pem_x509_certificates(material)
            self.assertEqual(chain, root)
            leaf.verify_directly_issued_by(root)
            self.assertEqual(list(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value), [x509.DNSName(HOST)])
            self.assertFalse(leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
            self.assertLessEqual((leaf.not_valid_after_utc - leaf.not_valid_before_utc).total_seconds(), 86700)
            key = serialization.load_pem_private_key(material, password=None)
            self.assertEqual(key.public_key().public_numbers(), leaf.public_key().public_numbers())
            self.assertNotEqual(key.public_key().public_numbers(), root_key.public_key().public_numbers())
            command.assert_not_called()
        self.assertFalse(path.parent.exists())
        self.assertEqual((self.ca.directory / KEY).read_bytes(), original)

    def test_local_leaf_is_removed_after_capture_exception(self):
        with self.assertRaisesRegex(RuntimeError, 'capture stopped'):
            with self.ca.server_certificate() as path:
                raise RuntimeError('capture stopped')
        self.assertFalse(path.parent.exists())


if __name__ == "__main__":
    unittest.main()
