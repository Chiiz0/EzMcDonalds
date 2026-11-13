"""Experimental HTTP handlers, not a runnable network interception addon.

An external driver must enforce App/process routing, exact upstream TLS/DNS,
bounded lifetime, certificate scope, ownership locks and inter-phase shutdown.
These handlers alone are NOT sufficient to start a real capture.
"""
import ipaddress
import json
import re
from urllib.parse import urlsplit

from ezmcd.capture import sample_headers
from ezmcd.errors import EzMcdError
from ezmcd.sampling_guard import READS, RuntimeGuard
from ezmcd.storage import write_private
from transaction import CONFIG_HOST, CONFIG_PATH, BALANCE, MAX_BYTES, unique


class RequestHandler:
    def __init__(self, transaction):
        self.transaction = transaction
        self.audit = {'api_reads_forwarded':0,'config_reads_forwarded':0,
            'requests_blocked':0,'runtime_captured':False,'restoration_verified':False,
            'account_updated':False,'financial_requests_forwarded':0,
            'known_api_read_counts':{},'wallet_requests':{'v4':0,'v5':0,'other':0},
            'wallet_requests_with_query':0,'wallet_signatures_seen':0}

    def save(self):
        write_private(self.transaction.directory/('http-audit-'+self.transaction.epoch+'.json'),self.audit)

    @staticmethod
    def origin(flow, host):
        req, client = flow.request, flow.client_conn
        if (req.scheme != 'https' or req.port != 443
                or getattr(getattr(client,'proxy_mode',None),'type_name',None) != 'local'
                or client.sni != host or req.host_header not in (host,host+':443')):
            return False
        if req.host != host:
            try:ipaddress.ip_address(req.host)
            except ValueError:return False
        return req.headers.get_all('host') in ([host],[host+':443'])

    @staticmethod
    def headers(req, host):
        # Validate the full list first; do not hide duplicate unknown headers by
        # projecting only those used for identity. Host is validated separately.
        if req.headers.get_all('host') not in ([host],[host+':443']):
            raise ValueError('invalid_authority')
        values = [{'name':k,'value':v} for k,v in req.headers.items(multi=True) if k.lower()!='host']
        return sample_headers(values,har=True)

    def requestheaders(self, flow):
        req = flow.request
        RuntimeGuard.block(flow,terminate=req.method!='GET')
        self.audit['requests_blocked'] += 1
        flow.metadata['readonly_request_sequence'] = self.transaction.request_started()
        try:
            if req.method!='GET':
                return
            if self.origin(flow,CONFIG_HOST) and req.path==CONFIG_PATH:
                headers=self.headers(req,CONFIG_HOST)
                if (any(k in headers for k in ('authorization','cookie','sid','token','d','x-mcd-sign'))
                        or headers.get('content-length','0')!='0' or 'transfer-encoding' in headers):
                    return
                # Ask for actual public bytes, never use a 304 as restore proof.
                req.headers.pop('if-none-match',None)
                req.headers.pop('if-modified-since',None)
                req.host=CONFIG_HOST
                flow.metadata['readonly_config_request']=True
                self.audit['config_reads_forwarded']+=1
            elif self.origin(flow,'api.mcd.cn'):
                headers=self.headers(req,'api.mcd.cn')
                if headers.get('content-length','0')!='0' or 'transfer-encoding' in headers:
                    return
                # No pattern paths or additional POST read exceptions here.
                if urlsplit(req.path).path not in READS:
                    return
                path=urlsplit(req.path).path
                counts=self.audit['known_api_read_counts']
                counts[path]=counts.get(path,0)+1
                req.host='api.mcd.cn'
                if path==BALANCE:
                    version=headers.get('sv')
                    self.audit['wallet_requests'][version if version in ('v4','v5') else 'other']+=1
                    self.audit['wallet_requests_with_query']+=int(req.path!=BALANCE)
                    self.audit['wallet_signatures_seen']+=int(bool(headers.get('x-mcd-sign')))
                if path==BALANCE and (headers.get('sv')=='v5' or headers.get('x-mcd-sign')):
                    # A signature-bearing request with an unexpected version or
                    # query must stay blocked, never fall through as an ordinary read.
                    if req.path!=BALANCE:
                        raise EzMcdError('readonly_capture_wallet_query_not_supported')
                    request={'method':'GET','host':'api.mcd.cn','path':BALANCE,'body':'','headers':headers}
                    self.transaction.capture_runtime(request)
                    self.audit['runtime_captured']=True
                    # This signature-bearing GET is not forwarded. A recovered
                    # v4 balance response and later fixed v5 read are separate.
                    return
                self.audit['api_reads_forwarded']+=1
            else:
                return
            self.audit['requests_blocked']-=1
            # Durable accounting before removing the local blocking response.
            self.save()
            flow.response=None
            flow.metadata.pop('ezmcd_blocked',None)
        except (EzMcdError,ValueError,TypeError,AttributeError,KeyError,OSError) as error:
            RuntimeGuard.block(flow,terminate=True)
            self.audit['last_error']=(error.code if isinstance(error,EzMcdError)
                and error.code.startswith('readonly_capture_') else 'request_rejected')
        finally:
            if flow.metadata.get('ezmcd_blocked') and req.host=='api.mcd.cn':
                route=re.sub(r'[A-Za-z0-9_-]{20,}|[0-9]{6,}','{id}',urlsplit(req.path).path)
                if (len(route)<160 and re.fullmatch(r'[A-Z]{1,10}',req.method)
                        and re.fullmatch(r'/[A-Za-z0-9_/{}/.-]*',route)):
                    counts=self.audit.setdefault('blocked_api_routes',{})
                    key=req.method+' '+route
                    if key in counts or len(counts)<60:counts[key]=counts.get(key,0)+1
            self.save()

    def responseheaders(self, flow):
        if flow.metadata.get('readonly_config_request') or flow.request.path==BALANCE:
            flow.response.stream=False

    def response(self, flow):
        if flow.metadata.get('ezmcd_blocked'):
            return
        if flow.metadata.get('readonly_config_request'):
            original=flow.response
            collecting=self.transaction.can_override
            # Failure while building a candidate must not leak partial output.
            # Recovery always retains the actual unmodified official response.
            if collecting:RuntimeGuard.block(flow)
            try:
                result=self.transaction.config_response(method=flow.request.method,host=CONFIG_HOST,
                    path=flow.request.path,status=original.status_code,raw=original.content)
                if result!=original.content:
                    original.content=result
                    original.headers.pop('etag',None)
                    original.headers.pop('last-modified',None)
                    original.headers['cache-control']='no-store'
                    self.audit['candidate_response_released']=True
                self.save()
                flow.response=original
                flow.metadata.pop('ezmcd_blocked',None)
            except (EzMcdError,ValueError,TypeError,KeyError,OSError):
                self.transaction.require_recovery()
                self.audit['last_error']='config_recovery_required'
            finally:
                self.save()
            return
        if (flow.request.path!=BALANCE or self.transaction.can_override
                or self.transaction.state['state']!='RESTORE_SENT'):
            return
        try:
            if flow.response.status_code!=200 or len(flow.response.content or b'')>MAX_BYTES:
                raise ValueError('invalid_balance')
            body=json.loads(flow.response.content,object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
            request={'method':'GET','host':'api.mcd.cn','path':BALANCE,'body':'',
                'headers':self.headers(flow.request,'api.mcd.cn')}
            self.transaction.observe_restored_balance({'request':request,
                'response':{'status':200,'body':body}},
                request_sequence=flow.metadata['readonly_request_sequence'])
            self.audit['restoration_verified']=True
        except (EzMcdError,ValueError,TypeError,AttributeError,KeyError,OSError) as error:
            self.audit['last_error']=(error.code if isinstance(error,EzMcdError)
                and error.code.startswith('readonly_capture_') else 'restoration_unverified')
        self.save()
