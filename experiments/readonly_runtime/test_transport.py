from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography import x509
from ezmcd.sampling_cert import SamplingCertificate as ApiCertificate
from ezmcd.storage import Store
from config_certificate import SamplingCertificate as ConfigCertificate
from transport_fence import Fence, Lifecycle, snapshots
from ezmcd.sampling_local import LOCAL_MODE, TRANSPORT

DNS={'api.mcd.cn':{'addresses':['1.1.1.1'],'names':['api.mcd.cn']},
     'img.mcd.cn':{'addresses':['8.8.8.8'],'names':['img.mcd.cn','public-cdn.example']}}


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.fence=Fence({'dns':DNS,'deadline':200,'phase_directory':str(self.root)},lambda:100)

    def connect(self,address,sni=None,protocol='tcp'):
        data=SimpleNamespace(server=SimpleNamespace(address=address,sni=sni,transport_protocol=protocol,error=None))
        self.fence.server_connect(data);return data.server

    def test_only_two_fixed_origins_use_snapshot_and_preserve_host_tls(self):
        for host,ip in [('api.mcd.cn','1.1.1.1'),('img.mcd.cn','8.8.8.8')]:
            for dest in (host,'9.9.9.9'):
                r=self.connect((dest,443),host)
                self.assertIsNone(r.error)
                self.assertEqual(r.address,(ip,443));self.assertEqual(r.sni,host)

    def test_wallet_other_host_udp_and_conflicting_names_blocked(self):
        for address,sni,protocol in [
            (('spurcard.chinaums.com',443),'spurcard.chinaums.com','tcp'),
            (('api.mcd.cn',80),'api.mcd.cn','tcp'),
            (('api.mcd.cn',443),'api.mcd.cn','udp'),
            (('api.mcd.cn',443),'img.mcd.cn','tcp'),
            (('api.mcd.cn',443),'other.example','tcp'),
            (('other.example',443),'api.mcd.cn','tcp'),
            (('1.1.1.1',443),None,'tcp')]:
            with self.subTest(address=address,sni=sni,protocol=protocol):
                self.assertIsNotNone(self.connect(address,sni,protocol).error)
        self.assertEqual(self.fence.audit['upstream_connections'],0)

    def test_deadline_or_audit_failure_cannot_open_upstream(self):
        self.fence.clock=lambda:201
        self.assertIsNotNone(self.connect(('api.mcd.cn',443),'api.mcd.cn').error)
        self.fence.clock=lambda:100
        data=SimpleNamespace(server=SimpleNamespace(address=('api.mcd.cn',443),sni='api.mcd.cn',
            transport_protocol='tcp',error=None))
        with patch('transport_fence.write_private',side_effect=OSError('disk failed')):
            with self.assertRaises(OSError):self.fence.server_connect(data)
        self.assertIsNotNone(data.server.error)

    def test_snapshot_rejects_private_addresses_extra_hosts_and_ambiguous_alias(self):
        import copy
        for kind in ('private','host','alias'):
            source=copy.deepcopy(DNS)
            if kind=='private':source['api.mcd.cn']['addresses']=['127.0.0.1']
            elif kind=='host':source['other.example']=source['api.mcd.cn']
            else:source['api.mcd.cn']['names'].append('public-cdn.example')
            with self.assertRaises(ValueError):snapshots(source)

    def test_lifecycle_does_not_accept_generic_local_or_other_purpose(self):
        good={'purpose':'readonly_runtime_experiment','transport':TRANSPORT,
            'local_mode':LOCAL_MODE,'parent_pid':1234,'deadline':200}
        self.assertEqual(Lifecycle(good).config,good)
        for key,value in [('local_mode','local'),('purpose','account_maintenance'),('parent_pid',1),('deadline',float('nan'))]:
            with self.assertRaises(ValueError):Lifecycle(good|{key:value})


class CertificateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name))

    def test_config_root_is_independent_and_cannot_broaden_api_root(self):
        api,config=ApiCertificate(self.store),ConfigCertificate(self.store)
        a=api.prepare();before=(api.directory/'mitmproxy-ca.pem').read_bytes()
        c=config.prepare()
        self.assertEqual(a['host'],'api.mcd.cn');self.assertEqual(c['host'],'img.mcd.cn')
        self.assertNotEqual(a['certificate_sha256'],c['certificate_sha256'])
        self.assertEqual((api.directory/'mitmproxy-ca.pem').read_bytes(),before)
        _,cert,_=config._load()
        self.assertEqual(cert.extensions.get_extension_for_class(x509.NameConstraints).value.permitted_subtrees,
            [x509.DNSName('img.mcd.cn')])
        with config.server_certificate() as leaf:
            raw=leaf.read_bytes();obj=x509.load_pem_x509_certificate(raw)
            self.assertEqual(list(obj.extensions.get_extension_for_class(x509.SubjectAlternativeName).value),
                [x509.DNSName('img.mcd.cn')])
        self.assertFalse(leaf.exists())

    def test_install_explicitly_requests_img_only_and_never_installs_api(self):
        c=ConfigCertificate(self.store);c.prepare()
        no={'img.mcd.cn':False,'outside.example':False,'child.img.mcd.cn':False}
        yes=no|{'img.mcd.cn':True}
        with patch.object(c,'_probe',side_effect=[no,yes]),patch.object(c,'_command',return_value=SimpleNamespace(returncode=0)) as command:
            c.install()
        args=command.call_args.args[0]
        self.assertEqual(args[:7],['add-trusted-cert','-r','trustRoot','-p','ssl','-s','img.mcd.cn'])
        self.assertNotIn('api.mcd.cn',str(args))


if __name__=='__main__':unittest.main()
