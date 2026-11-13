from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock
from contextlib import redirect_stdout, nullcontext
import io

from ezmcd.errors import EzMcdError
from ezmcd.storage import write_private
from runner import command, require_direct, stop_child, require_no_pending_restoration, successful_result
from process_receipt import require_previous_children_stopped


class RunnerTests(unittest.TestCase):
    def test_pid_reused_by_another_user_does_not_block_old_child_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            write_private(root/'collect-owned/owned-child.json',
                {'pid':1234,'fields':['501','Wed','Nov','11','12:00:00','2026','/owned/mitmdump']})
            result=SimpleNamespace(returncode=0,stdout='0 Thu Nov 12 12:00:00 2026 /other/process\n')
            with patch('process_receipt.subprocess.run',return_value=result),patch('os.kill') as signal:
                require_previous_children_stopped(root,seconds=0)
                signal.assert_not_called()

    def test_unreadable_process_state_does_not_claim_prior_child_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            write_private(root/'collect-owned/owned-child.json',{'pid':1234,'fields':['501']})
            result=SimpleNamespace(returncode=1,stdout='permission denied')
            with patch('process_receipt.subprocess.run',return_value=result):
                with self.assertRaisesRegex(EzMcdError,'child_identity_unreadable'):
                    require_previous_children_stopped(root,seconds=0)

    def test_recovery_success_does_not_require_unavailable_runtime_but_collection_does(self):
        result={'restoration_verified':True,'runtime_saved':False,'account_updated':False}
        self.assertTrue(successful_result('recover',False,result))
        self.assertFalse(successful_result('run',False,result))
        self.assertFalse(successful_result('recover',True,result))
        self.assertFalse(successful_result('recover',False,result|{'cleanup':{'errors':['not_closed']}}))
        self.assertFalse(successful_result('recover',False,result|{'restoration_verified':False}))

    def test_unresolved_shared_app_config_blocks_new_trial_before_network(self):
        from runner import run
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();path=root/'readonly-captures'/('a'*32)/'transaction/state.json'
            write_private(path,{'state':'RESTORE_SENT','candidate_may_have_applied':True,'restore_observed':False})
            with patch('runner.proxy_snapshot',return_value=''),patch('runner.prepare_dns') as dns, \
                    patch('runner.fetch_config') as fetch,patch('runner.SamplingApp') as app:
                with self.assertRaisesRegex(EzMcdError,'previous_restoration_required'):
                    run(SimpleNamespace(root=root),'another-alias','unused')
            dns.assert_not_called();fetch.assert_not_called();app.assert_not_called()
            write_private(path,{'state':'RESTORED','candidate_may_have_applied':True,'restore_observed':False})
            with self.assertRaises(EzMcdError):require_no_pending_restoration(root)
            write_private(path,{'state':'RESTORED','candidate_may_have_applied':True,'restore_observed':True})
            require_no_pending_restoration(root)

    def test_proxy_command_is_positive_app_scope_with_two_exact_host_certificates(self):
        args=command(Path('/private/driver.json'),'/owned/mitmdump',Path('/private/api.pem'),
            Path('/private/img.pem'),Path('/private/ca'))
        modes=[args[i+1] for i,v in enumerate(args[:-1]) if v=='--mode']
        self.assertEqual(modes,['local:/McdApp.app/McdApp','regular@127.0.0.1:18089'])
        certs=[args[i+1] for i,v in enumerate(args[:-1]) if v=='--certs']
        self.assertEqual(certs,['api.mcd.cn=/private/api.pem','img.mcd.cn=/private/img.pem'])
        self.assertIn('upstream_cert=false',args)
        self.assertNotIn('ssl_insecure=true',args)

    def test_existing_system_proxy_is_not_reconfigured(self):
        require_direct('<dictionary> {\n HTTPEnable : 0\n HTTPSEnable : 0\n}')
        for key in ('HTTPEnable','HTTPSEnable','SOCKSEnable','ProxyAutoConfigEnable','ProxyAutoDiscoveryEnable'):
            with self.assertRaises(EzMcdError):require_direct(key+' : 1')

    def test_child_shutdown_targets_only_given_handle(self):
        child=Mock();child.poll.side_effect=[None,0]
        self.assertTrue(stop_child(child))
        child.terminate.assert_called_once();child.wait.assert_called_once_with(timeout=5)
        child.kill.assert_not_called()
        finished=Mock();finished.poll.return_value=0
        self.assertTrue(stop_child(finished));finished.terminate.assert_not_called()

    def test_live_previous_child_cannot_be_treated_as_terminal_or_restarted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();saved={'pid':1234,'fields':['same-user','same-start','same-command']}
            write_private(root/'collect-owned/owned-child.json',saved)
            with patch('process_receipt.identity',return_value=saved) as probe:
                with self.assertRaisesRegex(EzMcdError,'previous_child_still_running'):
                    require_previous_children_stopped(root,seconds=0)
                probe.assert_called_once_with(1234)

    def test_pid_reuse_and_missing_pid_are_terminal_for_original_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();saved={'pid':1234,'fields':['same-user','same-start','same-command']}
            write_private(root/'collect-owned/owned-child.json',saved)
            for current in (None,{'pid':1234,'fields':['same-user','new-start','same-command']}):
                with patch('process_receipt.identity',return_value=current),patch('os.kill') as signal:
                    require_previous_children_stopped(root,seconds=0)
                    signal.assert_not_called()

    def test_phase_startup_failure_still_stops_owned_child_without_launching_app(self):
        from runner import phase
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();store=SimpleNamespace(root=root,account=lambda _: {'binary':'unused'})
            child=Mock();child.poll.return_value=1
            app=Mock();app.pids.return_value=[]
            with patch('runner.SamplingApp',return_value=app),patch('runner.subprocess.Popen',return_value=child), \
                    patch('runner.close_app',return_value=True),patch('runner.stop_child',return_value=True) as stop, \
                    redirect_stdout(io.StringIO()):
                result=phase(store,'fixture',root,'collect','{}',{},'unused',root/'api',root/'img',root,[False])
            app.start.assert_not_called();stop.assert_called_once_with(child)
            self.assertTrue(result['official_app_closed']);self.assertTrue(result['capture_process_stopped'])
            self.assertIn('readonly_capture_startup_stopped',result['errors'])

    def test_app_cleanup_failure_does_not_skip_child_shutdown(self):
        from runner import phase
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();store=SimpleNamespace(root=root,account=lambda _: {'binary':'unused'})
            child=Mock();child.poll.return_value=1
            app=Mock();app.pids.return_value=[]
            with patch('runner.SamplingApp',return_value=app),patch('runner.subprocess.Popen',return_value=child), \
                    patch('runner.close_app',side_effect=OSError('app stalled')),patch('runner.stop_child',return_value=True) as stop, \
                    redirect_stdout(io.StringIO()):
                result=phase(store,'fixture',root,'collect','{}',{},'unused',root/'api',root/'img',root,[False])
            stop.assert_called_once_with(child)
            self.assertFalse(result['official_app_closed']);self.assertTrue(result['capture_process_stopped'])
            self.assertIn('app_close_unverified',result['errors'])

    def test_missing_guard_audit_cannot_pass_probe_even_if_socket_closed(self):
        from runner import probe
        import http.client
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();client=Mock();client.getresponse.side_effect=http.client.RemoteDisconnected()
            with patch('runner.ssl.create_default_context'),patch('runner.http.client.HTTPSConnection',return_value=client):
                with self.assertRaises((EzMcdError,OSError)):probe(root/'ca',root,root/'transaction')
            client.close.assert_called_once()

    def test_restored_child_with_missing_parent_cleanup_completes_cleanup_without_new_capture(self):
        from runner import run
        from ezmcd.storage import read_private
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();directory=root/'readonly-captures'/('a'*32)
            write_private(directory/'transaction/state.json',{'state':'RESTORED',
                'runtime_saved':False,'restore_observed':True,'candidate_may_have_applied':True})
            store=SimpleNamespace(root=root,account=lambda _:{'identity':'fixture','binary':'unused'},
                lock=lambda _:nullcontext())
            ca=SimpleNamespace(lease=lambda:nullcontext(root),server_certificate=lambda:nullcontext(root/'leaf'))
            with patch('runner.proxy_snapshot',return_value=''),patch('runner.prepare_dns',return_value={}), \
                    patch('runner.require_previous_children_stopped'),patch('runner.SamplingCertificate',return_value=ca), \
                    patch('runner.ConfigCertificate',return_value=ca),patch('runner.SamplingApp'), \
                    patch('runner.close_app',return_value=True) as closed,patch('runner.resources.cleanup_previous') as clean, \
                    patch('runner.resources.record'),patch('runner.resources.removed',return_value=True), \
                    patch('runner.phase') as phase,redirect_stdout(io.StringIO()):
                result=run(store,'fixture','unused',recover=directory)
            closed.assert_called_once();clean.assert_called_once_with(root,directory);phase.assert_not_called()
            self.assertTrue(result['restoration_verified'])
            self.assertTrue(read_private(directory/'cleanup.json')['temporary_leaf_keys_removed'])


if __name__=='__main__':unittest.main()
