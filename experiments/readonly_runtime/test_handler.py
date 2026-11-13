import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ezmcd.storage import read_private
from handler import RequestHandler
from transaction import CONFIG_HOST, CONFIG_PATH, BALANCE
import test_transaction as fixtures
from test_transaction import ORIGINAL, NOW


class Headers:
    def __init__(self, values):self.values=list(values.items()) if isinstance(values,dict) else list(values)
    def get_all(self,name):return [v for k,v in self.values if k.lower()==name.lower()]
    def items(self,multi=False):return list(self.values)
    def pop(self,name,default=None):
        values=self.get_all(name);self.values=[(k,v) for k,v in self.values if k.lower()!=name.lower()]
        return values[0] if values else default


def response(status,content,headers=None):
    return SimpleNamespace(status_code=status,content=content,headers=headers or {},stream=False)


def flow(host,path,headers=None,method='GET'):
    f=SimpleNamespace(request=SimpleNamespace(scheme='https',port=443,host=host,
        host_header=host,method=method,path=path,stream=True,headers=Headers({'Host':host}|(headers or {}))),
        client_conn=SimpleNamespace(sni=host,proxy_mode=SimpleNamespace(type_name='local')),
        response=None,metadata={},killable=True,killed=False)
    def kill():f.killed=True
    f.kill=kill
    return f


class HandlerTests(unittest.TestCase):
    setUp=fixtures.TransactionTests.setUp
    tearDown=fixtures.TransactionTests.tearDown
    prepare=fixtures.TransactionTests.prepare
    config=fixtures.TransactionTests.config
    recover=fixtures.TransactionTests.recover
    request=fixtures.TransactionTests.request
    balance=fixtures.TransactionTests.balance
    captured=fixtures.TransactionTests.captured

    def http(self):
        return patch.dict('sys.modules',{'mitmproxy':SimpleNamespace(http=SimpleNamespace(
            Response=SimpleNamespace(make=response)))})

    def config_flow(self,h):
        f=flow(CONFIG_HOST,CONFIG_PATH)
        h.requestheaders(f)
        self.assertIsNone(f.response)
        f.response=response(200,ORIGINAL,{'etag':'old-etag','last-modified':'old-date'})
        h.responseheaders(f);h.response(f)
        return f

    def test_complete_handler_sequence_never_forwards_runtime_get_or_any_write(self):
        with self.http():
            h=RequestHandler(self.prepare()); c=self.config_flow(h)
            self.assertNotEqual(c.response.content,ORIGINAL)
            self.assertEqual(c.response.headers,{'cache-control':'no-store'})
            self.now+=1
            f=flow('api.mcd.cn',BALANCE,self.request()['headers'])
            h.requestheaders(f)
            self.assertEqual(f.response.status_code,409)
            self.assertTrue(h.audit['runtime_captured'])
            self.assertEqual(h.audit['api_reads_forwarded'],0)
            recovery=RequestHandler(self.recover());c=self.config_flow(recovery)
            self.assertEqual(c.response.content,ORIGINAL)
            self.assertNotIn('cache-control',c.response.headers)
            b=self.balance();f=flow('api.mcd.cn',BALANCE,b['request']['headers'])
            recovery.requestheaders(f);self.assertIsNone(f.response)
            f.response=response(200,json.dumps(b['response']['body']).encode())
            recovery.responseheaders(f);recovery.response(f)
            self.assertTrue(recovery.audit['restoration_verified'])
            self.assertFalse(recovery.transaction.summary()['import_ready'])

    def test_all_mutation_methods_block_before_headers_are_read(self):
        with self.http():
            h=RequestHandler(self.prepare())
            for method in ('POST','PUT','PATCH','DELETE','CONNECT'):
                for host,path in [('api.mcd.cn','/bff/order/orders'),('api.mcd.cn','/bff/cashier/preorder'),
                    ('spurcard.chinaums.com','/v1/h5/front/consume/transConsume'),(CONFIG_HOST,CONFIG_PATH)]:
                    f=flow(host,path,method=method);f.request.headers=None
                    h.requestheaders(f)
                    self.assertEqual(f.response.status_code,409)
                    self.assertTrue(f.killed)
            self.assertEqual(h.audit['config_reads_forwarded'],0)
            self.assertEqual(h.audit['api_reads_forwarded'],0)

    def test_origin_confusion_and_preflight_surface_cannot_capture(self):
        with self.http():
            h=RequestHandler(self.prepare())
            for alter in ('sni','authority','ip-host','scheme','port','regular','duplicate-host'):
                f=flow(CONFIG_HOST,CONFIG_PATH)
                if alter=='sni':f.client_conn.sni='other.example'
                elif alter=='authority':f.request.host_header='other.example'
                elif alter=='ip-host':f.request.host='lookalike.example'
                elif alter=='scheme':f.request.scheme='http'
                elif alter=='port':f.request.port=444
                elif alter=='regular':f.client_conn.proxy_mode.type_name='regular'
                else:f.request.headers.values.append(('HOST',CONFIG_HOST))
                h.requestheaders(f)
                self.assertEqual(f.response.status_code,409,alter)
            self.assertEqual(h.audit['config_reads_forwarded'],0)

    def test_transparent_ip_requires_matching_sni_and_http_authority(self):
        with self.http():
            h=RequestHandler(self.prepare());f=flow(CONFIG_HOST,CONFIG_PATH)
            f.request.host='203.0.113.1';h.requestheaders(f)
            self.assertIsNone(f.response)
            self.assertEqual(f.request.host,CONFIG_HOST)

    def test_public_config_request_rejects_credentials_body_and_other_paths(self):
        with self.http():
            h=RequestHandler(self.prepare())
            for key in ('sid','token','authorization','cookie','d','x-mcd-sign','transfer-encoding','content-length'):
                f=flow(CONFIG_HOST,CONFIG_PATH,{key:'private'})
                h.requestheaders(f);self.assertEqual(f.response.status_code,409,key)
            for path in (CONFIG_PATH+'?x=1',CONFIG_PATH+'/', '/other'):
                f=flow(CONFIG_HOST,path);h.requestheaders(f);self.assertEqual(f.response.status_code,409)
            self.assertEqual(h.audit['config_reads_forwarded'],0)

    def test_conditional_config_request_becomes_actual_get(self):
        with self.http():
            h=RequestHandler(self.prepare());f=flow(CONFIG_HOST,CONFIG_PATH,{
                'If-None-Match':'etag','If-Modified-Since':'date'})
            h.requestheaders(f);self.assertIsNone(f.response)
            self.assertEqual(f.request.headers.get_all('if-none-match'),[])
            self.assertEqual(f.request.headers.get_all('if-modified-since'),[])

    def test_duplicate_unknown_headers_rejected_before_forwarding(self):
        with self.http():
            h=RequestHandler(self.prepare());f=flow(CONFIG_HOST,CONFIG_PATH)
            f.request.headers.values.extend([('Accept','a'),('accept','b')])
            h.requestheaders(f);self.assertEqual(f.response.status_code,409)

    def test_capture_audit_failure_leaves_blocking_response(self):
        with self.http():
            h=RequestHandler(self.prepare());f=flow(CONFIG_HOST,CONFIG_PATH)
            with patch('handler.write_private',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):h.requestheaders(f)
            self.assertEqual(f.response.status_code,409)

    def test_config_claim_failure_does_not_release_candidate(self):
        with self.http():
            h=RequestHandler(self.prepare());f=flow(CONFIG_HOST,CONFIG_PATH);h.requestheaders(f)
            f.response=response(200,ORIGINAL)
            with patch('transaction.write_private',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):h.response(f)
            self.assertEqual(f.response.status_code,409)
            self.assertNotIn(b'tdSignConfig',f.response.content)
            self.assertFalse(h.transaction.can_override)

    def test_late_config_response_cannot_resend_candidate_after_runtime_capture(self):
        with self.http():
            h=RequestHandler(self.prepare());self.config_flow(h)
            late=flow(CONFIG_HOST,CONFIG_PATH);h.requestheaders(late)
            self.now+=1
            f=flow('api.mcd.cn',BALANCE,self.request()['headers']);h.requestheaders(f)
            late.response=response(200,ORIGINAL);h.response(late)
            self.assertEqual(late.response.content,ORIGINAL)
            self.assertFalse(h.transaction.summary()['restoration_verified'])
            self.assertEqual(h.transaction.state['candidate_responses'],1)

    def test_failed_recovery_validation_passes_official_response_unchanged_without_success(self):
        self.captured()
        with self.http():
            h=RequestHandler(self.recover());f=flow(CONFIG_HOST,CONFIG_PATH);h.requestheaders(f)
            raw=b'{"new":"unsupported official schema"}';f.response=response(200,raw)
            h.response(f)
            self.assertEqual(f.response.content,raw)
            self.assertEqual(h.transaction.state['state'],'RECOVERY_REQUIRED')
            self.assertFalse(h.audit['restoration_verified'])

    def test_balance_response_without_runtime_sample_can_only_prove_recovery(self):
        t=self.prepare();self.config(t)
        with self.http():
            h=RequestHandler(self.recover());self.config_flow(h)
            b=self.balance();f=flow('api.mcd.cn',BALANCE,b['request']['headers']);h.requestheaders(f)
            f.response=response(200,json.dumps(b['response']['body']).encode());h.response(f)
            self.assertTrue(h.audit['restoration_verified'])
            self.assertFalse(h.transaction.summary()['candidate_verified_after_restoration'])

    def test_wallet_diagnostics_contain_counts_and_known_paths_only(self):
        with self.http():
            h=RequestHandler(self.prepare())
            b=self.balance();f=flow('api.mcd.cn',BALANCE,b['request']['headers'])
            h.requestheaders(f)
            self.assertIsNone(f.response)
            self.assertEqual(h.audit['wallet_requests'],{'v4':1,'v5':0,'other':0})
            self.assertEqual(h.audit['known_api_read_counts'],{BALANCE:1})
            saved=json.dumps(h.audit)
            for value in ('synthetic-session','synthetic-device','synthetic-risk','synthetic-member'):
                self.assertNotIn(value,saved)

    def test_ambiguous_wallet_signature_and_query_never_escape_to_upstream(self):
        with self.http():
            h=RequestHandler(self.prepare());self.config_flow(h);self.now+=1
            for version,path in [('v4',BALANCE),(None,BALANCE),('v5',BALANCE+'?private=value')]:
                headers=self.request()['headers']
                if version is None:headers.pop('sv')
                else:headers['sv']=version
                f=flow('api.mcd.cn',path,headers);h.requestheaders(f)
                self.assertEqual(f.response.status_code,409)
            self.assertEqual(h.audit['api_reads_forwarded'],0)
            self.assertEqual(h.audit['wallet_signatures_seen'],3)
            self.assertEqual(h.audit['wallet_requests_with_query'],1)
            self.assertNotIn('private=value',json.dumps(h.audit))


if __name__=='__main__':unittest.main()
