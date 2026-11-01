import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import urlsplit
from ezmcd.sampling_guard import RuntimeGuard, BALANCE, ORDER, REFRESH
from ezmcd.models import fingerprint
from ezmcd.protocol.v5 import ObservedRuntime, encode
from ezmcd.protocol.signing import sign_v4
from test_project_accounts import sample, KEYS


class Headers(dict):
    def __init__(self, values=None):
        super().__init__((k.lower(), v) for k, v in (values or {}).items())
        self.extra = {}
    def get_all(self, key):
        return ([self[key]] if key in self else []) + self.extra.get(key, [])
    def add(self, key, value):
        self.extra.setdefault(key, []).append(value)


def response(status, content, headers=None):
    return SimpleNamespace(status_code=status, content=content, headers=headers or {}, stream=False)


http = SimpleNamespace(Response=SimpleNamespace(make=response))


def flow_for(method, url, headers=None):
    parsed = urlsplit(url)
    flow = SimpleNamespace(request=SimpleNamespace(method=method, scheme=parsed.scheme, host=parsed.hostname,
        port=parsed.port or 443, path=parsed.path, headers=Headers(headers), stream=True),
        metadata={}, response=None, killable=True, killed=False)
    def kill():
        flow.killed = True
    flow.kill = kill
    return flow


class GuardTests(unittest.TestCase):
    def setUp(self):
        module_patch=patch.dict('sys.modules', {'mitmproxy':SimpleNamespace(http=http)})
        module_patch.start(); self.addCleanup(module_patch.stop)
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory=Path(temp.name).resolve(); self.now=time.time()
        self.config={'output':str(self.directory),'binary':'unused',
            'identity':fingerprint({'provider':'mcd-cn','member':'synthetic-member'}),'deadline':self.now+120}
        self.guard=RuntimeGuard(self.config,clock=lambda:self.now)
        self.keys=KEYS[:2]+(tuple(range(64)),())
        self.patch=patch('ezmcd.accounts.constants',return_value=self.keys);self.patch.start();self.addCleanup(self.patch.stop)

    def flow(self, method, path, headers=None):
        return flow_for(method,'https://api.mcd.cn'+path,headers)

    def balance(self, **overrides):
        evidence=sample()
        if overrides:
            headers=evidence['request']['headers']
            headers.update(overrides)
            headers.update(sign_v4(headers,*KEYS[:2],datetime.now(timezone.utc),'GET',BALANCE))
        flow=self.flow('GET',BALANCE,evidence['request']['headers'])
        self.guard.requestheaders(flow); self.assertIsNone(flow.response)
        flow.response=http.Response.make(200,evidence['response']['body'].encode(),{'content-type':'application/json'})
        self.guard.response(flow)

    def runtime(self):
        value=encode(ORDER,ObservedRuntime('A'*32,'b'*16),milliseconds=int(self.now*1000),random_value=3,counter=1,constants=self.keys[2])
        return self.flow('POST',ORDER,{'sid':'synthetic-session','token':'synthetic-device','x-mcd-sign':value,'cookie':'discard-me'})

    def test_valid_balance_then_runtime_captured_without_forwarding_or_bodies(self):
        self.balance(); self.assertTrue(self.guard.audit['balance_saved'])
        flow=self.runtime();flow.request.content=b'NEVER-SAVE-ORDER-BODY'
        self.guard.requestheaders(flow)
        self.assertEqual(flow.response.status_code,409);self.assertFalse(flow.request.stream)
        self.assertTrue(flow.killed)
        self.assertTrue(self.guard.audit['runtime_saved'])
        saved=json.loads((self.directory/'runtime.json').read_text())
        self.assertEqual(set(saved['request']['headers']),{'sid','token','x-mcd-sign'})
        self.assertEqual(saved['request']['body'],'')
        self.assertNotIn('NEVER-SAVE',json.dumps(saved));self.assertNotIn('discard-me',json.dumps(saved))
        self.assertEqual((self.directory/'runtime.json').stat().st_mode&0o777,0o600)
        self.assertNotIn('balance',json.dumps(json.loads((self.directory/'balance.json').read_text())['response']))

    def test_missing_balance_and_repeated_writes_are_always_blocked(self):
        for path in (ORDER,'/bff/cashier/preorder','/bff/cart/carts','/unrecognized'):
            for method in ('POST','PUT','DELETE','PATCH'):
                flow=self.flow(method,path);self.guard.requestheaders(flow)
                self.assertEqual(flow.response.status_code,409);self.assertFalse(flow.request.stream)
        self.assertFalse((self.directory/'runtime.json').exists())

    def test_expiry_continues_blocking_and_does_not_renew_samples(self):
        self.balance();self.now+=121
        for flow in (self.runtime(),self.flow('GET',BALANCE)):
            self.guard.requestheaders(flow);self.assertEqual(flow.response.status_code,409)
        self.assertFalse((self.directory/'runtime.json').exists())

    def test_invalid_signature_and_duplicate_or_mismatched_session_never_save(self):
        self.balance()
        for change in ('signature','duplicate','session'):
            flow=self.runtime()
            if change=='signature':flow.request.headers['x-mcd-sign']='bad'
            if change=='duplicate':flow.request.headers.add('sid','duplicate')
            if change=='session':flow.request.headers['sid']='wrong'
            self.guard.requestheaders(flow)
            self.assertEqual(flow.response.status_code,409)
            self.assertFalse((self.directory/'runtime.json').exists())

    def test_storage_failure_cannot_release_a_blocked_write(self):
        flow=self.runtime()
        with patch.object(self.guard,'save_audit',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):self.guard.requestheaders(flow)
        self.assertEqual(flow.response.status_code,409);self.assertFalse(flow.request.stream)

    def test_malformed_path_and_unlisted_get_fail_closed(self):
        flow=self.flow('POST',ORDER);flow.request.path='//['
        with self.assertRaises(ValueError):self.guard.requestheaders(flow)
        self.assertEqual(flow.response.status_code,409)
        flow=self.flow('GET','/bff/member/mwallet/token');self.guard.requestheaders(flow)
        self.assertEqual(flow.response.status_code,409)

    def test_other_member_balance_is_not_captured(self):
        self.guard.identity='wrong-member'
        self.balance();self.assertFalse(self.guard.audit['balance_saved'])
        self.assertFalse((self.directory/'balance.json').exists())

    def test_empty_optional_headers_do_not_reject_a_valid_official_signature(self):
        evidence=sample()
        flow=self.flow('GET',BALANCE,{**evidence['request']['headers'],'routingid':'','tid':''})
        self.guard.requestheaders(flow)
        flow.response=http.Response.make(200,evidence['response']['body'].encode())
        self.guard.response(flow)
        self.assertTrue(self.guard.audit['balance_saved'])
        saved=json.loads((self.directory/'balance.json').read_text())
        self.assertNotIn('routingid',saved['request']['headers'])

    def test_wallet_tunnels_are_blocked_without_tls_decryption(self):
        for host in ('spurcard.chinaums.com','wbapi.archcard.mcdonalds.com.cn','unknown.example'):
            flow=flow_for('CONNECT','https://'+host)
            self.guard.http_connect(flow);self.assertEqual(flow.response.status_code,409)

    def test_balance_diagnostics_explain_missing_headers_without_values(self):
        evidence=sample()
        headers=dict(evidence['request']['headers']); headers.pop('sid')
        flow=self.flow('GET',BALANCE,headers)
        self.guard.requestheaders(flow)
        flow.response=http.Response.make(200,evidence['response']['body'].encode())
        self.guard.response(flow)
        self.assertFalse(self.guard.audit['balance_saved'])
        self.assertEqual(self.guard.audit['balance_header_checks']['missing'],['sid'])
        audit=json.dumps(self.guard.audit)
        for value in ('synthetic-session','synthetic-device','synthetic-member'):
            self.assertNotIn(value,audit)

    def test_navigation_gets_allowed_but_same_routes_post_still_blocked(self):
        for path in ('/bff/member/store/collection','/bff/common/normal/config'):
            flow=self.flow('GET',path);self.guard.requestheaders(flow)
            self.assertIsNone(flow.response)
            flow=self.flow('POST',path);self.guard.requestheaders(flow)
            self.assertEqual(flow.response.status_code,409)

    def test_explicit_session_refresh_is_bounded_and_does_not_allow_orders(self):
        headers={'sid':'synthetic-session','token':'synthetic-device'}
        self.guard.refresh_session=fingerprint(headers)
        wrong=self.flow('POST',REFRESH,dict(headers,sid='wrong'))
        self.guard.requestheaders(wrong);self.assertEqual(wrong.response.status_code,409)
        flow=self.flow('POST',REFRESH,headers)
        self.guard.requestheaders(flow);self.assertIsNone(flow.response)
        self.assertFalse(flow.request.stream)
        for path in (REFRESH,ORDER,'/bff/cart/carts','/bff/cashier/preorder'):
            flow=self.flow('POST',path,headers);self.guard.requestheaders(flow)
            self.assertEqual(flow.response.status_code,409)
        self.assertEqual(self.guard.audit['allowed_session_refreshes'],1)

    def test_refresh_requires_explicit_scope_and_live_window(self):
        headers={'sid':'synthetic-session','token':'synthetic-device'}
        flow=self.flow('POST',REFRESH,headers);self.guard.requestheaders(flow)
        self.assertEqual(flow.response.status_code,409)
        self.guard.refresh_session=fingerprint(headers);self.now+=121
        flow=self.flow('POST',REFRESH,headers);self.guard.requestheaders(flow)
        self.assertEqual(flow.response.status_code,409)

    def test_refresh_audit_failure_remains_blocked(self):
        headers={'sid':'synthetic-session','token':'synthetic-device'}
        self.guard.refresh_session=fingerprint(headers)
        flow=self.flow('POST',REFRESH,headers)
        with patch.object(self.guard,'save_audit',side_effect=OSError()):
            with self.assertRaises(OSError):self.guard.requestheaders(flow)
        self.assertEqual(flow.response.status_code,409)
        self.assertTrue(self.guard.refresh_used)

    def test_synthetic_block_and_balance_responses_cannot_be_streamed(self):
        for flow in (self.runtime(),self.flow('GET',BALANCE)):
            self.guard.requestheaders(flow)
            flow.response=flow.response or http.Response.make(200,b'{"success":true}')
            flow.response.stream=True
            self.guard.responseheaders(flow)
            self.assertFalse(flow.response.stream)

    def refresh(self, *, data=None, token='synthetic-device'):
        headers={'sid':'synthetic-old-session','token':token}
        self.guard.refresh_session=fingerprint(headers)
        flow=self.flow('POST',REFRESH,headers)
        self.guard.requestheaders(flow)
        self.assertIsNone(flow.response)
        payload={'success':True,'data':data if data is not None else {
            'sid':'synthetic-session','refreshToken':'synthetic-new-refresh','meddyId':'synthetic-member',
            'phone':'must-not-save-phone','cookie':'must-not-save-cookie'}}
        flow.response=http.Response.make(200,json.dumps(payload).encode())
        return flow

    def test_refresh_credentials_wait_for_signed_balance_and_are_minimal(self):
        flow=self.refresh()
        self.guard.responseheaders(flow)
        self.assertFalse(flow.response.stream)
        self.guard.response(flow)
        self.assertFalse((self.directory/'session.json').exists())
        self.balance()
        path=self.directory/'session.json'
        saved=json.loads(path.read_text())
        self.assertEqual(saved['fields'],{'sid':'synthetic-session','refreshToken':'synthetic-new-refresh'})
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertTrue(self.guard.audit['session_saved'])
        self.assertIsNone(self.guard.session_candidate)
        for value in ('must-not-save','synthetic-old-session','synthetic-device','synthetic-member'):
            self.assertNotIn(value,json.dumps(saved))
        self.assertNotIn('synthetic-',json.dumps(self.guard.audit))
        # Neither a duplicate callback nor a second request can rotate again.
        self.guard.response(flow)
        again=self.flow('POST',REFRESH,flow.request.headers)
        self.guard.requestheaders(again)
        self.assertTrue(again.killed)
        self.assertEqual(self.guard.audit['allowed_session_refreshes'],1)

    def test_balance_response_may_arrive_before_refresh_response(self):
        flow=self.refresh()
        self.balance()
        self.guard.response(flow)
        self.assertTrue(self.guard.audit['session_saved'])

    def test_refresh_matches_rotated_sid_not_source_sid(self):
        flow=self.refresh(data={'sid':'synthetic-rotated','refreshToken':'new-token'})
        self.guard.response(flow)
        self.balance(sid='synthetic-rotated')
        self.assertTrue(self.guard.audit['session_saved'])

    def test_refresh_mismatched_device_or_session_never_persists_token(self):
        for key,value in (('token','other-device'),('sid','other-session')):
            with self.subTest(key=key):
                self.guard=RuntimeGuard(self.config,clock=lambda:self.now)
                flow=self.refresh()
                self.guard.response(flow)
                self.balance(**{key:value})
                self.assertEqual(self.guard.audit['session_result'],'balance_session_mismatch')
                self.assertFalse((self.directory/'session.json').exists())
                self.assertIsNone(self.guard.session_candidate)

    def test_refresh_never_accepts_unsolicited_response(self):
        flow=self.flow('POST',REFRESH,{'sid':'synthetic-session','token':'synthetic-device'})
        flow.response=http.Response.make(200,b'{"success":true,"data":{"sid":"x","refreshToken":"y"}}')
        self.guard.response(flow)
        self.balance()
        self.assertFalse((self.directory/'session.json').exists())
        self.assertNotIn('refresh_succeeded',self.guard.audit)

    def test_invalid_refresh_response_discarded_without_credential_diagnostics(self):
        for case in ('oversize','bad_json','list','status','expired','wrong_host','blocked','changed_headers',
                     'wrong_member','empty_token','control','delete','long','missing_sid','challenge','risk'):
            with self.subTest(case=case):
                self.guard=RuntimeGuard(self.config,clock=lambda:self.now)
                flow=self.refresh()
                payload=json.loads(flow.response.content)
                if case=='oversize': flow.response.content=b' '*65537
                elif case=='bad_json': flow.response.content=b'not-json'
                elif case=='list': flow.response.content=b'[]'
                elif case=='status': flow.response.status_code=500
                elif case=='expired': self.guard.deadline=self.now-1
                elif case=='wrong_host': flow.request.host='other.example'
                elif case=='blocked': flow.metadata['ezmcd_blocked']=True
                elif case=='changed_headers': flow.request.headers['token']='other-device'
                else:
                    if case=='wrong_member': payload['data']['meddyId']='other-member'
                    if case=='empty_token': payload['data']['refreshToken']=''
                    if case=='control': payload['data']['refreshToken']='secret\nvalue'
                    if case=='delete': payload['data']['refreshToken']='secret\x7fvalue'
                    if case=='long': payload['data']['refreshToken']='a'*16385
                    if case=='missing_sid': payload['data'].pop('sid')
                    if case=='challenge': payload['data']['needVerify']=True
                    if case=='risk': payload['code']='707053'
                    flow.response.content=json.dumps(payload).encode()
                self.guard.response(flow)
                self.assertFalse((self.directory/'session.json').exists())
                self.assertIsNone(self.guard.session_candidate)
                self.assertNotIn('synthetic-',json.dumps(self.guard.audit))

    def test_failed_session_storage_keeps_guard_blocking(self):
        flow=self.refresh()
        self.balance()
        from ezmcd.storage import write_private
        def write(path,data):
            if path.name=='session.json': raise OSError('synthetic-token must not appear')
            return write_private(path,data)
        with patch('ezmcd.sampling_guard.write_private',side_effect=write):
            self.guard.response(flow)
        self.assertEqual(self.guard.audit['session_result'],'session_storage_failed')
        order=self.runtime(); self.guard.requestheaders(order)
        self.assertTrue(order.killed)
        self.assertNotIn('synthetic-token',json.dumps(self.guard.audit))


if __name__=='__main__':unittest.main()
