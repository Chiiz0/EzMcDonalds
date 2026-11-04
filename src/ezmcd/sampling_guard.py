"""Experimental request-side runtime capture. No proxy/trust changes here.

Blocks financial/cart mutations before body forwarding, including retries.
An allowlist of API GETs and an optional single matching session refresh pass.
Expiry stops collection/read forwarding while the guard is alive. Public
native capture is wallet-only; it never asks users to test order submission.
"""
from pathlib import Path
import json
import re
import time
from urllib.parse import urlsplit

from ezmcd.accounts import profile_from_balance_sample, runtime_from_sample
from ezmcd.models import fingerprint
from ezmcd.errors import EzMcdError
from ezmcd.storage import write_private
from ezmcd.sampling_session import valid_credential, validate_session
from ezmcd.sampling_diagnostics import NATIVE_COUNTERS

BALANCE = '/bff/member/mwallet/balance'
ORDER = '/bff/order/orders'
REFRESH = '/bff/passport/login/session/refresh'
HEADER_NAMES = {'ct','v','language','sv','sid','token','meddyid','routingid','tid','d',
                'p','x-mcd-gw-v','authorization','x-hmac-digest'}
READS = {BALANCE, ORDER, '/bff/order/confirmation/info', '/bff/cart/carts',
         '/bff/spc/menu', '/bff/store/stores/collection/list', '/bff/promotion/coupons/rightCards',
         '/bff/promotion/coupons/menu/available', '/bff/promotion/coupons/products',
         '/bff/store/stores','/bff/store/cities','/bff/store/cities/group','/bff/store/stores/vicinity',
         '/bff/member/user/portal/info','/bff/member/user/portal/v2','/bff/member/user/portal/v3',
         '/bff/member/user/config/get','/bff/order/confirmation/pageconfig'}
# Observed official startup/navigation GETs. No wildcard or mutation exemption.
READS.update({'/bff/member/store/collection', '/bff/common/normal/config',
              '/bff/common/normal/ab/experiments', '/bff/common/normal/share',
              '/bff/portal/account/center/base', '/bff/portal/home/index',
              '/bff/portal/home/index/user', '/bff/portal/home/orderinfo',
              '/bff/portal/home/splash', '/bff/portal/version/upgrade'})
READS.update({'/bff/store/stores/portal/base','/bff/store/stores/hottags'})
READS.update({'/bff/order/confirmation/validationinfo', '/bff/order/confirmation/carts',
              '/bff/order/confirmation/heartcard', '/bff/order/confirmation/promotion',
              '/bff/order/confirmation/rightcard', '/bff/order/confirmation/agreement',
              '/bff/promotion/coupons/card','/bff/spc/menu/campaign','/bff/spc/frequent'})


class RuntimeGuard:
    def __init__(self, config, *, clock=time.time):
        self.output = Path(config['output'])
        self.local_capture = config.get('transport') == 'local-app-v1'
        self.binary = config['binary']
        self.identity = config['identity']
        maintenance = config.get('purpose') == 'account_maintenance'
        self.balance_only = maintenance or config.get('purpose') == 'account_connect'
        if self.balance_only:
            bound = (config.get('schema_version') == 4 and isinstance(self.identity, str)
                     and re.fullmatch(r'[0-9a-f]{64}', self.identity)) if maintenance else (
                         config.get('schema_version') == 3 and self.identity is None)
            if not bound or config.get('refresh_session') is not None:
                raise ValueError('invalid_connection_capture_scope')
        self.deadline = config['deadline']
        self.clock = clock
        self.profile = None
        self.refresh_session = config.get('refresh_session')
        self.refresh_used = False
        self.refresh_flow = None
        self.session_candidate = None
        self.runtime_saved = False
        self.audit = {'blocked_requests':0, 'allowed_gets':0, 'balance_saved':False,
                      'runtime_saved':False, 'upstream_connections':0, 'last_result':'starting'}
        if self.local_capture:
            self.audit.update({key: 0 for key in NATIVE_COUNTERS})

    def count_native(self, key, client):
        # The regular loopback preflight must never count as native traffic.
        if self.local_capture and getattr(getattr(client, 'proxy_mode', None), 'type_name', None) == 'local':
            self.audit[key] = min(1000000, self.audit[key] + 1)
            self.save_audit()

    def client_connected(self, client):
        self.count_native('native_connections', client)
        if getattr(client, 'transport_protocol', None) == 'udp':
            self.count_native('native_udp_connections', client)

    def tls_clienthello(self, data):
        key = 'native_tls_api' if data.client_hello.sni == 'api.mcd.cn' else 'native_tls_other'
        self.count_native(key, data.context.client)

    def tls_established_client(self, data):
        self.count_native('native_tls_completed', data.context.client)

    def tls_failed_client(self, data):
        self.count_native('native_tls_errors', data.context.client)

    def server_connect_error(self, data):
        self.count_native('native_upstream_errors', data.client)

    def save_audit(self):
        write_private(self.output/'audit.json', self.audit)

    def running(self):
        self.audit['last_result'] = 'ready'
        self.save_audit()

    @staticmethod
    def block(flow, *, terminate=False):
        from mitmproxy import http
        # Must happen before any parsing, validation or disk I/O. A later
        # exception must not turn an intercepted write into a forwarded request.
        flow.request.stream = False
        flow.response = http.Response.make(409,
            b'{"success":false,"code":"EZMCD_CAPTURE_BLOCKED","message":"Local capture: request not sent"}',
            {'content-type':'application/json','x-ezmcd-local-block':'1'})
        flow.metadata['ezmcd_blocked'] = True
        if terminate and flow.killable:
            flow.kill()

    def http_connect(self, flow):
        # Only the API tunnel may pass. Other destinations, including wallets,
        # are rejected without decrypting any of their traffic.
        host = flow.request.host.lower()
        if host != 'api.mcd.cn' or flow.request.port != 443:
            self.block(flow)

    def server_connect(self, data):
        # Local capture has no CONNECT tunnel. Never let ignored TLS, raw TCP
        # or UDP traffic become an unguarded route to wallet/payment hosts.
        if self.local_capture:
            previous_error = data.server.error
            data.server.error = 'EZMCD_LOCAL_DESTINATION_BLOCKED'
            address = data.server.address
            if (data.server.transport_protocol != 'tcp' or not isinstance(address, tuple)
                    or len(address) != 2 or address[1] != 443
                    or (address[0] != 'api.mcd.cn' and data.server.sni != 'api.mcd.cn')):
                self.count_native('native_destinations_blocked', getattr(data, 'client', None))
                return
            data.server.error = previous_error
        self.audit['upstream_connections'] += 1
        self.save_audit()

    def requestheaders(self, flow):
        req = flow.request
        # Terminate writes at the headers hook. Merely replacing the response
        # can still enter the proxy's request-body streaming state machine.
        refresh_candidate = (self.refresh_session and not self.refresh_used and
            (req.scheme,req.host,req.port,req.method,req.path) == ('https','api.mcd.cn',443,'POST',REFRESH))
        self.block(flow, terminate=req.method != 'GET' and not refresh_candidate)
        self.count_native('native_http_requests', getattr(flow, 'client_conn', None))
        path = urlsplit(req.path).path
        live = self.clock() <= self.deadline
        official = req.scheme == 'https' and req.host == 'api.mcd.cn' and req.port == 443
        if refresh_candidate and live:
            try:
                headers=self.headers(req,{'sid','token'})
                matching=(set(headers)=={'sid','token'} and fingerprint(headers)==self.refresh_session)
                self.audit['refresh_session_matched']=matching
                if matching:
                    # Claim before forwarding; unknown outcomes are never retried.
                    self.refresh_used=True
                    self.audit['allowed_session_refreshes']=1
                    self.save_audit()
                    self.refresh_flow=flow
                    flow.response=None
                    flow.metadata.pop('ezmcd_blocked',None)
                    return
            except Exception:
                self.block(flow,terminate=True)
                raise
        safe_read = path in READS or re.fullmatch(r'/bff/store/stores/[0-9]{7}(?:/menu/reservationTimeOption)?', path)
        safe_read = safe_read or re.fullmatch(r'/bff/order/orders/[A-Za-z0-9_-]{1,80}',path)
        if official and live and req.method == 'GET' and safe_read:
            req.stream = False
            self.audit['allowed_gets'] += 1
            self.save_audit()
            flow.response = None
            flow.metadata.pop('ezmcd_blocked', None)
            return
        self.block(flow, terminate=True)
        self.audit['blocked_requests'] += 1
        # Endpoint names only: omit queries and redact variable path segments.
        route=re.sub(r'[A-Za-z0-9_-]{20,}|[0-9]{6,}','{id}',path)
        if len(route)<160 and len(self.audit.setdefault('blocked_routes',{}))<100:
            key=req.method+' '+route
            self.audit['blocked_routes'][key]=self.audit['blocked_routes'].get(key,0)+1
        self.audit['last_result'] = 'expired_blocking' if not live else 'blocked'
        if official and live and req.method == 'POST' and req.path == ORDER and not self.runtime_saved and not self.balance_only:
            try:
                if self.profile is None:
                    raise ValueError('balance_not_captured')
                headers = self.headers(req, {'sid','token','x-mcd-sign'})
                sample = {'request':{'method':'POST','host':'api.mcd.cn','path':ORDER,
                                     'headers':headers,'body':''},'request_blocked_before_upstream':True}
                runtime_from_sample(self.profile,sample,self.binary)
                write_private(self.output/'runtime.json',sample)
                self.runtime_saved = self.audit['runtime_saved'] = True
                self.audit['last_result'] = 'runtime_captured_request_blocked'
            except (ValueError, KeyError, TypeError, OSError) as error:
                self.audit['last_result'] = 'runtime_rejected'
            except Exception:
                # Never render arbitrary exception/credential text into logs.
                self.audit['last_result'] = 'runtime_rejected'
        self.save_audit()

    @staticmethod
    def headers(req, names):
        result = {}
        for name in names:
            values = req.headers.get_all(name)
            if len(values)>1 or any(len(value)>16384 or any(ord(c)<32 for c in value) for value in values):
                raise ValueError('invalid_headers')
            if values and values[0]:
                result[name] = values[0]
        return result

    def responseheaders(self, flow):
        self.audit['response_header_events'] = self.audit.get('response_header_events',0)+1
        if flow.metadata.get('ezmcd_blocked') or (
                flow.request.host == 'api.mcd.cn' and flow.request.path in (BALANCE, REFRESH)):
            flow.response.stream = False
        self.save_audit()

    def request(self, flow):
        self.audit['complete_request_events'] = self.audit.get('complete_request_events',0)+1
        self.save_audit()

    def response(self, flow):
        req = flow.request
        if flow is self.refresh_flow:
            # Only the exact flow claimed before forwarding is eligible. Never
            # turn this response hook into a second refresh request or a retry.
            self.refresh_flow = None
            try:
                if (self.clock() > self.deadline or flow.metadata.get('ezmcd_blocked')
                        or (req.scheme,req.host,req.port,req.method,req.path) != ('https','api.mcd.cn',443,'POST',REFRESH)
                        or fingerprint(self.headers(req, {'sid','token'})) != self.refresh_session
                        or flow.response.status_code != 200 or len(flow.response.content or b'') > 65536):
                    raise ValueError('invalid_refresh_response')
                payload=json.loads(flow.response.content)
                if not isinstance(payload,dict) or payload.get('success') is not True:
                    raise ValueError('invalid_refresh_response')
                self.audit['refresh_succeeded'] = True
                data = payload.get('data')
                if (not isinstance(data,dict) or data.get('securityContext') or data.get('needVerify')
                        or str(payload.get('code')) in ('420','707053')
                        or not all(valid_credential(data.get(k)) for k in ('sid','refreshToken'))
                        or ('meddyId' in data and fingerprint({'provider':'mcd-cn','member':data['meddyId']}) != self.identity)):
                    raise ValueError('invalid_refresh_response')
                # Drop every response field except the credentials and binding.
                self.session_candidate = {'schema_version':1, 'identity':self.identity,
                    'source_session':self.refresh_session,
                    'target_session':fingerprint({'sid':data['sid'],'token':req.headers['token']}),
                    'fields':{k:data[k] for k in ('sid','refreshToken')}}
                self.audit['session_result'] = 'waiting_for_matching_balance'
                self.save_session()
            except (ValueError,TypeError,AttributeError,KeyError,EzMcdError,OSError):
                self.audit['refresh_succeeded']=False
                self.audit['session_result']='refresh_response_rejected'
                self.session_candidate=None
            self.save_audit()
            return
        if (self.profile is not None or self.clock()>self.deadline or flow.metadata.get('ezmcd_blocked')
                or (req.scheme,req.host,req.port,req.method,req.path) != ('https','api.mcd.cn',443,'GET',BALANCE)):
            return
        try:
            # Presence/shape only; never persist actual session/device values.
            self.audit['balance_header_checks'] = {
                'missing': sorted(k for k in ('sid','token','meddyid','v','language')
                                  if not req.headers.get(k)),
                'ct_101': req.headers.get('ct') == '101',
                'sv_v4': req.headers.get('sv') == 'v4',
                'authorization_six_parts': len(req.headers.get('authorization','').split('#')) == 6,
                'duplicates': sorted(k for k in HEADER_NAMES if len(req.headers.get_all(k)) > 1)}
            if flow.response.status_code != 200 or len(flow.response.content or b'')>65536:
                raise ValueError('balance_response_invalid')
            payload = json.loads(flow.response.content)
            if not isinstance(payload,dict) or payload.get('success') is not True:
                raise ValueError('balance_not_successful')
            # Keep only the necessary request headers. Discard account balance,
            # response body, cookies and every request body.
            sample = {'request':{'method':'GET','host':'api.mcd.cn','path':BALANCE,
                      'headers':self.headers(req,HEADER_NAMES),'body':''},
                      'response':{'status':200,'body':{'success':True}}}
            profile = profile_from_balance_sample(sample,self.binary)
            identity = fingerprint({'provider':'mcd-cn','member':profile['fields']['meddyId']})
            if self.identity is not None and identity != self.identity:
                raise ValueError('wrong_member')
            write_private(self.output/'balance.json',sample)
            self.profile = profile
            if self.balance_only:
                self.identity = identity
                self.audit['captured_identity'] = identity
            self.audit['balance_saved'] = True
            self.audit['last_result'] = 'balance_captured'
        except Exception as error:
            self.audit['last_result'] = 'balance_rejected'
            known={'successful_balance_sample_required','official_session_headers_incomplete',
                   'invalid_official_sample_signature','official_client_build_not_supported',
                   'wrong_member','invalid_headers','balance_not_successful','balance_response_invalid'}
            self.audit['balance_error'] = str(error) if str(error) in known else type(error).__name__
        self.save_audit()
        self.save_session()

    def save_session(self):
        if self.session_candidate is None or self.profile is None or self.clock() > self.deadline:
            return
        candidate, self.session_candidate = self.session_candidate, None
        try:
            validate_session(candidate, self.profile,
                {'identity':self.identity, 'refresh_session':self.refresh_session},
                self.audit | {'session_saved':True})
            write_private(self.output/'session.json', candidate)
            self.audit['session_saved'] = True
            self.audit['session_result'] = 'saved_observed_not_redeemed'
        except EzMcdError:
            self.audit['session_result'] = 'balance_session_mismatch'
        except OSError:
            self.audit['session_result'] = 'session_storage_failed'
        self.save_audit()
