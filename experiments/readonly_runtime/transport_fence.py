"""Narrow process transport for the private read-only runtime experiment."""
import ipaddress
import math
import threading

from ezmcd.sampling_local import LocalCaptureLifecycle, LOCAL_MODE, TRANSPORT
from ezmcd.storage import write_private
from transaction import CONFIG_HOST

HOSTS = {'api.mcd.cn', CONFIG_HOST}


def snapshots(value):
    if not isinstance(value, dict) or set(value)!=HOSTS:
        raise ValueError('fixed_dns_hosts_required')
    result={}
    names={}
    for host,item in value.items():
        if (not isinstance(item,dict) or set(item)!={'addresses','names'}
                or not isinstance(item['addresses'],list) or not 1<=len(item['addresses'])<=64
                or not isinstance(item['names'],list) or not 1<=len(item['names'])<=64
                or host not in item['names']):
            raise ValueError('invalid_fixed_dns_snapshot')
        ips=tuple(dict.fromkeys(ipaddress.ip_address(v) for v in item['addresses']))
        if any(not a.is_global for a in ips):
            raise ValueError('public_origin_address_required')
        for name in item['names']:
            if (not isinstance(name,str) or not name or len(name)>253 or name.lower()!=name
                    or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789.-' for c in name)
                    or '..' in name or (name in names and names[name]!=host)):
                raise ValueError('ambiguous_fixed_dns_name')
            names[name]=host
        result[host]=ips
    return result,names


class Fence:
    def __init__(self, config, clock):
        self.addresses,self.names=snapshots(config['dns'])
        self.deadline=config['deadline'];self.clock=clock
        self.output=config['phase_directory']
        self.audit={'upstream_connections':0,'destinations_blocked':0,'dns_answered':0,'native_connections':0}

    def tls_clienthello(self,data):
        name=data.client_hello.sni
        label=name if name in HOSTS else 'absent' if name is None else 'other'
        address=data.context.server.address
        match=[]
        if isinstance(address,tuple) and len(address)==2:
            try:
                ip=ipaddress.ip_address(address[0])
                match=sorted(host for host,ips in self.addresses.items() if ip in ips)
            except ValueError:pass
        key=label+':'+(','.join(match) if match else 'destination_not_in_fixed_snapshot')
        counts=self.audit.setdefault('tls_hello_binding',{})
        counts[key]=counts.get(key,0)+1
        self.save()

    def tls_failed_client(self,data):
        # Finite labels only: no request headers, payloads, or arbitrary error text.
        host=data.conn.sni if data.conn.sni in HOSTS else 'other'
        error=(data.conn.error or '').lower()
        kind=next((label for text,label in (
            ('unknown ca','unknown_ca'),('certificate','certificate_error'),
            ('closed','closed'),('reset','reset'),('eof','eof')) if text in error),'other')
        counts=self.audit.setdefault('tls_client_failures',{})
        key=host+':'+kind;counts[key]=counts.get(key,0)+1
        self.save()

    def tls_established_client(self,data):
        host=data.conn.sni if data.conn.sni in HOSTS else 'other'
        counts=self.audit.setdefault('tls_client_established',{})
        counts[host]=counts.get(host,0)+1
        self.save()

    def save(self):
        from pathlib import Path
        write_private(Path(self.output)/'network.json',self.audit)

    def client_connected(self,client):
        if getattr(getattr(client,'proxy_mode',None),'type_name',None)=='local':
            self.audit['native_connections']+=1;self.save()

    def server_connect(self,data):
        previous=data.server.error
        data.server.error='EZMCD_READONLY_DESTINATION_BLOCKED'
        address=data.server.address
        host=(data.server.sni if data.server.sni in HOSTS else
              address[0] if isinstance(address,tuple) and address[0] in HOSTS else None)
        if (self.clock()>self.deadline or data.server.transport_protocol!='tcp'
                or not isinstance(address,tuple) or len(address)!=2 or address[1]!=443
                or host is None or data.server.sni not in (None,*HOSTS)):
            self.audit['destinations_blocked']+=1;self.save();return
        # A conflicting authority must not be rewritten to an allowed origin.
        if address[0] not in HOSTS:
            try:ipaddress.ip_address(address[0])
            except ValueError:self.audit['destinations_blocked']+=1;self.save();return
        elif address[0]!=host:
            self.audit['destinations_blocked']+=1;self.save();return
        ips=self.addresses[host]
        chosen=next((x for x in ips if x.version==4),ips[0])
        data.server.address=(str(chosen),443)
        data.server.sni=host
        self.audit['upstream_connections']+=1
        self.save()
        data.server.error=previous

    def http_connect(self,flow):
        from ezmcd.sampling_guard import RuntimeGuard
        if flow.request.host not in HOSTS or flow.request.port!=443:
            RuntimeGuard.block(flow,terminate=True)

    async def dns_request(self,flow):
        from mitmproxy import dns
        request=flow.request
        flow.response=request.fail(dns.response_codes.REFUSED)
        if (self.clock()>self.deadline or not request.query or request.op_code!=dns.op_codes.QUERY
                or len(request.questions)!=1):return
        q=request.questions[0]
        if q.class_!=dns.classes.IN or q.type not in (dns.types.A,dns.types.AAAA,dns.types.HTTPS):return
        host=self.names.get(q.name.lower().removesuffix('.'))
        if host is None:return
        version=4 if q.type==dns.types.A else 6
        answers=[dns.ResourceRecord(name=q.name,type=q.type,class_=q.class_,ttl=30,data=ip.packed)
            for ip in self.addresses[host] if q.type!=dns.types.HTTPS and ip.version==version]
        self.audit['dns_answered']+=1;self.save()
        flow.response=request.succeed(answers)


class Lifecycle(LocalCaptureLifecycle):
    def __init__(self,config):
        # Reuse the existing bounded watchdog, without pretending this is a
        # wallet-only maintenance purpose in the child's configuration.
        if (config.get('purpose')!='readonly_runtime_experiment' or config.get('transport')!=TRANSPORT
                or config.get('local_mode')!=LOCAL_MODE
                or type(config.get('parent_pid')) is not int or config['parent_pid']<=1
                or type(config.get('deadline')) not in (int,float) or not math.isfinite(config['deadline'])):
            raise ValueError('invalid_readonly_lifecycle_scope')
        self.config=config
        self.task=None
        self.stopped=threading.Event()
