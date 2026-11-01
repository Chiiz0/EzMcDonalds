import hashlib
import os
from pathlib import Path
import plistlib
import signal
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from ezmcd.errors import EzMcdError
from ezmcd.sampling_app import SamplingApp


class SamplingAppTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bundle = self.root / 'App with spaces' / 'McdApp.app'
        self.bundle.mkdir(parents=True)
        self.binary = self.bundle / 'McdApp'
        self.binary.write_bytes(b'synthetic-build')
        self.plist = {'CFBundleIdentifier':'com.mcdonalds.gma.cn','CFBundleExecutable':'McdApp'}
        (self.bundle/'Info.plist').write_bytes(plistlib.dumps(self.plist))
        self.app = SamplingApp(self.binary)
        p=patch('ezmcd.sampling_app.BUILD',hashlib.sha256(b'synthetic-build').hexdigest())
        p.start(); self.addCleanup(p.stop)
        self.identity=(1234,str(os.getuid()),'Sun','Nov','1','12:00:00','2026',str(self.binary))

    def test_bundle_with_spaces_matches_its_supported_executable(self):
        self.assertEqual(self.app.validate(),self.bundle)

    def test_wrapped_app_uses_outer_bundle_and_exact_wrapped_executable(self):
        outer=self.root/'McdApp.app'; inner=outer/'Wrapper'/'McdApp.app'
        inner.mkdir(parents=True)
        (inner/'McdApp').write_bytes(self.binary.read_bytes())
        (inner/'Info.plist').write_bytes(plistlib.dumps(self.plist))
        (outer/'WrappedBundle').symlink_to(inner,target_is_directory=True)
        self.assertEqual(SamplingApp(outer/'WrappedBundle'/'McdApp').validate(),outer)

    def test_changed_or_missing_binary_is_rejected(self):
        self.binary.write_bytes(b'unsupported')
        with self.assertRaisesRegex(EzMcdError,'unsupported'):self.app.validate()
        self.binary.unlink()
        with self.assertRaisesRegex(EzMcdError,'unsupported'):self.app.validate()

    def test_other_bundle_identity_or_executable_is_rejected(self):
        for info in ({**self.plist,'CFBundleIdentifier':'unrelated'},
                     {**self.plist,'CFBundleExecutable':'Other'},[] ):
            (self.bundle/'Info.plist').write_bytes(plistlib.dumps(info))
            with self.assertRaisesRegex(EzMcdError,'unsupported'):self.app.validate()

    def test_configured_binary_must_be_the_bundle_entrypoint(self):
        other=self.bundle/'other'/'McdApp'; other.parent.mkdir(); other.write_bytes(self.binary.read_bytes())
        with self.assertRaisesRegex(EzMcdError,'unsupported'):SamplingApp(other).validate()

    def test_pid_lookup_is_limited_to_current_user_and_exact_name(self):
        self.app.command=Mock(return_value=SimpleNamespace(returncode=0,stdout='1234\n1235\n'))
        self.assertEqual(self.app.pids(),[1234,1235])
        self.app.command.assert_called_once_with(['/usr/bin/pgrep','-u',str(os.getuid()),'-x','McdApp'])
        self.app.command.return_value=SimpleNamespace(returncode=1,stdout='')
        self.assertEqual(self.app.pids(),[])

    def test_pid_lookup_errors_are_not_treated_as_app_closed(self):
        for code,value in ((2,''),(0,''),(1,'1234'),(0,'1'),(0,'unexpected')):
            self.app.command=Mock(return_value=SimpleNamespace(returncode=code,stdout=value))
            with self.assertRaisesRegex(EzMcdError,'state_unreadable'):self.app.pids()

    def test_process_identity_preserves_spaces_in_executable_path(self):
        self.app.command=Mock(return_value=SimpleNamespace(returncode=0,stdout=' '.join(self.identity[1:])))
        self.assertEqual(self.app.identity(1234),self.identity)

    def test_foreign_user_invalid_path_or_failed_ps_never_identifies_target(self):
        for code,value in ((0,' '.join(('999999',*self.identity[2:]))),
                           (0,' '.join((*self.identity[1:-1],'/other/Unknown'))),
                           (2,'')):
            self.app.command=Mock(return_value=SimpleNamespace(returncode=code,stdout=value))
            with self.assertRaisesRegex(EzMcdError,'process_changed'):self.app.identity(1234)
        self.app.command=Mock(return_value=SimpleNamespace(returncode=1,stdout=''))
        self.assertIsNone(self.app.identity(1234))

    def test_close_verifies_then_sends_one_term_only_for_same_identity(self):
        self.app.pids=Mock(return_value=[1234]); self.app.identity=Mock(return_value=self.identity)
        with patch('ezmcd.sampling_app.os.kill') as kill:
            self.app.request_close(); self.app.request_close()
        kill.assert_called_once_with(1234,signal.SIGTERM)

    def test_all_candidates_are_validated_before_first_signal(self):
        self.app.pids=Mock(return_value=[1234,1235])
        bad=self.root/'untrusted'/'McdApp.app'/'McdApp'; bad.parent.mkdir(parents=True); bad.write_bytes(b'not-official')
        self.app.identity=Mock(side_effect=[self.identity,(1235,*self.identity[1:-1],str(bad))])
        with patch('ezmcd.sampling_app.os.kill') as kill:
            with self.assertRaisesRegex(EzMcdError,'unsupported'):self.app.request_close()
        kill.assert_not_called()

    def test_pid_reuse_before_signal_is_rejected(self):
        self.app.pids=Mock(return_value=[1234])
        changed=(*self.identity[:5],'12:00:01',*self.identity[6:])
        self.app.identity=Mock(side_effect=[self.identity,changed])
        with patch('ezmcd.sampling_app.os.kill') as kill:
            with self.assertRaisesRegex(EzMcdError,'process_changed'):self.app.request_close()
        kill.assert_not_called()

    def test_process_disappearing_before_signal_is_not_an_error(self):
        self.app.pids=Mock(return_value=[1234]); self.app.identity=Mock(side_effect=[self.identity,None])
        with patch('ezmcd.sampling_app.os.kill') as kill:self.app.request_close()
        kill.assert_not_called()

    def test_signal_failure_does_not_retry_or_escalate(self):
        self.app.pids=Mock(return_value=[1234]); self.app.identity=Mock(return_value=self.identity)
        with patch('ezmcd.sampling_app.os.kill',side_effect=PermissionError) as kill:
            with self.assertRaisesRegex(EzMcdError,'close_failed'):self.app.request_close()
            self.app.request_close()
        kill.assert_called_once_with(1234,signal.SIGTERM)

    def test_start_waits_for_launchservices_and_app_exit_without_second_launch(self):
        self.app.pids=Mock(return_value=[])
        process=Mock(); process.poll.return_value=None
        with patch('ezmcd.sampling_app.subprocess.Popen',return_value=process) as start:
            self.app.start()
            self.assertTrue(self.app.launch_pending())
            self.assertFalse(self.app.launch_failed())
            with self.assertRaisesRegex(EzMcdError,'already_attempted'):self.app.start()
        self.assertEqual(start.call_args.args[0],['/usr/bin/open','-W','-a',str(self.bundle)])
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        process.poll.return_value=0
        self.assertFalse(self.app.launch_pending()); self.assertFalse(self.app.launch_failed())
        process.poll.return_value=1
        self.assertTrue(self.app.launch_failed())

    def test_start_does_not_relaunch_already_running_app(self):
        self.app.pids=Mock(return_value=[1234])
        with patch('ezmcd.sampling_app.subprocess.Popen') as start:
            with self.assertRaisesRegex(EzMcdError,'environment_changed'):self.app.start()
        start.assert_not_called()

    def test_unsupported_launch_or_os_error_has_no_raw_output(self):
        self.app.pids=Mock(return_value=[])
        with patch('ezmcd.sampling_app.subprocess.Popen',side_effect=OSError('private path')):
            with self.assertRaisesRegex(EzMcdError,'^sampling_managed_app_launch_failed$'):self.app.start()
            with self.assertRaisesRegex(EzMcdError,'already_attempted'):self.app.start()
        with patch('ezmcd.sampling_app.subprocess.run',side_effect=OSError('private path')):
            with self.assertRaisesRegex(EzMcdError,'^sampling_app_state_unreadable$'):self.app.command(['unused'])


if __name__=='__main__':unittest.main()
