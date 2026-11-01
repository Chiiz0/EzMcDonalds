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
from ezmcd.sampling import RuntimeCapture
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
        for options in ({'seconds':121},{'seconds':True},{'seconds':0},{'service':'-all'}, {'service':'Wi-Fi\n'}):
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


if __name__=='__main__':unittest.main()
