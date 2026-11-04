import json
import subprocess
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from ezmcd.errors import EzMcdError
from ezmcd.sampling_dns import api_dns_snapshot, parse_snapshot

SNAPSHOT={'host':'api.mcd.cn','addresses':['192.0.2.1','2001:db8::1'],
          'names':['api.mcd.cn','canonical.example']}


class DnsSnapshotTests(unittest.TestCase):
    def test_bounded_fixed_lookup_before_capture(self):
        with patch('ezmcd.sampling_dns.subprocess.run',return_value=NS(returncode=0,stdout=json.dumps(SNAPSHOT))) as run:
            self.assertEqual(api_dns_snapshot(),SNAPSHOT)
        self.assertEqual(run.call_args.kwargs['timeout'],5)
        self.assertEqual(run.call_args.args[0][1:3],['-I','-c'])
        self.assertIn("getaddrinfo('api.mcd.cn',443",run.call_args.args[0][-1])

    def test_missing_or_invalid_resolution_is_redacted(self):
        for failure in (NS(returncode=1,stdout='private'),NS(returncode=0,stdout='invalid'),
                        NS(returncode=0,stdout=json.dumps(SNAPSHOT|{'addresses':[]}))):
            with patch('ezmcd.sampling_dns.subprocess.run',return_value=failure):
                with self.assertRaisesRegex(EzMcdError,'^sampling_api_dns_unavailable$'):api_dns_snapshot()
        with patch('ezmcd.sampling_dns.subprocess.run',side_effect=subprocess.TimeoutExpired('private',5)):
            with self.assertRaisesRegex(EzMcdError,'^sampling_api_dns_unavailable$'):api_dns_snapshot()

    def test_snapshot_cannot_supply_urls_or_change_fixed_api(self):
        for value in (SNAPSHOT|{'host':'other.example'},SNAPSHOT|{'addresses':['https://other.example']},
                      SNAPSHOT|{'addresses':[True]},SNAPSHOT|{'addresses':[1234]},
                      SNAPSHOT|{'names':['other.example']},SNAPSHOT|{'names':['api.mcd.cn','bad/name']},
                      SNAPSHOT|{'names':['api.mcd.cn','bad..name']}):
            with self.assertRaises((ValueError,TypeError)):parse_snapshot(value)
