import asyncio
import ipaddress
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import test_project_sampling_guard as fixture
from ezmcd.sampling_guard import RuntimeGuard


class Message:
    def __init__(self, name='api.mcd.cn', type_=1):
        self.query=True; self.op_code=0
        self.questions=[NS(name=name,type=type_,class_=1)]
    def fail(self, code): return NS(code=code,answers=[])
    def succeed(self, answers): return NS(code=0,answers=answers)


class LocalDnsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.GuardTests.setUp(self)
        self.guard=RuntimeGuard(self.config|{'transport':'local-app-v1','schema_version':3,
            'purpose':'account_connect','identity':None,'refresh_session':None,
            'api_dns_snapshot':{'host':'api.mcd.cn','addresses':['192.0.2.1','2001:db8::1'],
                                'names':['api.mcd.cn','api-cname.example']}},clock=lambda:self.now)
        dns=NS(response_codes=NS(REFUSED=5,SERVFAIL=2),op_codes=NS(QUERY=0),classes=NS(IN=1),
               types=NS(A=1,AAAA=28,HTTPS=65),ResourceRecord=lambda **kw:NS(**kw))
        p=patch.dict(sys.modules,{'mitmproxy':NS(dns=dns)});p.start();self.addCleanup(p.stop)
        self.resolve=AsyncMock(side_effect=AssertionError('No resolver calls during interception'))

    def flow(self, name='api.mcd.cn', type_=1):
        return NS(request=Message(name,type_),response=None,
                  client_conn=NS(proxy_mode=NS(type_name='local')))

    async def test_only_fixed_api_is_resolved_and_address_families_are_filtered(self):
        with patch.object(asyncio.get_running_loop(),'getaddrinfo',self.resolve):
            for type_,address in ((1,'192.0.2.1'),(28,'2001:db8::1')):
                flow=self.flow('API.MCD.CN.',type_)
                await self.guard.dns_request(flow)
                self.assertEqual(flow.response.code,0)
                self.assertEqual([a.data for a in flow.response.answers],[ipaddress.ip_address(address).packed])
        self.resolve.assert_not_awaited()
        self.assertEqual(self.guard.audit['native_dns_answered'],2)
        self.assertEqual(self.guard.audit['upstream_connections'],0)

    async def test_other_hosts_types_queries_and_expired_capture_never_resolve(self):
        cases=[self.flow('wallet.example'),self.flow('api.mcd.cn.evil'),self.flow('child.api.mcd.cn'),self.flow(type_=16)]
        flow=self.flow();flow.request.questions.append(NS(name='outside.example',type=1,class_=1));cases.append(flow)
        flow=self.flow();flow.request.op_code=5;cases.append(flow)
        flow=self.flow();flow.request.query=False;cases.append(flow)
        flow=self.flow();flow.request.questions[0].class_=3;cases.append(flow)
        with patch.object(asyncio.get_running_loop(),'getaddrinfo',self.resolve):
            for flow in cases:
                await self.guard.dns_request(flow);self.assertEqual(flow.response.code,5)
            self.guard.deadline=self.now-1;flow=self.flow()
            await self.guard.dns_request(flow);self.assertEqual(flow.response.code,5)
        self.resolve.assert_not_awaited()

    async def test_missing_snapshot_refuses_without_network(self):
        self.guard.dns_names=set();self.guard.dns_addresses=()
        with patch.object(asyncio.get_running_loop(),'getaddrinfo',self.resolve):
            flow=self.flow();await self.guard.dns_request(flow)
            self.assertEqual(flow.response.code,5)
        self.resolve.assert_not_awaited()

    async def test_audit_failure_keeps_refusal_attached(self):
        with patch.object(self.guard,'save_audit',side_effect=OSError('disk')):
            flow=self.flow()
            with self.assertRaises(OSError):await self.guard.dns_request(flow)
            self.assertEqual(flow.response.code,5)

    async def test_only_os_canonical_alias_is_supported_and_https_has_no_address_hints(self):
        with patch.object(asyncio.get_running_loop(),'getaddrinfo',self.resolve):
            flow=self.flow('api-cname.example',65)
            await self.guard.dns_request(flow)
            self.assertEqual(flow.response.code,0);self.assertEqual(flow.response.answers,[])
            flow=self.flow('api-cname.example',1);await self.guard.dns_request(flow)
            self.assertEqual(flow.response.code,0)
            flow=self.flow('other-cname.example',1);await self.guard.dns_request(flow)
            self.assertEqual(flow.response.code,5)
        self.resolve.assert_not_awaited()

    def test_only_api_upstream_is_pinned_and_udp_is_still_blocked(self):
        connection=NS(proxy_mode=NS(type_name='local'))
        server=NS(address=('api.mcd.cn',443),sni='api.mcd.cn',transport_protocol='tcp',error=None)
        self.guard.server_connect(NS(server=server,client=connection))
        self.assertEqual(server.address,('192.0.2.1',443));self.assertEqual(server.sni,'api.mcd.cn')
        self.assertIsNone(server.error)
        for protocol,host,sni in (('udp','api.mcd.cn','api.mcd.cn'),('tcp','other.example','other.example')):
            server=NS(address=(host,443),sni=sni,transport_protocol=protocol,error=None)
            self.guard.server_connect(NS(server=server,client=connection))
            self.assertEqual(server.error,'EZMCD_LOCAL_DESTINATION_BLOCKED')
            self.assertEqual(server.address,(host,443))
