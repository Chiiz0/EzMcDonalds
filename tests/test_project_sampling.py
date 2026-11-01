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
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()
        self.capture=RuntimeCapture(SimpleNamespace(root=self.root), 'synthetic', report=Mock())
        self.capture.directory=self.root/'capture'
        write_private(self.capture.directory/'audit.json', {'runtime_saved':True,'balance_saved':True})
        self.before={'Enabled':'No','Server':'127.0.0.1','Port':'9668','Authenticated Proxy Enabled':'0'}
        self.effective={'HTTPEnable':'0','HTTPSEnable':'0','SOCKSEnable':'0'}
        self.active=dict(self.effective,HTTPSEnable='1',HTTPSProxy='127.0.0.1',HTTPSPort='18089')
        self.running=False; self.started=False; self.calls=[]
        def command(args,check=True):
            if '-setsecurewebproxystate' in args and args[-1]=='off':
                self.assertFalse(self.running)
            self.calls.append(args)
            return SimpleNamespace(returncode=0)
        def tick(_):
            if not self.started:self.running=True; self.started=True
            else:self.running=False
        self.process=Mock(); self.process.poll.return_value=None
        for name,value in [('proxy_state',Mock(return_value=self.before)),
                           ('effective_proxy',Mock(side_effect=[self.effective,self.effective,self.active])),
                           ('command',command), ('probe',Mock()), ('app_running',lambda:self.running)]:
            p=patch.object(self.capture,name,value); p.start(); self.addCleanup(p.stop)
        for name,value in [('subprocess.Popen',Mock(return_value=self.process)),
                           ('signal.signal',Mock()), ('time.sleep',tick)]:
            p=patch('ezmcd.sampling.'+name,value); p.start(); self.addCleanup(p.stop)
        self.account={'binary':'unused','identity':'synthetic'}

    def run_capture(self):
        return self.capture._run(self.account,self.root/'ca','/synthetic/mitmdump','synthetic-session-hash')

    def managed_app(self):
        self.capture.manage_app=True
        app=Mock()
        app.validate.side_effect=lambda:self.calls.append(['app-validate'])
        def close():
            self.calls.append(['app-close'])
            self.running=False
        def start():
            self.calls.append(['app-start'])
            self.running=True;self.started=True
        app.request_close.side_effect=close;app.start.side_effect=start
        app.launch_pending.return_value=False;app.launch_failed.return_value=False
        p=patch('ezmcd.sampling_app.SamplingApp',return_value=app);p.start();self.addCleanup(p.stop)
        return app

    def test_managed_lifecycle_closes_before_proxy_and_launches_only_after_guard(self):
        app=self.managed_app();self.running=True
        self.capture.probe.side_effect=lambda *_:self.calls.append(['probe'])
        result=self.run_capture()
        self.assertTrue(result['managed_app'])
        self.assertLess(self.calls.index(['app-close']),self.calls.index(['probe']))
        on=['/usr/sbin/networksetup','-setsecurewebproxystate','Wi-Fi','on']
        off=['/usr/sbin/networksetup','-setsecurewebproxystate','Wi-Fi','off']
        self.assertLess(self.calls.index(on),self.calls.index(['app-start']))
        self.assertLess(self.calls.index(['app-start']),self.calls.index(off))
        self.assertEqual(app.request_close.call_count,2)
        self.assertFalse(read_private(self.capture.directory/'cleanup.json')['app_launch_pending'])

    def test_managed_unsupported_app_stops_before_any_lifecycle_or_proxy(self):
        app=self.managed_app();app.validate.side_effect=EzMcdError('sampling_managed_app_unsupported')
        with self.assertRaisesRegex(EzMcdError,'unsupported'):self.run_capture()
        app.start.assert_not_called();app.request_close.assert_not_called()
        self.assertEqual(self.calls,[])

    def test_managed_preparation_cannot_close_unverified_process(self):
        app=self.managed_app();app.request_close.side_effect=EzMcdError('sampling_managed_app_process_changed')
        with self.assertRaisesRegex(EzMcdError,'process_changed'):self.run_capture()
        app.start.assert_not_called();self.process.terminate.assert_not_called()
        self.assertEqual(self.calls,[['app-validate']])

    def test_managed_preflight_failure_never_opens_app(self):
        app=self.managed_app();self.capture.probe.side_effect=EzMcdError('sampling_guard_preflight_failed')
        with self.assertRaisesRegex(EzMcdError,'preflight_failed'):self.run_capture()
        app.start.assert_not_called()
        self.assertFalse(any(args[0]=='/usr/sbin/networksetup' for args in self.calls))

    def test_managed_launch_failure_restores_proxy(self):
        app=self.managed_app();app.start.side_effect=EzMcdError('sampling_managed_app_launch_failed')
        with self.assertRaisesRegex(EzMcdError,'launch_failed'):self.run_capture()
        self.assertEqual(read_private(self.capture.directory/'cleanup.json')['proxy_after'],self.before)
        self.process.terminate.assert_called_once()

    def test_managed_exit_failure_requests_manual_close_and_waits(self):
        app=self.managed_app()
        app.request_close.side_effect=[None,EzMcdError('sampling_managed_app_process_changed')]
        self.run_capture()
        stages=[c.args[0]['stage'] for c in self.capture.report.call_args_list]
        self.assertIn('APP_MANUAL_CLOSE_REQUIRED',stages)
        self.assertLess(stages.index('APP_MANUAL_CLOSE_REQUIRED'),stages.index('CLEANUP_RESULT'))
        self.assertEqual(app.request_close.call_count,2)

    def test_delayed_launch_remains_guarded_until_app_and_launcher_both_end(self):
        app=self.managed_app();clock=[0];pending=[False];ticks=[0]
        app.start.side_effect=lambda:pending.__setitem__(0,True)
        app.launch_pending.side_effect=lambda:pending[0]
        def close():
            if self.running:self.running=False;pending[0]=False
        app.request_close.side_effect=close
        def tick(_):
            ticks[0]+=1
            if ticks[0]==1:clock[0]=121
            elif ticks[0]==2:self.running=True
        with patch('ezmcd.sampling.time.time',side_effect=lambda:clock[0]),patch('ezmcd.sampling.time.sleep',side_effect=tick):
            self.run_capture()
        self.assertGreaterEqual(app.request_close.call_count,3)
        self.assertFalse(pending[0]);self.assertFalse(self.running)
        self.assertFalse(read_private(self.capture.directory/'cleanup.json')['app_launch_pending'])

    def test_guard_failure_during_launch_waits_before_restoring_proxy(self):
        app=self.managed_app();pending=[False]
        def start():pending[0]=True;self.process.poll.return_value=1
        app.start.side_effect=start;app.launch_pending.side_effect=lambda:pending[0]
        def close():
            if self.running:self.running=False;pending[0]=False
        app.request_close.side_effect=close
        def tick(_):
            if pending[0]:self.running=True
        with patch('ezmcd.sampling.time.sleep',side_effect=tick):
            with self.assertRaisesRegex(EzMcdError,'listener_failed'):self.run_capture()
        self.assertFalse(pending[0]);self.assertFalse(self.running)
        self.assertEqual(read_private(self.capture.directory/'cleanup.json')['proxy_after'],self.before)

    def test_waits_for_app_close_then_restores_proxy_and_retains_certificate(self):
        result=self.run_capture()
        cleanup=read_private(self.capture.directory/'cleanup.json')
        self.assertTrue(cleanup['official_app_closed'])
        self.assertEqual(cleanup['proxy_after'],self.before)
        self.assertTrue(cleanup['persistent_certificate_retained'])
        self.assertFalse(result['account_updated'])
        self.assertEqual(result['order_requests_forwarded'],0)
        self.assertEqual(result['payment_requests_forwarded'],0)
        self.assertTrue(result['runtime_saved'])
        self.assertEqual(result['capture_state'],'complete')
        self.assertTrue(result['sample_import_ready'])
        self.assertFalse(result['capture_window_active'])
        self.assertFalse(any('security' in args[0] for args in self.calls))
        self.process.terminate.assert_called_once()
        loader=self.capture.directory/'addon.py'
        self.assertTrue(loader.is_file())
        self.assertEqual(loader.stat().st_mode&0o777,0o600)

    def test_running_app_stops_before_proxy_or_listener(self):
        self.running=True
        with self.assertRaisesRegex(EzMcdError,'sampling_close_official_app_first'):self.run_capture()
        self.assertEqual(self.calls,[])
        self.process.terminate.assert_not_called()

    def test_already_captured_runtime_does_not_invite_another_submission(self):
        self.run_capture()
        balance=next(c.args[0] for c in self.capture.report.call_args_list if c.args[0]['stage']=='BALANCE_CAPTURED')
        self.assertFalse(balance['capture_window_active'])
        self.assertIn('不要继续提交',balance['next'])

    def test_live_balance_reports_remaining_window_without_claiming_complete_capture(self):
        write_private(self.capture.directory/'audit.json', {'runtime_saved':False,'balance_saved':True,'allowed_gets':1})
        result=self.run_capture()
        balance=next(c.args[0] for c in self.capture.report.call_args_list if c.args[0]['stage']=='BALANCE_CAPTURED')
        self.assertTrue(balance['capture_window_active'])
        self.assertTrue(0 < balance['seconds_remaining'] <= 120)
        self.assertEqual(result['capture_state'],'runtime_missing')

    def test_balance_observed_after_window_does_not_invite_submission(self):
        write_private(self.capture.directory/'audit.json', {'runtime_saved':False,'balance_saved':True,'allowed_gets':1})
        clock=[0]
        def tick(_):
            if not self.started:self.running=True;self.started=True;clock[0]=121
            else:self.running=False
        with patch('ezmcd.sampling.time.time',side_effect=lambda:clock[0]), patch('ezmcd.sampling.time.sleep',side_effect=tick):
            self.run_capture()
        balance=next(c.args[0] for c in self.capture.report.call_args_list if c.args[0]['stage']=='BALANCE_CAPTURED')
        self.assertEqual(balance['seconds_remaining'],0)
        self.assertFalse(balance['capture_window_active'])

    def test_preflight_failure_leaves_proxy_untouched(self):
        self.capture.probe.side_effect=EzMcdError('sampling_guard_preflight_failed')
        with self.assertRaisesRegex(EzMcdError,'sampling_guard_preflight_failed'):self.run_capture()
        self.assertEqual(self.calls,[])
        self.process.terminate.assert_called_once()

    def test_existing_proxy_stops_without_mutation(self):
        self.capture.effective_proxy.side_effect=None
        self.capture.effective_proxy.return_value=dict(self.effective,ProxyAutoConfigEnable='1')
        with self.assertRaisesRegex(EzMcdError,'sampling_existing_proxy_requires_review'):self.run_capture()
        self.assertEqual(self.calls,[])

    def test_wrong_effective_service_is_restored_without_launch_instruction(self):
        self.capture.effective_proxy.side_effect=[self.effective,self.effective,self.effective]
        with self.assertRaisesRegex(EzMcdError,'sampling_effective_proxy_mismatch'):self.run_capture()
        stages=[c.args[0]['stage'] for c in self.capture.report.call_args_list]
        self.assertNotIn('GUARD_ACTIVE',stages)
        self.assertEqual(read_private(self.capture.directory/'cleanup.json')['proxy_after'],self.before)

    def test_changes_during_preflight_stop_before_touching_proxy(self):
        self.capture.effective_proxy.side_effect=[self.effective,dict(self.effective,SOCKSEnable='1')]
        with self.assertRaisesRegex(EzMcdError,'sampling_environment_changed'):self.run_capture()
        self.assertEqual(self.calls,[])

    def test_failed_restoration_reports_failure(self):
        self.capture.proxy_state.side_effect=[self.before,self.before,dict(self.before,Enabled='Yes')]
        with self.assertRaisesRegex(EzMcdError,'sampling_cleanup_needs_attention'):self.run_capture()
        self.assertEqual(read_private(self.capture.directory/'cleanup.json')['cleanup_errors'],['proxy_verification'])

    def test_invalid_capture_duration_and_service_are_rejected(self):
        for options in ({'seconds':121},{'seconds':True},{'seconds':0},{'service':'-all'}, {'service':'Wi-Fi\n'}, {'manage_app':1}):
            with self.assertRaisesRegex(EzMcdError,'sampling_options_invalid'):
                RuntimeCapture(SimpleNamespace(root=self.root),'synthetic',**options)

    def test_public_entry_holds_account_and_certificate_locks(self):
        held=set()
        @contextmanager
        def lock(name, value=None):
            held.add(name)
            try:yield value
            finally:held.remove(name)
        fields={'sid':'synthetic-session','deviceToken':'synthetic-device','meddyId':'synthetic-member'}
        identity=fingerprint({'provider':'mcd-cn','member':fields['meddyId']})
        account={'provider':'mcd-cn','identity':identity,'profile':{'fields':fields}}
        self.capture.store.account=Mock(return_value=account)
        self.capture.store.lock=lambda key:lock('account')
        self.capture.ca=SimpleNamespace(lease=lambda:lock('certificate',self.root/'ca'))
        self.capture.executable=sys.executable
        def run(*_):
            self.assertEqual(held,{'account','certificate'})
            return {'runtime_saved':True}
        with patch('ezmcd.sampling.platform.system',return_value='Darwin'), patch.object(self.capture,'_run',side_effect=run):
            self.assertTrue(self.capture.run()['runtime_saved'])
        self.assertEqual(held,set())

    def test_cli_incomplete_capture_returns_nonzero_without_importing(self):
        from ezmcd.cli import main
        output=io.StringIO()
        argv=['ezmcd','--data-dir',str(self.root/'cli'),'sampling-capture','synthetic']
        with patch.object(sys,'argv',argv), patch('ezmcd.sampling.RuntimeCapture') as capture, redirect_stdout(output):
            capture.return_value.run.return_value={'runtime_saved':False,'account_updated':False}
            self.assertEqual(main(),2)
        self.assertFalse(json.loads(output.getvalue())['account_updated'])


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

    def test_cli_rejects_runtime_only_result_without_running_import(self):
        from ezmcd.cli import main
        with tempfile.TemporaryDirectory() as root:
            output=io.StringIO()
            argv=['ezmcd','--data-dir',root,'sampling-capture','synthetic','--update-account']
            with patch.object(sys,'argv',argv), patch('ezmcd.sampling.RuntimeCapture') as capture, \
                    patch('ezmcd.sampling_import.import_capture') as importer, redirect_stdout(output):
                capture.return_value.run.return_value={'runtime_saved':True,'balance_saved':False,'account_updated':False}
                self.assertEqual(main(),2)
                importer.assert_not_called()


if __name__=='__main__':unittest.main()
