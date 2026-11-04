import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ezmcd.errors import EzMcdError
from ezmcd.sampling_local import LocalCaptureLifecycle, LOCAL_MODE, PROBE_MODE, TRANSPORT, ready_servers
from ezmcd.sampling_status import sampling_status
from ezmcd.storage import read_private, write_private


def server(mode, **changes):
    return SimpleNamespace(mode=SimpleNamespace(full_spec=mode), is_running=True, last_exception=None, **changes)


class Servers(list):
    is_updating = False


class LocalReadinessTests(unittest.TestCase):
    def test_requires_exact_running_native_and_probe_modes(self):
        valid=Servers([server(LOCAL_MODE),server(PROBE_MODE)])
        self.assertTrue(ready_servers(SimpleNamespace(servers=valid)))
        for values in ([],valid[:1],valid[1:],valid+[server('local')],
                       [server('local'),server(PROBE_MODE)], [server('local:!McdApp'),server(PROBE_MODE)]):
            self.assertFalse(ready_servers(SimpleNamespace(servers=Servers(values))))
        valid[0].is_running=False
        self.assertFalse(ready_servers(SimpleNamespace(servers=valid)))
        valid[0].is_running=True;valid[0].last_exception=RuntimeError('synthetic')
        self.assertFalse(ready_servers(SimpleNamespace(servers=valid)))
        valid[0].last_exception=None;valid.is_updating=True
        self.assertFalse(ready_servers(SimpleNamespace(servers=valid)))
        self.assertFalse(ready_servers(None))


class LocalWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()
        self.config={'transport':TRANSPORT,'local_mode':LOCAL_MODE,'parent_pid':os.getppid(),
                     'deadline':time.time()+120,'purpose':'account_connect','output':str(self.root)}
        self.master=SimpleNamespace(shutdown=Mock(),addons=SimpleNamespace(get=Mock()))
        p=patch.dict('sys.modules',{'mitmproxy':SimpleNamespace(ctx=SimpleNamespace(master=self.master))})
        p.start();self.addCleanup(p.stop)

    async def test_parent_death_stops_child_without_waiting_for_app(self):
        with patch('ezmcd.sampling_local.os.getppid',return_value=1):
            await LocalCaptureLifecycle(self.config).watch()
        self.assertEqual(read_private(self.root/'transport-stop.json'),{'reason':'parent_exited'})
        self.master.shutdown.assert_called_once()
        self.assertFalse((self.root/'transport-ready.json').exists())

    async def test_hard_deadline_is_independent_of_wall_clock(self):
        with patch('ezmcd.sampling_local.time.monotonic',side_effect=[0,151]):
            await LocalCaptureLifecycle(self.config).watch()
        self.assertEqual(read_private(self.root/'transport-stop.json'),{'reason':'deadline'})
        self.master.shutdown.assert_called_once()

    async def test_ready_marker_requires_native_mode_then_routing_loss_stops(self):
        with patch('ezmcd.sampling_local.ready_servers',side_effect=[False,True,False]), \
                patch('ezmcd.sampling_local.asyncio.sleep',return_value=None):
            await LocalCaptureLifecycle(self.config).watch()
        self.assertEqual(read_private(self.root/'transport-ready.json'),{
            'transport':TRANSPORT,'local_mode':LOCAL_MODE,'system_proxy_modified':False})
        self.assertEqual(read_private(self.root/'transport-stop.json'),{'reason':'routing_stopped'})
        self.master.shutdown.assert_called_once()

    async def test_disk_failure_still_shuts_down(self):
        with patch('ezmcd.sampling_local.os.getppid',return_value=1), \
                patch('ezmcd.sampling_local.write_private',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):await LocalCaptureLifecycle(self.config).watch()
        self.master.shutdown.assert_called_once()

    async def test_cancelled_watcher_shuts_down(self):
        with patch('ezmcd.sampling_local.ready_servers',return_value=False):
            lifecycle=LocalCaptureLifecycle(self.config);lifecycle.running()
            await asyncio.sleep(0)
            lifecycle.done()
            with self.assertRaises(asyncio.CancelledError):await lifecycle.task
        self.master.shutdown.assert_called_once()

    def test_stalled_native_startup_has_independent_emergency_exit(self):
        lifecycle=LocalCaptureLifecycle(self.config)
        lifecycle.stopped=Mock();lifecycle.stopped.wait.side_effect=[False,False]
        with patch('ezmcd.sampling_local.os.getppid',return_value=1), \
                patch('ezmcd.sampling_local.os._exit',side_effect=SystemExit(70)) as terminate:
            with self.assertRaises(SystemExit):lifecycle.emergency_watch()
        terminate.assert_called_once_with(70)
        self.assertEqual(lifecycle.stopped.wait.call_args_list[-1].args,(5,))
        self.assertEqual(read_private(self.root/'transport-emergency-stop.json'),
                         {'reason':'parent_exited_or_deadline'})

    def test_finished_proxy_does_not_trigger_emergency_exit(self):
        lifecycle=LocalCaptureLifecycle(self.config);lifecycle.done()
        with patch('ezmcd.sampling_local.os._exit') as terminate:lifecycle.emergency_watch()
        terminate.assert_not_called()

    def test_unbounded_or_broad_scope_is_rejected(self):
        for edit in ({'transport':'system'},{'local_mode':'local'}, {'local_mode':'local:!McdApp'},
                     {'purpose':'runtime'}, {'parent_pid':1}, {'parent_pid':True},
                     {'deadline':float('inf')},{'deadline':float('nan')},{'deadline':True}):
            with self.assertRaisesRegex(EzMcdError,'local_scope_invalid'):
                LocalCaptureLifecycle(self.config|edit)


class LocalDestinationTests(unittest.TestCase):
    def test_non_api_tcp_and_all_udp_cannot_be_forwarded(self):
        from ezmcd.sampling_guard import RuntimeGuard
        with tempfile.TemporaryDirectory() as directory:
            guard=RuntimeGuard({'output':str(Path(directory).resolve()),'binary':'unused','identity':None,
                'deadline':time.time()+60,'refresh_session':None,'purpose':'account_connect',
                'schema_version':3,'transport':TRANSPORT})
            for host,sni,port,protocol in [('wallet.example',None,443,'tcp'),
                    ('127.0.0.1',None,443,'tcp'),('api.mcd.cn',None,80,'tcp'),
                    ('api.mcd.cn','api.mcd.cn',443,'udp'),('192.0.2.1','wallet.example',443,'tcp')]:
                connection=SimpleNamespace(address=(host,port),sni=sni,transport_protocol=protocol,error=None)
                guard.server_connect(SimpleNamespace(server=connection))
                self.assertEqual(connection.error,'EZMCD_LOCAL_DESTINATION_BLOCKED')
            self.assertEqual(guard.audit['upstream_connections'],0)
            for address in (None,(),('api.mcd.cn',)):
                connection=SimpleNamespace(address=address,sni='api.mcd.cn',transport_protocol='tcp',error=None)
                guard.server_connect(SimpleNamespace(server=connection))
                self.assertIsNotNone(connection.error)
            connection=SimpleNamespace(address=('192.0.2.1',443),sni='api.mcd.cn',transport_protocol='tcp',error=None)
            guard.server_connect(SimpleNamespace(server=connection))
            self.assertIsNone(connection.error);self.assertEqual(guard.audit['upstream_connections'],1)
            connection.error='earlier rejection'
            guard.server_connect(SimpleNamespace(server=connection))
            self.assertEqual(connection.error,'earlier rejection')


class SamplingStatusTests(unittest.TestCase):
    def diagnose(self, enabled='No', effective='0', *, port='18089', extra=''):
        replies=[SimpleNamespace(returncode=0,stdout='HTTPSEnable : '+effective+'\n'+extra),
                 SimpleNamespace(returncode=0,stdout=f'Enabled: {enabled}\nServer: 127.0.0.1\nPort: {port}\n')]
        with patch('ezmcd.sampling_status.platform.system',return_value='Darwin'), \
                patch('ezmcd.sampling_status.subprocess.run',side_effect=replies) as command:
            result=sampling_status()
        for call in command.call_args_list:
            self.assertIn(call.args[0][1],('--proxy','-getsecurewebproxy'))
        self.assertFalse(result['system_proxy_modified']);self.assertFalse(result['sampler_started'])
        return result

    def test_disabled_leftover_port_is_not_an_active_proxy(self):
        result=self.diagnose()
        self.assertEqual(result['proxy_status'],'disabled')
        self.assertFalse(result['possible_legacy_ezmcd_proxy'])

    def test_active_legacy_proxy_is_reported_but_never_changed(self):
        result=self.diagnose('Yes','1')
        self.assertEqual(result['proxy_status'],'active');self.assertTrue(result['possible_legacy_ezmcd_proxy'])
        result=self.diagnose('Yes','1',port='8888')
        self.assertFalse(result['possible_legacy_ezmcd_proxy'])

    def test_other_active_proxy_is_preserved(self):
        result=self.diagnose(extra='ProxyAutoConfigEnable : 1\n')
        self.assertEqual(result['proxy_status'],'active')
        self.assertIn('ProxyAutoConfigEnable',result['active_proxy_types'])

    def test_failed_query_reports_unknown_without_raw_error(self):
        with patch('ezmcd.sampling_status.platform.system',return_value='Darwin'), \
                patch('ezmcd.sampling_status.subprocess.run',side_effect=OSError('private-string')):
            result=sampling_status()
        self.assertEqual(result['proxy_status'],'unreadable');self.assertNotIn('private-string',json.dumps(result))

    def test_unsupported_platform_does_not_run_system_commands(self):
        with patch('ezmcd.sampling_status.platform.system',return_value='Linux'), \
                patch('ezmcd.sampling_status.subprocess.run') as command:
            result=sampling_status()
        command.assert_not_called();self.assertEqual(result['proxy_status'],'unsupported_platform')


class LocalImportTests(unittest.TestCase):
    from test_project_sampling_connect import ConnectionImportTests as _fixture
    make_capture = _fixture.make_capture
    def setUp(self):
        from test_project_sampling_connect import ConnectionImportTests
        ConnectionImportTests.setUp(self)
        self.edit('config',lambda x:x.update(transport=TRANSPORT,local_mode=LOCAL_MODE))
        self.edit('cleanup',lambda x:x.update(transport=TRANSPORT,system_proxy_modified=False,
            system_proxy_unchanged=True,capture_process_stopped=True,
            effective_proxy_before={'HTTPSEnable':'0'},effective_proxy_after={'HTTPSEnable':'0'}))

    def edit(self,name,fn):
        path=self.directory/(name+'.json');data=read_private(path);fn(data);write_private(path,data)

    def test_clean_native_evidence_imports_once_and_repeats_readonly(self):
        from ezmcd.sampling_connect import import_connection
        first=import_connection(self.store,'mine',self.directory)
        self.assertTrue(first['account_created'])
        self.assertTrue(import_connection(self.store,'mine',self.directory)['already_imported'])
        self.assertEqual(self.wallet.call_count,2)

    def test_incomplete_cleanup_never_issues_wallet_request_or_imports(self):
        from ezmcd.sampling_connect import import_connection
        original=read_private(self.directory/'cleanup.json')
        for edit in ({'system_proxy_modified':True},{'system_proxy_unchanged':False},
                     {'capture_process_stopped':False},{'effective_proxy_after':{'HTTPSEnable':'1'}},
                     {'transport':'system'}):
            write_private(self.directory/'cleanup.json',original|edit)
            with self.assertRaisesRegex(EzMcdError,'cleanup_needs_attention'):
                import_connection(self.store,'mine',self.directory)
        self.wallet.assert_not_called();self.assertEqual(self.store.accounts(),[])
