from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from ezmcd.errors import EzMcdError
from ezmcd.storage import Store, read_private,write_private
from import_evidence import apply_evidence
import test_transaction as fixtures


class ImportTests(unittest.TestCase):
    setUp=fixtures.TransactionTests.setUp
    tearDown=fixtures.TransactionTests.tearDown
    prepare=fixtures.TransactionTests.prepare
    config=fixtures.TransactionTests.config
    recover=fixtures.TransactionTests.recover
    request=fixtures.TransactionTests.request
    balance=fixtures.TransactionTests.balance
    captured=fixtures.TransactionTests.captured

    def ready(self):
        patch('import_evidence.constants',return_value=fixtures.KEYS).start()
        self.store=Store(self.root/'store')
        self.store.add_account(self.account)
        self.store.claim('original-paid-attempt','consume')
        self.directory=self.store.root/'readonly-captures'/('a'*32)
        self.path=self.directory/'transaction'
        self.captured();r=self.recover();self.config(r)
        r.observe_restored_balance(self.balance(),request_sequence=r.request_started())
        write_private(self.directory/'cleanup.json',{'official_app_closed':True,
            'capture_processes_stopped':True,'interphase_shutdown_verified':True,
            'system_proxy_unchanged':True,'system_proxy_modified':False,'errors':[],
            'temporary_leaf_keys_removed':True})
        self.wallet=patch('ezmcd.provider.McdProvider.wallet',autospec=True,return_value={'balance_cents':900}).start()
        self.probe=patch('ezmcd.transport.Transport.verify_runtime',autospec=True,return_value={
            'http_status':200,'response':{'success':True,'data':{'list':[]}}}).start()
        patch('import_evidence.time.time',side_effect=lambda:self.now).start()

    def test_new_get_evidence_is_verified_before_atomic_update_and_repeat_keeps_file_bytes(self):
        self.ready()
        original=self.store.account('mine')
        result=apply_evidence(self.store,'mine',self.directory)
        updated=self.store.account('mine')
        self.assertTrue(result['account_updated'])
        self.assertEqual(updated['runtime']['observed_at'],fixtures.NOW+1)
        self.assertEqual(updated['profile'],original['profile'])
        self.assertEqual(updated['policy'],original['policy'])
        self.assertEqual(read_private(self.directory/'runtime-before.json')['runtime'],original['runtime'])
        path=self.store.root/'accounts'/(original['identity']+'.json')
        before=path.read_bytes();mtime=path.stat().st_mtime_ns
        repeat=apply_evidence(self.store,'mine',self.directory)
        self.assertFalse(repeat['account_updated']);self.assertTrue(repeat['already_imported'])
        self.assertEqual(path.read_bytes(),before);self.assertEqual(path.stat().st_mtime_ns,mtime)
        self.assertEqual(self.wallet.call_count,2);self.assertEqual(self.probe.call_count,2)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0],1)
        self.assertFalse(result['order_post_acceptance_verified'])

    def test_failed_live_v5_check_does_not_replace_account(self):
        self.ready();before=self.store.account('mine')
        self.probe.return_value={'http_status':401,'response':{'success':False}}
        with self.assertRaises(EzMcdError):apply_evidence(self.store,'mine',self.directory)
        self.assertEqual(self.store.account('mine'),before)
        self.wallet.assert_called_once()

    def test_cleanup_gaps_cannot_update_or_contact_server(self):
        self.ready();path=self.directory/'cleanup.json';original=read_private(path)
        for key in ('official_app_closed','capture_processes_stopped','interphase_shutdown_verified',
                    'system_proxy_unchanged','temporary_leaf_keys_removed'):
            write_private(path,original|{key:False})
            with self.subTest(key=key),self.assertRaises(EzMcdError):apply_evidence(self.store,'mine',self.directory)
        self.wallet.assert_not_called();self.probe.assert_not_called()
        self.assertEqual(self.store.account('mine'),self.before)

    def test_changed_account_or_evidence_is_rejected_without_network(self):
        self.ready();changed=deepcopy(self.before);changed['profile']['fields']['sid']='changed'
        self.store.update_account(changed)
        with self.assertRaises(EzMcdError):apply_evidence(self.store,'mine',self.directory)
        self.wallet.assert_not_called();self.probe.assert_not_called()
        self.assertEqual(self.store.account('mine'),changed)

    def test_expired_observation_cannot_be_renewed_by_recovery(self):
        self.ready();self.now+=86401
        with self.assertRaises(EzMcdError):apply_evidence(self.store,'mine',self.directory)
        self.wallet.assert_not_called();self.probe.assert_not_called()
        self.assertEqual(self.store.account('mine'),self.before)

    def test_interrupted_same_epoch_or_modified_sample_is_not_eligible(self):
        self.ready();state_path=self.path/'state.json';state=read_private(state_path)
        write_private(state_path,state|{'recovery_epoch':state['collector_epoch']})
        with self.assertRaises(EzMcdError):apply_evidence(self.store,'mine',self.directory)
        write_private(state_path,state)
        runtime_path=self.path/'runtime.json';runtime=read_private(runtime_path)
        runtime['request']['headers']['sid']='other'
        write_private(runtime_path,runtime)
        with self.assertRaises(EzMcdError):apply_evidence(self.store,'mine',self.directory)
        self.wallet.assert_not_called();self.probe.assert_not_called()

    def test_matching_fresh_risk_field_is_live_verified_then_updated_and_original_archived(self):
        original_request=self.request
        def new_request(**kwargs):
            value=original_request(**kwargs)
            value['headers']['d']='fresh-official-sdk-risk'
            return value
        with patch.object(self,'request',side_effect=new_request):self.ready()
        original=self.store.account('mine')
        apply_evidence(self.store,'mine',self.directory)
        updated=self.store.account('mine')
        expected=deepcopy(original['profile']);expected['fields']['riskDeviceId']='fresh-official-sdk-risk'
        self.assertEqual(updated['profile'],expected)
        self.assertEqual(self.wallet.call_args.args[0].account['profile'],expected)
        self.assertEqual(self.probe.call_args.args[0].signer.account['profile'],expected)
        self.assertEqual(read_private(self.directory/'runtime-before.json')['risk_device_id'],'synthetic-risk')
        self.assertEqual(updated['policy'],original['policy'])

    def test_changed_profile_after_import_cannot_be_silently_marked_already_imported(self):
        self.ready();apply_evidence(self.store,'mine',self.directory)
        changed=self.store.account('mine');changed['profile']['fields']['riskDeviceId']='later-device-change'
        self.store.update_account(changed);self.wallet.reset_mock();self.probe.reset_mock()
        with self.assertRaises(EzMcdError):apply_evidence(self.store,'mine',self.directory)
        self.wallet.assert_not_called();self.probe.assert_not_called()
        self.assertEqual(self.store.account('mine'),changed)


if __name__=='__main__':unittest.main()
