"""Ambiguous private exports must fail before account verification or mutation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ezmcd.accounts import connect_har, profile_from_balance_sample, runtime_from_sample, update_runtime
from ezmcd.capture import runtime_sample_from_har, samples_from_har
from ezmcd.errors import EzMcdError
from ezmcd.models import Policy, fingerprint
from ezmcd.storage import Store, read_private, write_private
from test_project_accounts import KEYS, sample
from test_project_runtime_update import CONSTANTS, NOW, evidence


def har_entry(s):
    request=s['request']
    entry={'request': {'url': 'https://api.mcd.cn'+request['path'], 'method': request['method'],
        'headers': [{'name': k, 'value': v} for k,v in request['headers'].items()],
        'postData': {'text': request.get('body','')}}}
    if 'response' in s:
        entry['response']={'status': s['response']['status'], 'content': {'text': s['response']['body']}}
    return entry


class SampleHeaderTests(unittest.TestCase):
    def test_raw_json_duplicate_keys_cannot_be_lost_before_header_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'sample.json'
            path.write_text('{"request":{"headers":{"sid":"first-private","sid":"last-private"}}}')
            path.chmod(0o600)
            with self.assertRaisesRegex(EzMcdError,'private_json_duplicate_keys') as error:
                read_private(path)
            self.assertEqual(str(error.exception),'private_json_duplicate_keys')

    def test_raw_har_duplicate_value_keys_stop_before_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder));path=store.root/'sample.har'
            path.write_text('{"log":{"entries":[{"request":{"method":"GET",'
                '"url":"https://api.mcd.cn/bff/member/mwallet/balance","headers":['
                '{"name":"sid","value":"first-private","value":"last-private"}]}}]}}')
            path.chmod(0o600)
            with patch('ezmcd.provider.McdProvider.wallet') as wallet:
                with self.assertRaisesRegex(EzMcdError,'private_json_duplicate_keys'):
                    connect_har(store,'mine',path,'unused')
                wallet.assert_not_called()
            self.assertEqual(store.accounts(),[])

    def test_duplicate_runtime_headers_are_rejected_by_both_har_entry_points(self):
        for name in ('SID','Token','X-Mcd-Sign'):
            e=har_entry(evidence())
            e['request']['headers'].append({'name': name.lower(), 'value': evidence()['request']['headers'][name]})
            for parser in (runtime_sample_from_har, samples_from_har):
                with self.subTest(name=name, parser=parser.__name__):
                    with self.assertRaisesRegex(EzMcdError,'headers_invalid_or_repeated'):
                        parser({'log': {'entries': [har_entry(sample()),e]}})

    def test_invalid_names_values_and_duplicate_noncredential_headers_are_rejected(self):
        for header in ({'name': 'bad name','value': 'x'}, {'name': 'SID','value': None},
                       {'name': 'extra','value': 'secret\r\nToken: override'},
                       {'name': 'extra','value': 'x'*8193}, {'name': 12,'value': 'x'},
                       {'name': 'extra'}, 'bad-header'):
            e=har_entry(evidence()); e['request']['headers'].append(header)
            with self.subTest(header_type=type(header).__name__):
                with self.assertRaisesRegex(EzMcdError,'headers_invalid_or_repeated'):
                    runtime_sample_from_har({'log': {'entries': [e]}})
        e=har_entry(evidence()); e['request']['headers'] += [
            {'name':'Cookie','value':'private'}, {'name':'cookie','value':'other'}]
        with self.assertRaisesRegex(EzMcdError,'headers_invalid_or_repeated'):
            runtime_sample_from_har({'log': {'entries': [e]}})

    def test_conflicting_host_is_not_silently_replaced_with_official_origin(self):
        for s,parser in ((sample(),samples_from_har),(evidence(),runtime_sample_from_har)):
            e=har_entry(s);e['request']['headers'].append({'name':'Host','value':'different.example'})
            with self.subTest(parser=parser.__name__):
                with self.assertRaisesRegex(EzMcdError,'official_sample_host_mismatch'):
                    parser({'log': {'entries': [e]}})

    def test_irrelevant_requests_do_not_require_header_parsing(self):
        irrelevant={'request': {'url':'https://api.mcd.cn/unrelated','method':'GET','headers':None}}
        balance,runtime=samples_from_har({'log': {'entries':[irrelevant,har_entry(sample()),har_entry(evidence())]}})
        self.assertEqual(balance['request']['headers']['sid'],'synthetic-session')
        self.assertEqual(runtime['request']['body'],'')

    def test_direct_runtime_sample_cannot_hide_duplicate_case_variants(self):
        s=evidence();s['request']['headers']['sid']='synthetic-session'
        profile={'fields':{'sid':'synthetic-session','deviceToken':'synthetic-device'}}
        with patch('ezmcd.accounts.constants',return_value=CONSTANTS), patch('ezmcd.accounts.time.time',return_value=NOW):
            with self.assertRaisesRegex(EzMcdError,'headers_invalid_or_repeated'):
                runtime_from_sample(profile,s,'unused')

    def test_direct_balance_sample_rejects_conflicting_host_before_signature_work(self):
        s=sample();s['request']['headers']['Host']='different.example'
        with patch('ezmcd.accounts.constants') as keys:
            with self.assertRaisesRegex(EzMcdError,'official_sample_host_mismatch'):
                profile_from_balance_sample(s,'unused')
            keys.assert_not_called()

    def test_invalid_har_container_and_header_count_are_bounded(self):
        for headers in (None,{},'private', [{'name':f'x-{i}','value':'x'} for i in range(101)]):
            e=har_entry(evidence());e['request']['headers']=headers
            with self.subTest(kind=type(headers).__name__):
                with self.assertRaisesRegex(EzMcdError,'headers_invalid_or_repeated'):
                    runtime_sample_from_har({'log': {'entries':[e]}})

    def test_valid_export_still_connects_and_updates_without_replaying_body(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder));path=store.root/'sample.har'
            balance=har_entry(sample());runtime=har_entry(evidence())
            for entry in (balance,runtime):
                entry['request']['headers'].append({'name':'Host','value':'api.mcd.cn:443'})
            write_private(path,{'log':{'entries':[balance,runtime]}})
            with patch('ezmcd.accounts.constants',return_value=(*KEYS[:2],CONSTANTS[2],())), \
                    patch('ezmcd.accounts.time.time',return_value=NOW), \
                    patch('ezmcd.provider.McdProvider.wallet',return_value={'balance_cents':100}) as wallet:
                connected=connect_har(store,'mine',path,'unused')
                self.assertTrue(connected['runtime_available'])
                before=store.account('mine')
                updated=update_runtime(store,'mine',path,har=True)
                self.assertTrue(updated['runtime_updated'])
                self.assertEqual(wallet.call_count,2)
            after=store.account('mine')
            self.assertEqual(after,before)
            self.assertFalse(after['policy']['allow_checkout'])
            self.assertEqual(after['runtime']['observed_at'],NOW-10)
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0],0)

    def test_ambiguous_balance_export_cannot_create_an_account_or_query_wallet(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder));path=store.root/'sample.har'
            e=har_entry(sample());e['request']['headers'].append({'name':'SID','value':'synthetic-session'})
            write_private(path,{'log':{'entries':[e]}})
            with patch('ezmcd.accounts.constants',return_value=KEYS), patch('ezmcd.provider.McdProvider.wallet') as wallet:
                with self.assertRaisesRegex(EzMcdError,'headers_invalid_or_repeated'):
                    connect_har(store,'mine',path,'unused')
                wallet.assert_not_called()
            self.assertEqual(store.accounts(),[])

    def test_ambiguous_runtime_export_preserves_existing_account_and_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder));path=store.root/'sample.har'
            record={'alias':'mine','identity':fingerprint({'member':'synthetic-member'}),'provider':'mcd-cn',
                'binary':'unused','profile':{'fields':{'sid':'synthetic-session','deviceToken':'synthetic-device'}},
                'runtime':None,'policy':Policy().model_dump()}
            store.add_account(record);store.claim('existing-write','order')
            e=har_entry(evidence());e['request']['headers'].insert(0,{'name':'sid','value':'different-session'})
            write_private(path,{'log':{'entries':[e]}})
            with patch('ezmcd.accounts.constants',return_value=CONSTANTS), patch('ezmcd.accounts.time.time',return_value=NOW), \
                    patch('ezmcd.provider.McdProvider.wallet') as wallet:
                with self.assertRaisesRegex(EzMcdError,'headers_invalid_or_repeated'):
                    update_runtime(store,'mine',path,har=True)
                wallet.assert_not_called()
            self.assertEqual(store.account('mine'),record)
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT stage FROM writes').fetchall()[0][0],'order')


if __name__=='__main__':unittest.main()
