from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from ezmcd.errors import EzMcdError
from ezmcd.models import fingerprint
from ezmcd.sampling_connect import AccountCapture, import_connection, require_new_alias
from ezmcd.sampling_guard import RuntimeGuard, BALANCE, REFRESH
from ezmcd.sampling_console import SamplingConsole
from ezmcd.storage import Store, read_private, write_private
import test_project_sampling_guard as guard_fixture
import test_project_sampling as capture_fixture
from test_project_accounts import KEYS, sample
from test_project_sampling_console import Terminal


class ConnectionGuardTests(unittest.TestCase):
    def setUp(self):
        guard_fixture.GuardTests.setUp(self)
        self.config.update(schema_version=3,purpose='account_connect',identity=None,refresh_session=None)
        self.guard=RuntimeGuard(self.config,clock=lambda:self.now)

    flow=guard_fixture.GuardTests.flow
    balance=guard_fixture.GuardTests.balance
    runtime=guard_fixture.GuardTests.runtime

    def test_verified_balance_binds_first_member_without_collecting_runtime(self):
        self.balance()
        self.assertTrue(self.guard.audit['balance_saved'])
        self.assertEqual(self.guard.audit['captured_identity'],fingerprint({'provider':'mcd-cn','member':'synthetic-member'}))
        self.guard.requestheaders(self.runtime())
        self.assertFalse((self.directory/'runtime.json').exists())
        self.assertFalse(self.guard.audit['runtime_saved'])
        self.assertNotIn('synthetic-',json.dumps(self.guard.audit))

    def test_all_auth_cart_order_and_wallet_writes_remain_blocked(self):
        self.balance()
        for route in (REFRESH,'/bff/passport/login/mobile','/bff/passport/verifyCode/sms/send',
                      '/bff/cart/carts','/bff/order/orders','/bff/cashier/preorder'):
            flow=self.flow('POST',route,{'sid':'synthetic-session','token':'synthetic-device'})
            self.guard.requestheaders(flow)
            self.assertTrue(flow.killed);self.assertEqual(flow.response.status_code,409)
        self.assertNotIn('allowed_session_refreshes',self.guard.audit)

    def test_first_sample_is_not_replaced_by_another_member(self):
        self.balance()
        before=(self.directory/'balance.json').read_bytes()
        self.balance(meddyid='another-member')
        self.assertEqual(before,(self.directory/'balance.json').read_bytes())

    def test_invalid_signature_or_expired_window_cannot_select_identity(self):
        for expired in (False,True):
            self.guard=RuntimeGuard(self.config,clock=lambda:self.now)
            evidence=sample();evidence['request']['headers']['authorization']='invalid-signature'
            flow=self.flow('GET',BALANCE,evidence['request']['headers'])
            self.guard.requestheaders(flow)
            flow.response=guard_fixture.http.Response.make(200,b'{"success":true}')
            if expired:self.guard.deadline=self.now-1
            self.guard.response(flow)
            self.assertFalse(self.guard.audit['balance_saved'])
            self.assertNotIn('captured_identity',self.guard.audit)

    def test_connection_scope_cannot_enable_an_unbound_refresh(self):
        for change in ({'identity':'some-member'},{'refresh_session':'some-session'},{'schema_version':2}):
            with self.assertRaisesRegex(ValueError,'invalid_connection_capture_scope'):
                RuntimeGuard(self.config|change)


class ConnectionImportTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.store=Store(Path(temporary.name).resolve())
        self.directory=self.store.root/'captures'/('a'*32)
        self.binary=str(self.store.root/'client')
        self.identity=fingerprint({'provider':'mcd-cn','member':'synthetic-member'})
        p=patch('ezmcd.accounts.constants',return_value=KEYS);p.start();self.addCleanup(p.stop)
        p=patch('ezmcd.provider.McdProvider.wallet',return_value={'balance_cents':100})
        self.wallet=p.start();self.addCleanup(p.stop)
        p=patch('ezmcd.transport.Transport.send',side_effect=AssertionError('No non-wallet requests'))
        p.start();self.addCleanup(p.stop)
        self.make_capture()

    def make_capture(self):
        proxy={'Enabled':'No','Server':'localhost','Port':'1234','Authenticated Proxy Enabled':'0'}
        files={'config':{'schema_version':3,'purpose':'account_connect','output':str(self.directory),
                 'binary':self.binary,'identity':None,'refresh_session':None,'alias':'mine'},
            'audit':{'balance_saved':True,'runtime_saved':False,'blocked_requests':1,'captured_identity':self.identity},
            'cleanup':{'official_app_closed':True,'app_launch_pending':False,'cleanup_errors':[],
                'proxy_before':proxy,'proxy_after':proxy},'balance':sample()}
        for name,value in files.items():write_private(self.directory/(name+'.json'),value)

    def edit(self,name,change):
        p=self.directory/(name+'.json');v=read_private(p);change(v);write_private(p,v)

    def test_first_import_and_restart_repeat_preserve_no_spending_boundary(self):
        first=import_connection(self.store,'mine',self.directory)
        self.assertTrue(first['account_created']);self.assertFalse(first['account_updated'])
        account=self.store.account('mine')
        self.assertFalse(account['policy']['allow_checkout']);self.assertIsNone(account['runtime'])
        self.assertFalse(account['phone_ownership_verified_by_project'])
        before=(self.store.root/'accounts'/(self.identity+'.json')).read_bytes()
        repeated=import_connection(Store(self.store.root),'mine',self.directory)
        self.assertTrue(repeated['already_imported']);self.assertFalse(repeated['account_created'])
        self.assertEqual(before,(self.store.root/'accounts'/(self.identity+'.json')).read_bytes())
        self.assertEqual(self.wallet.call_count,2)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0],0)
        self.assertNotIn('synthetic-session',json.dumps(first))

    def test_replay_cannot_reset_a_later_policy(self):
        import_connection(self.store,'mine',self.directory)
        account=self.store.account('mine');account['policy']['max_order_cents']=999
        self.store.update_account(account)
        self.assertTrue(import_connection(self.store,'mine',self.directory)['already_imported'])
        self.assertEqual(self.store.account('mine')['policy']['max_order_cents'],999)

    def test_existing_alias_or_member_is_not_replaced_even_after_revocation(self):
        import_connection(self.store,'mine',self.directory)
        original=self.store.account('mine')
        for change in ('revoked','credentials','receipt','alias'):
            account=deepcopy(original)
            if change=='revoked':account['revoked']=True
            if change=='credentials':account['profile']['fields']['sid']='later-session'
            if change=='receipt':account.pop('sampling_import')
            self.store.update_account(account)
            self.make_capture()
            alias='mine'
            if change=='alias':alias='other';self.edit('config',lambda x:x.update(alias=alias))
            self.wallet.reset_mock()
            with self.assertRaisesRegex(EzMcdError,'account_exists_use_same_member_maintenance'):
                import_connection(self.store,alias,self.directory)
            self.wallet.assert_not_called();self.assertEqual(self.store.account('mine'),account)

    def test_wrong_alias_binding_is_rejected_before_live_reads(self):
        with self.assertRaisesRegex(EzMcdError,'alias_mismatch'):
            import_connection(self.store,'other',self.directory)
        self.wallet.assert_not_called()

    def test_bad_cleanup_scope_member_and_signature_rejected_before_network(self):
        cases=(('config',lambda x:x.update(schema_version=2)),
               ('config',lambda x:x.update(refresh_session='unknown')),
               ('config',lambda x:x.update(binary='relative-binary')),
               ('audit',lambda x:x.update(runtime_saved=True)),
               ('audit',lambda x:x.update(allowed_session_refreshes=1)),
               ('audit',lambda x:x.update(captured_identity='another-member')),
               ('audit',lambda x:x.update(blocked_requests=0)),
               ('cleanup',lambda x:x.update(official_app_closed=False)),
               ('cleanup',lambda x:x.update(app_launch_pending=True)),
               ('cleanup',lambda x:x['proxy_after'].update(Enabled='Yes')),
               ('balance',lambda x:x['request']['headers'].update(authorization='invalid')))
        for name,change in cases:
            self.make_capture();self.edit(name,change)
            with self.subTest(name=name,change=change),self.assertRaises(EzMcdError):
                import_connection(self.store,'mine',self.directory)
        self.wallet.assert_not_called();self.assertEqual(self.store.accounts(),[])

    def test_runtime_or_refresh_file_cannot_be_smuggled_into_first_connection(self):
        for name in ('runtime','session'):
            p=self.directory/(name+'.json');write_private(p,{})
            with self.assertRaisesRegex(EzMcdError,'connection_scope_invalid'):
                import_connection(self.store,'mine',self.directory)
            p.unlink()
        self.wallet.assert_not_called()

    def test_wallet_failure_creates_no_account_and_can_resume_reads(self):
        self.wallet.side_effect=EzMcdError('session_expired_reconnect_locally')
        with self.assertRaises(EzMcdError):import_connection(self.store,'mine',self.directory)
        self.assertEqual(self.store.accounts(),[])
        self.wallet.side_effect=None
        self.assertTrue(import_connection(self.store,'mine',self.directory)['account_created'])

    def test_lost_output_after_commit_is_recovered_without_recreation(self):
        from ezmcd.accounts import _enroll
        def lost(*args,**kwargs):
            _enroll(*args,**kwargs)
            raise OSError('synthetic lost output')
        with patch('ezmcd.sampling_connect._enroll',side_effect=lost),self.assertRaises(OSError):
            import_connection(self.store,'mine',self.directory)
        self.assertTrue(import_connection(self.store,'mine',self.directory)['already_imported'])
        self.assertEqual(len(self.store.accounts()),1)

    def test_capture_replacement_after_success_is_not_imported_again(self):
        import_connection(self.store,'mine',self.directory)
        self.edit('balance',lambda x:x.update(untrusted='later modification'))
        before=self.store.account('mine');self.wallet.reset_mock()
        with self.assertRaisesRegex(EzMcdError,'account_exists'):
            import_connection(self.store,'mine',self.directory)
        self.wallet.assert_not_called();self.assertEqual(before,self.store.account('mine'))

    def test_private_permissions_and_directory_boundary_apply(self):
        link=self.directory.with_name('b'*32);link.symlink_to(self.directory,target_is_directory=True)
        with self.assertRaisesRegex(EzMcdError,'directory_invalid'):import_connection(self.store,'mine',link)
        (self.directory/'balance.json').chmod(0o644)
        with self.assertRaisesRegex(EzMcdError,'permissions'):import_connection(self.store,'mine',self.directory)
        self.wallet.assert_not_called()

    def test_new_alias_preflight_never_overwrites_existing_accounts(self):
        require_new_alias(self.store,'mine')
        import_connection(self.store,'mine',self.directory)
        capture=AccountCapture(self.store,'mine',binary=self.binary)
        with patch.object(capture,'executable_path') as executable,patch.object(capture.ca,'lease') as lease:
            with self.assertRaisesRegex(EzMcdError,'account_exists'):capture.run()
            executable.assert_not_called();lease.assert_not_called()


class ConnectionLifecycleTests(unittest.TestCase):
    def setUp(self):
        capture_fixture.SamplingTests.setUp(self)
        self.capture.__class__=AccountCapture
        self.capture.binary=str(self.root/'client')
        write_private(self.capture.directory/'audit.json',{'runtime_saved':False,'balance_saved':True})

    managed_app=capture_fixture.SamplingTests.managed_app
    run_capture=capture_fixture.SamplingTests.run_capture

    def test_stops_after_balance_and_closes_app_before_restoring_proxy(self):
        app=self.managed_app()
        result=self.run_capture()
        self.assertTrue(result['sample_import_ready']);self.assertFalse(result['runtime_saved'])
        config=read_private(self.capture.directory/'config.json')
        self.assertEqual(config['purpose'],'account_connect');self.assertIsNone(config['identity'])
        self.assertIsNone(config['refresh_session']);self.assertNotIn('account_fingerprint',config)
        events=[c.args[0] for c in self.capture.report.call_args_list]
        self.assertFalse(next(e for e in events if e['stage']=='BALANCE_CAPTURED')['capture_window_active'])
        self.assertEqual(app.request_close.call_count,2)
        self.assertEqual(read_private(self.capture.directory/'cleanup.json')['proxy_after'],self.before)

    def test_missing_balance_is_incomplete_without_any_import_claim(self):
        write_private(self.capture.directory/'audit.json',{'runtime_saved':False,'balance_saved':False})
        result=self.run_capture()
        self.assertFalse(result['sample_import_ready']);self.assertFalse(result['account_created'])


class ConnectionConsoleCliTests(unittest.TestCase):
    def test_cleanup_reports_local_directory_without_rendering_control_sequences(self):
        output=Terminal();console=SamplingConsole(stderr=output,balance_only=True)
        console({'stage':'CLEANUP_RESULT','errors':['proxy_restoration'],'capture_directory':'/local/private/capture'})
        self.assertIn('/local/private/capture',output.getvalue())
        output=Terminal();console=SamplingConsole(stderr=output,balance_only=True)
        console({'stage':'CLEANUP_RESULT','errors':[],'capture_directory':'unsafe\ncredential'})
        self.assertNotIn('credential',output.getvalue())

    def test_connect_console_never_invites_a_blocked_submission(self):
        output=Terminal();console=SamplingConsole(stdin=Terminal('\n'),stderr=output,balance_only=True)
        console.ready('mine',120)
        console({'stage':'BALANCE_CAPTURED','capture_window_active':True,'seconds_remaining':120})
        console({'stage':'CAPTURE_FINISHED','capture_state':'complete','sample_import_ready':True})
        text=output.getvalue()
        self.assertNotIn('提交一次',text);self.assertNotIn('准备测试购物车',text)
        self.assertIn('account_created=true',text);self.assertIn('没有采集下单运行字段',text)

    def invoke(self,input='\n',*,complete=True,resume=False,import_error=None):
        from ezmcd.cli import main
        with tempfile.TemporaryDirectory() as root:
            args=['ezmcd','--data-dir',root]
            args+=['sampling-import','mine','/synthetic/capture','--connect'] if resume else ['sampling-connect','mine']
            out,err=io.StringIO(),Terminal()
            with patch.object(sys,'argv',args),patch.object(sys,'stdin',Terminal(input)),patch.object(sys,'stderr',err),redirect_stdout(out), \
                    patch('ezmcd.sampling_connect.AccountCapture') as capture, \
                    patch('ezmcd.sampling_connect.import_connection',return_value={'account_created':True}) as importer:
                capture.return_value.run.return_value={'sample_import_ready':complete,'balance_saved':complete,
                    'runtime_saved':False,'capture_directory':'/synthetic/capture'}
                if import_error:importer.side_effect=import_error
                code=main()
                return code,out.getvalue(),capture,importer

    def test_cli_always_prompts_and_cancellation_prevents_capture(self):
        code,out,capture,importer=self.invoke('q\n')
        self.assertEqual(code,2);capture.return_value.run.assert_not_called();importer.assert_not_called()

    def test_complete_balance_only_capture_is_imported_and_incomplete_is_not(self):
        code,out,capture,importer=self.invoke()
        self.assertEqual(code,0);self.assertTrue(json.loads(out)['account_created'])
        self.assertTrue(capture.call_args.kwargs['report'].balance_only)
        importer.assert_called_once()
        code,out,capture,importer=self.invoke(complete=False)
        self.assertEqual(code,2);importer.assert_not_called()

    def test_resume_uses_only_import_without_console_proxy_or_capture(self):
        code,out,capture,importer=self.invoke(resume=True)
        self.assertEqual(code,0);capture.assert_not_called();importer.assert_called_once()

    def test_failed_import_keeps_the_recovery_directory_and_nonzero_exit(self):
        for error in (EzMcdError('session_expired_reconnect_locally'),OSError('private-secret')):
            code,out,capture,importer=self.invoke(import_error=error)
            value=json.loads(out)
            self.assertEqual(code,2);self.assertFalse(value['account_created'])
            self.assertEqual(value['capture_directory'],'/synthetic/capture')
            self.assertEqual(value['resume_command'][-4:],['sampling-import','mine','/synthetic/capture','--connect'])
            self.assertNotIn('private-secret',out)


if __name__=='__main__':unittest.main()
