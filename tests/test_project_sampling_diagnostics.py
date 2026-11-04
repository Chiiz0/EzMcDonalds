import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ezmcd.sampling_diagnostics import NATIVE_COUNTERS, routing_diagnostics
from ezmcd.sampling import capture_outcome
from ezmcd.sampling_console import SamplingConsole
from ezmcd.sampling_guard import RuntimeGuard
import test_project_sampling_guard as guard_fixture


def client(mode='local', protocol='tcp'):
    return SimpleNamespace(proxy_mode=SimpleNamespace(type_name=mode), transport_protocol=protocol,
                           error='private-tls-error', sni='private.example')


class NativeDiagnosticTests(unittest.TestCase):
    flow=guard_fixture.GuardTests.flow

    def setUp(self):
        guard_fixture.GuardTests.setUp(self)
        self.guard=RuntimeGuard(self.config|{'transport':'local-app-v1','schema_version':3,
            'purpose':'account_connect','identity':None,'refresh_session':None},clock=lambda:self.now)

    def test_regular_preflight_is_not_native_traffic(self):
        connection=client('regular')
        data=SimpleNamespace(context=SimpleNamespace(client=connection),client_hello=SimpleNamespace(sni='api.mcd.cn'))
        self.guard.client_connected(connection)
        self.guard.tls_clienthello(data)
        self.guard.tls_failed_client(data)
        self.guard.tls_established_client(data)
        self.guard.server_connect_error(SimpleNamespace(client=connection))
        probe=self.flow('POST','/bff/order/orders');probe.client_conn=connection
        self.guard.requestheaders(probe)
        self.assertTrue(probe.killed)
        self.assertTrue(all(self.guard.audit[key]==0 for key in NATIVE_COUNTERS))
        self.assertEqual(capture_outcome(self.guard.audit)['capture_state'],'no_api_requests')

    def test_native_tls_counters_omit_hostname_and_error_details(self):
        connection=client()
        data=SimpleNamespace(context=SimpleNamespace(client=connection),client_hello=SimpleNamespace(sni='api.mcd.cn'))
        self.guard.client_connected(connection)
        self.guard.tls_clienthello(data);self.guard.tls_failed_client(data)
        data.client_hello.sni='private.example'
        self.guard.tls_clienthello(data);self.guard.tls_established_client(data)
        self.guard.server_connect_error(SimpleNamespace(client=connection))
        self.guard.audit['blocked_requests']=1
        view=capture_outcome(self.guard.audit)
        self.assertEqual(view['capture_state'],'native_traffic_without_api')
        self.assertFalse(view['sample_import_ready'])
        counters=view['routing_diagnostics']
        for key in ('native_connections','native_tls_api','native_tls_other','native_tls_errors',
                    'native_tls_completed','native_upstream_errors'):
            self.assertEqual(counters[key],1)
        self.assertNotIn('private',json.dumps(view))
        self.assertNotIn('private', (self.directory/'audit.json').read_text())

    def test_udp_and_blocked_native_destination_are_counted_without_weakening_block(self):
        connection=client(protocol='udp');self.guard.client_connected(connection)
        server=SimpleNamespace(address=('private.example',443),sni=None,transport_protocol='udp',error=None)
        self.guard.server_connect(SimpleNamespace(server=server,client=connection))
        self.assertEqual(server.error,'EZMCD_LOCAL_DESTINATION_BLOCKED')
        self.assertEqual(self.guard.audit['native_udp_connections'],1)
        self.assertEqual(self.guard.audit['native_destinations_blocked'],1)
        self.assertEqual(self.guard.audit['upstream_connections'],0)

    def test_native_write_is_blocked_before_diagnostic_io(self):
        flow=self.flow('POST','/bff/order/orders');flow.client_conn=client()
        with patch.object(self.guard,'save_audit',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.guard.requestheaders(flow)
        self.assertTrue(flow.killed);self.assertEqual(flow.response.status_code,409)

    def test_http_headers_are_distinct_from_connection_counters(self):
        flow=self.flow('GET','/bff/member/mwallet/balance');flow.client_conn=client()
        self.guard.requestheaders(flow)
        self.assertIsNone(flow.response)
        self.assertEqual(self.guard.audit['native_http_requests'],1)
        self.assertEqual(self.guard.audit['allowed_gets'],1)

    def test_counter_saturation_and_legacy_audit(self):
        self.guard.audit['native_connections']=1000000
        self.guard.client_connected(client())
        self.assertEqual(self.guard.audit['native_connections'],1000000)
        self.assertEqual(routing_diagnostics({'allowed_gets':0}),{'available':False})
        for invalid in (True,-1,'private',1000001):
            view=routing_diagnostics(self.guard.audit|{'native_connections':invalid})
            self.assertEqual(view,{'available':False})


class FinalConsoleTests(unittest.TestCase):
    def render(self, result):
        out=io.StringIO();SamplingConsole(stderr=out,balance_only=True).finished(result)
        return out.getvalue()

    def test_only_confirmed_import_has_a_success_marker(self):
        for key,marker in (('account_created','ACCOUNT_CONNECTED'),('account_updated','ACCOUNT_UPDATED'),
                           ('already_imported','ACCOUNT_REVERIFIED')):
            text=self.render({key:True})
            self.assertIn('['+marker+']',text);self.assertNotIn('ACCOUNT_NOT_CONFIRMED',text)

    def test_ready_balance_and_failed_import_do_not_look_successful(self):
        for result in ({},{'balance_saved':True},{'sample_import_ready':True}, {'account_updated':False},
                       {'account_updated':'true'}, {'account_created':True,'account_updated':True},
                       {'account_updated':True,'ok':False}, {'import_error':'private-secret'}):
            text=self.render(result)
            self.assertIn('[ACCOUNT_NOT_CONFIRMED]',text)
            self.assertNotIn('[ACCOUNT_UPDATED]',text);self.assertNotIn('private-secret',text)

    def test_progress_messages_do_not_contain_example_success_values(self):
        for maintenance in (False,True):
            out=io.StringIO();console=SamplingConsole(stderr=out,balance_only=True,update_account=maintenance)
            console({'stage':'CAPTURE_FINISHED','sample_import_ready':True,'capture_state':'complete'})
            self.assertNotIn('=true',out.getvalue())
            self.assertNotIn('[ACCOUNT_UPDATED]',out.getvalue())
            self.assertIn('尚未完成',out.getvalue())
