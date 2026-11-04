from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import contextmanager, redirect_stdout
import io
import json
import sys

from ezmcd.errors import EzMcdError
from ezmcd.sampling import RuntimeCapture, capture_outcome
from ezmcd.storage import read_private, write_private
from ezmcd.models import fingerprint


class SamplingTests(unittest.TestCase):
    def setUp(self):
        from ezmcd.sampling_connect import AccountCapture
        from ezmcd.sampling_local import TRANSPORT, LOCAL_MODE
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()
        self.capture=AccountCapture(SimpleNamespace(root=self.root), 'synthetic', binary=str(self.root/'client'), report=Mock())
        self.capture.directory=self.root/'capture'
        write_private(self.capture.directory/'audit.json', {'runtime_saved':False,'balance_saved':True})
        write_private(self.capture.directory/'transport-ready.json', {'transport':TRANSPORT,'local_mode':LOCAL_MODE,'system_proxy_modified':False})
        self.before={'Enabled':'No','Server':'127.0.0.1','Port':'9668','Authenticated Proxy Enabled':'0'}
        self.effective={'HTTPEnable':'0','HTTPSEnable':'0','SOCKSEnable':'0'}
        self.running=False; self.started=False; self.calls=[]; self.clock=0
        self.process=Mock(); self.process.poll.return_value=None
        self.process.wait.side_effect=lambda **kw:setattr(self.process.poll,'return_value',0)
        self.app=Mock()
        self.app.validate.side_effect=lambda:self.calls.append(['app-validate'])
        self.app.launch_pending.return_value=False;self.app.launch_failed.return_value=False
        def close():
            self.calls.append(['app-close']);self.running=False
        def start():
            self.calls.append(['app-start']);self.running=True;self.started=True
        self.app.request_close.side_effect=close;self.app.start.side_effect=start
        def command(args,check=True):
            self.calls.append(args)
            raise AssertionError('Capture must never change any system setting')
        def tick(duration):
            self.clock+=duration
            if not self.started:self.running=True;self.started=True
            else:self.running=False
        for name,value in [('proxy_state',Mock(return_value=self.before)),
                           ('effective_proxy',Mock(return_value=self.effective)),
                           ('command',command), ('probe',Mock()), ('app_running',lambda:self.running)]:
            patcher=patch.object(self.capture,name,value);patcher.start();self.addCleanup(patcher.stop)
        for name,value in [('subprocess.Popen',Mock(return_value=self.process)),
                           ('signal.signal',Mock()), ('time.sleep',tick), ('time.monotonic',lambda:self.clock)]:
            patcher=patch('ezmcd.sampling.'+name,value);patcher.start();self.addCleanup(patcher.stop)
        patcher=patch('ezmcd.sampling_app.SamplingApp',return_value=self.app);patcher.start();self.addCleanup(patcher.stop)
        self.account={'binary':str(self.root/'client'),'identity':'synthetic'}

    def run_capture(self):
        return self.capture._run(self.account,self.root/'ca','/synthetic/mitmdump',None)

    def managed_app(self):
        self.capture.manage_app=True
        return self.app

    def test_managed_lifecycle_never_touches_system_proxy(self):
        app=self.managed_app();self.running=True
        self.capture.probe.side_effect=lambda *_:self.calls.append(['probe'])
        result=self.run_capture()
        self.assertTrue(result['managed_app']);self.assertFalse(result['system_proxy_modified'])
        self.assertTrue(result['official_app_observed'])
        self.assertIn('APP_RUNNING',[c.args[0]['stage'] for c in self.capture.report.call_args_list])
        self.assertLess(self.calls.index(['app-close']),self.calls.index(['probe']))
        self.assertLess(self.calls.index(['probe']),self.calls.index(['app-start']))
        self.assertEqual(app.request_close.call_count,2)
        cleanup=read_private(self.capture.directory/'cleanup.json')
        self.assertTrue(cleanup['capture_process_stopped']);self.assertTrue(cleanup['system_proxy_unchanged'])
        self.assertEqual(cleanup['proxy_after'],self.before)

    def test_modes_cannot_select_every_process_or_bypass_guarded_destinations(self):
        from ezmcd.sampling_local import LOCAL_MODE, PROBE_MODE
        with patch('ezmcd.sampling.subprocess.Popen',return_value=self.process) as popen:
            self.run_capture()
        args=popen.call_args.args[0]
        modes=[args[i+1] for i,value in enumerate(args) if value=='--mode']
        self.assertEqual(modes,[LOCAL_MODE,PROBE_MODE])
        self.assertNotIn('--allow-hosts',args);self.assertNotIn('--ignore-hosts',args)
        self.assertIn('rawtcp=false',args)

    def test_unverified_app_stops_before_listener_or_app_controls(self):
        self.app.validate.side_effect=EzMcdError('sampling_managed_app_unsupported')
        with self.assertRaisesRegex(EzMcdError,'unsupported'):self.run_capture()
        self.app.start.assert_not_called();self.app.request_close.assert_not_called()
        self.process.terminate.assert_not_called()

    def test_preflight_failure_never_opens_app_and_stops_listener(self):
        self.managed_app();self.capture.probe.side_effect=EzMcdError('sampling_guard_preflight_failed')
        with self.assertRaisesRegex(EzMcdError,'preflight_failed'):self.run_capture()
        self.app.start.assert_not_called();self.process.terminate.assert_called_once()

    def test_launch_failure_stops_listener_without_changing_proxy(self):
        self.managed_app();self.app.start.side_effect=EzMcdError('sampling_managed_app_launch_failed')
        with self.assertRaisesRegex(EzMcdError,'launch_failed'):self.run_capture()
        self.assertTrue(read_private(self.capture.directory/'cleanup.json')['system_proxy_unchanged'])
        self.process.terminate.assert_called_once()

    def test_stuck_app_and_launcher_do_not_make_cleanup_unbounded(self):
        self.managed_app()
        def start():
            self.running=True;self.app.launch_pending.return_value=True
        self.app.start.side_effect=start;self.app.request_close.side_effect=lambda:None
        def tick(d):self.clock+=d
        with patch('ezmcd.sampling.time.sleep',side_effect=tick):
            with self.assertRaisesRegex(EzMcdError,'cleanup_needs_attention'):self.run_capture()
        self.assertLessEqual(self.clock,5)
        self.process.terminate.assert_called_once()
        cleanup=read_private(self.capture.directory/'cleanup.json')
        self.assertEqual(cleanup['cleanup_errors'],['official_app_still_running'])
        self.assertTrue(cleanup['system_proxy_unchanged']);self.assertTrue(cleanup['capture_process_stopped'])

    def test_dead_child_cannot_leave_system_proxy_enabled(self):
        self.process.poll.return_value=1
        with self.assertRaisesRegex(EzMcdError,'local_capture_unavailable'):self.run_capture()
        self.assertTrue(read_private(self.capture.directory/'cleanup.json')['capture_process_stopped'])
        self.assertFalse(read_private(self.capture.directory/'cleanup.json')['system_proxy_modified'])

    def test_unreadable_app_state_during_cleanup_still_stops_child(self):
        self.managed_app()
        def running():
            if self.started:raise EzMcdError('sampling_app_state_unreadable')
            return False
        self.capture.app_running=running
        with self.assertRaisesRegex(EzMcdError,'cleanup_needs_attention'):self.run_capture()
        self.process.terminate.assert_called_once()
        cleanup=read_private(self.capture.directory/'cleanup.json')
        self.assertTrue(cleanup['capture_process_stopped'])
        self.assertIn('app_cleanup_unconfirmed',cleanup['cleanup_errors'])

    def test_failed_network_query_cannot_prevent_child_cleanup(self):
        self.capture.proxy_state.side_effect=[self.before,self.before,EzMcdError('sampling_system_command_failed')]
        with self.assertRaisesRegex(EzMcdError,'cleanup_needs_attention'):self.run_capture()
        self.process.terminate.assert_called_once()
        cleanup=read_private(self.capture.directory/'cleanup.json')
        self.assertIsNone(cleanup['proxy_after'])
        self.assertIn('system_proxy_state_unreadable',cleanup['cleanup_errors'])

    def test_regular_probe_readiness_is_insufficient_for_starting_app(self):
        (self.capture.directory/'transport-ready.json').unlink()
        self.managed_app()
        with self.assertRaisesRegex(EzMcdError,'local_capture_unavailable'):self.run_capture()
        self.app.start.assert_not_called();self.capture.probe.assert_not_called()
        self.process.terminate.assert_called_once()
        self.assertLessEqual(self.clock,11)

    def test_malformed_scope_fails_before_launch(self):
        write_private(self.capture.directory/'transport-ready.json',{'transport':'local'})
        self.managed_app()
        with self.assertRaisesRegex(EzMcdError,'local_scope_invalid'):self.run_capture()
        self.app.start.assert_not_called()

    def test_running_app_and_existing_proxy_stop_without_mutation(self):
        self.running=True
        with self.assertRaisesRegex(EzMcdError,'close_official_app_first'):self.run_capture()
        self.running=False
        self.capture.effective_proxy.return_value=dict(self.effective,ProxyAutoConfigEnable='1')
        with self.assertRaisesRegex(EzMcdError,'existing_proxy_requires_review'):self.run_capture()
        self.process.terminate.assert_not_called()

    def test_environment_change_before_launch_is_not_overwritten(self):
        self.managed_app()
        changed=dict(self.effective,SOCKSEnable='1')
        self.capture.effective_proxy.side_effect=[self.effective,changed,changed]
        with self.assertRaisesRegex(EzMcdError,'cleanup_needs_attention'):self.run_capture()
        self.app.start.assert_not_called()
        cleanup=read_private(self.capture.directory/'cleanup.json')
        self.assertEqual(cleanup['effective_proxy_after'],changed)
        self.assertEqual(cleanup['cleanup_errors'],['system_proxy_changed_externally'])

    def test_shutdown_escalates_only_owned_proxy_child(self):
        import subprocess
        self.process.wait.side_effect=[subprocess.TimeoutExpired('synthetic',5),None]
        self.run_capture()
        self.process.terminate.assert_called_once();self.process.kill.assert_called_once()

    def test_legacy_runtime_entry_fails_before_account_certificate_or_process(self):
        legacy=RuntimeCapture(SimpleNamespace(root=self.root),'synthetic')
        with patch.object(legacy.ca,'lease') as lease,patch.object(legacy,'executable_path') as executable:
            with self.assertRaisesRegex(EzMcdError,'runtime_capture_suspended'):legacy.run()
            with self.assertRaisesRegex(EzMcdError,'runtime_capture_suspended'):
                legacy._run({},None,None,None)
            lease.assert_not_called();executable.assert_not_called()

    def test_invalid_capture_duration_and_service_are_rejected(self):
        for options in ({'seconds':121},{'seconds':True},{'seconds':0},{'service':'-all'}, {'service':'Wi-Fi\n'}, {'manage_app':1}):
            with self.assertRaisesRegex(EzMcdError,'sampling_options_invalid'):
                RuntimeCapture(SimpleNamespace(root=self.root),'synthetic',**options)

    def test_no_missing_sample_claims_import_or_runtime(self):
        write_private(self.capture.directory/'audit.json', {'runtime_saved':False,'balance_saved':False})
        result=self.run_capture()
        self.assertFalse(result['runtime_saved']);self.assertFalse(result['sample_import_ready'])
        self.assertFalse(result['account_updated']);self.assertEqual(result['native_acceptance'],'pending')

    def test_pending_launcher_is_not_evidence_that_app_started(self):
        self.managed_app()
        write_private(self.capture.directory/'audit.json',{'runtime_saved':False,'balance_saved':False})
        self.app.start.side_effect=lambda:None
        self.app.launch_pending.side_effect=lambda:self.clock < 120
        def tick(duration):self.clock+=duration
        with patch('ezmcd.sampling.time.sleep',side_effect=tick):result=self.run_capture()
        self.assertFalse(result['official_app_observed'])
        self.assertFalse(read_private(self.capture.directory/'cleanup.json')['official_app_observed'])
        self.assertNotIn('APP_RUNNING',[c.args[0]['stage'] for c in self.capture.report.call_args_list])


class CaptureOutcomeTests(unittest.TestCase):
    def test_probe_rejection_is_not_an_official_app_request(self):
        result=capture_outcome({'blocked_requests':1,'allowed_gets':0,'last_result':'runtime_rejected',
            'balance_saved':False,'runtime_saved':False})
        self.assertEqual(result['capture_state'],'no_api_requests')
        self.assertFalse(result['sample_import_ready'])
        self.assertTrue(result['new_sampling_required'])
        self.assertFalse(result['capture_window_active'])

    def test_refresh_or_blocked_real_requests_are_activity_even_without_gets(self):
        for evidence in ({'allowed_session_refreshes':1,'blocked_requests':1}, {'blocked_requests':2}):
            self.assertEqual(capture_outcome(evidence)['capture_state'],'balance_missing')

    def test_missing_and_rejected_balances_need_new_capture_not_partial_import(self):
        for error,state in ((None,'balance_missing'),('wrong_member','balance_rejected')):
            result=capture_outcome({'allowed_gets':1,'balance_error':error})
            self.assertEqual(result['capture_state'],state)
            self.assertFalse(result['sample_import_ready'])
            self.assertTrue(result['new_sampling_required'])

    def test_valid_balance_is_not_complete_runtime_evidence(self):
        for last,state in (('balance_captured','runtime_missing'),('runtime_rejected','runtime_rejected'),
                           ('expired_blocking','runtime_missing')):
            result=capture_outcome({'allowed_gets':1,'balance_saved':True,'runtime_saved':False,'last_result':last})
            self.assertEqual(result['capture_state'],state)
            self.assertFalse(result['sample_import_ready'])

    def test_complete_capture_overrides_historical_rejections_but_window_is_closed(self):
        result=capture_outcome({'balance_saved':True,'runtime_saved':True,'balance_error':'previous_error',
            'last_result':'expired_blocking','allowed_gets':5})
        self.assertEqual(result['capture_state'],'complete')
        self.assertTrue(result['sample_import_ready'])
        self.assertFalse(result['new_sampling_required'])
        self.assertFalse(result['capture_window_active'])

    def test_unknown_diagnostic_text_is_not_copied_into_guidance(self):
        result=capture_outcome({'allowed_gets':1,'balance_error':'secret-session-or-response'})
        self.assertNotIn('secret-session-or-response',json.dumps(result))

    def test_cli_legacy_capture_stops_before_constructing_capture(self):
        from ezmcd.cli import main
        from contextlib import redirect_stderr
        with tempfile.TemporaryDirectory() as root:
            output=io.StringIO()
            argv=['ezmcd','--data-dir',root,'sampling-capture','synthetic','--interactive','--update-account']
            with patch.object(sys,'argv',argv), patch('ezmcd.sampling.RuntimeCapture') as capture, \
                    patch('ezmcd.sampling_import.import_capture') as importer, redirect_stderr(output):
                self.assertEqual(main(),2)
                capture.assert_not_called();importer.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['error'],'sampling_runtime_capture_suspended')


if __name__=='__main__':unittest.main()
