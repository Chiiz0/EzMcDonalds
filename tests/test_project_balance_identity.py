from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from ezmcd.accounts import profile_from_balance_sample, profile_from_balance_request
from ezmcd.errors import EzMcdError
from ezmcd.provider import McdProvider
from ezmcd.protocol.signing import sign_v4
from test_project_accounts import sample, KEYS


def without_member():
    value=sample();headers=value['request']['headers'];headers.pop('meddyid')
    headers.update(sign_v4(headers,*KEYS[:2],datetime.now(timezone.utc),'GET',value['request']['path']))
    value['response']['body']={'success':True,'data':{'userId':'synthetic-member','balance':'1.00'}}
    return value


class BalanceIdentityTests(unittest.TestCase):
    def test_response_identity_keeps_original_signed_headers_unchanged(self):
        value=without_member();original=json.dumps(value)
        with patch('ezmcd.accounts.constants',return_value=KEYS):
            profile=profile_from_balance_sample(value,'unused')
        self.assertEqual(profile['fields']['meddyId'],'synthetic-member')
        self.assertEqual(profile['member_source'],'balance_response')
        self.assertEqual(json.dumps(value),original)

    def test_request_only_or_invalid_signature_cannot_use_response_fallback(self):
        value=without_member()
        with patch('ezmcd.accounts.constants',return_value=KEYS):
            with self.assertRaisesRegex(EzMcdError,'headers_incomplete'):
                profile_from_balance_request(value['request'],'unused')
            value['request']['headers']['sid']='modified'
            with self.assertRaisesRegex(EzMcdError,'invalid_official_sample_signature'):
                profile_from_balance_sample(value,'unused')

    def test_conflicting_or_malformed_member_is_rejected(self):
        with patch('ezmcd.accounts.constants',return_value=KEYS):
            for member in ('',123,{},'bad\nmember'):
                value=without_member();value['response']['body']['data']['userId']=member
                with self.assertRaises(EzMcdError):profile_from_balance_sample(value,'unused')
            value=sample();value['response']['body']={'success':True,'data':{'userId':'different-member'}}
            with self.assertRaisesRegex(EzMcdError,'identity_mismatch'):profile_from_balance_sample(value,'unused')

    def test_live_wallet_requires_matching_identity_for_response_bound_profile(self):
        account={'profile':{'member_source':'balance_response','fields':{'meddyId':'synthetic-member'}}}
        provider=McdProvider(account)
        with patch.object(provider,'get',return_value={'balance':'1.00','userId':'synthetic-member'}):
            self.assertEqual(provider.wallet(),{'balance_cents':100})
        for response in ({'balance':'1.00'},{'balance':'1.00','userId':'other-member'}):
            with patch.object(provider,'get',return_value=response):
                with self.assertRaisesRegex(EzMcdError,'wallet_member_mismatch'):provider.wallet()
