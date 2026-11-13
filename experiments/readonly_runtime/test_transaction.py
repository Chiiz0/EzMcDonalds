from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ezmcd.errors import EzMcdError
from ezmcd.models import fingerprint, Policy
from ezmcd.protocol.signing import BUILD, sign_v4
from ezmcd.protocol.v5 import ObservedRuntime, encode
from ezmcd.storage import read_private, write_private
from transaction import Transaction, CONFIG_HOST, CONFIG_PATH, BALANCE, candidate_bytes

NOW = 1800000000
KEYS = ('synthetic-access', 'synthetic-secret', tuple(range(64)), ())
ORIGINAL = json.dumps({'success': True, 'code': 200, 'data': {
    'unrelated': {'kept': ['original', True, 3]}, 'tdSignConfig': {'enabled': True, 'rules': [
        {'method':'POST','path':'/bff/order/orders','ratio':100},
        {'method':'POST','path':'/bff/passport/login/refresh','ratio':0}]}}}, indent=2).encode()


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root/'capture'
        self.now = NOW
        self.account = {'alias':'mine','identity':fingerprint({'member':'synthetic-member'}),
            'provider':'mcd-cn','binary':'unused','runtime':{'observed_at':NOW-900000},
            'policy':Policy(allow_checkout=False).model_dump(),
            'profile':{'build_sha256':BUILD,'client_headers':{'ct':'101','v':'test','language':'zh'},
                'fields':{'sid':'synthetic-session','deviceToken':'synthetic-device',
                    'riskDeviceId':'synthetic-risk','meddyId':'synthetic-member'}}}
        self.before = deepcopy(self.account)
        self.addCleanup(patch.stopall)
        patch('transaction.constants', return_value=KEYS).start()
        patch('ezmcd.accounts.constants', return_value=KEYS).start()
        self.network = patch('ezmcd.transport.Transport._open', side_effect=AssertionError('No networking')).start()

    def tearDown(self):
        self.network.assert_not_called()
        self.assertEqual(self.account, self.before)

    def prepare(self):
        return Transaction.prepare(self.path,self.account,ORIGINAL,clock=lambda:self.now)

    def config(self, t, raw=ORIGINAL, **overrides):
        kwargs={'method':'GET','host':CONFIG_HOST,'path':CONFIG_PATH,'status':200,'raw':raw}
        kwargs.update(overrides)
        return t.config_response(**kwargs)

    def request(self, *, v5=True, at=None):
        at = self.now if at is None else at
        headers={'sid':'synthetic-session','token':'synthetic-device','d':'synthetic-risk',
            'meddyid':'synthetic-member','ct':'101','v':'test','language':'zh','sv':'v5' if v5 else 'v4'}
        if v5:
            headers['x-mcd-sign']=encode(BALANCE,ObservedRuntime('A'*32,'b'*16),
                milliseconds=round(at*1000),random_value=11,counter=1,constants=KEYS[2])
        else:
            headers.update(sign_v4(headers,*KEYS[:2],datetime.fromtimestamp(at,timezone.utc),'GET',BALANCE))
        return {'method':'GET','host':'api.mcd.cn','path':BALANCE,'body':'','headers':headers}

    def balance(self, **kwargs):
        return {'request':self.request(v5=False,**kwargs),'response':{'status':200,
            'body':{'success':True,'data':{'userId':'synthetic-member','balance':'9.99','unused':'discard'}}}}

    def captured(self):
        t=self.prepare(); self.config(t); self.now+=1
        t.capture_runtime(self.request())
        return t

    def recover(self):
        self.now+=1
        return Transaction(self.path,self.account,clock=lambda:self.now)

    def test_candidate_changes_only_one_read_rule_and_keeps_raw_original(self):
        t=self.prepare(); candidate=self.config(t)
        expected=json.loads(ORIGINAL)
        expected['data']['tdSignConfig']['rules'].append({'method':'GET','path':BALANCE,'ratio':100})
        self.assertEqual(json.loads(candidate),expected)
        self.assertEqual(t.original,ORIGINAL)
        s=read_private(self.path/'state.json')
        self.assertEqual(s['state'],'MAY_HAVE_APPLIED')
        self.assertTrue(s['candidate_may_have_applied'])
        self.assertFalse(s['restore_observed'])

    def test_resume_before_or_after_dispatch_can_never_reapply_candidate(self):
        for dispatch in (False,True):
            with self.subTest(dispatch=dispatch):
                self.path=self.root/str(dispatch)
                t=self.prepare()
                if dispatch:self.config(t)
                r=self.recover()
                self.assertEqual(self.config(r),ORIGINAL)
                self.assertFalse(r.can_override)
                self.assertFalse(r.summary()['restoration_verified'])

    def test_resume_after_runtime_save_requires_new_successful_balance(self):
        t=self.captured(); observed=read_private(self.path/'runtime.json')['observed_at']
        self.assertFalse(t.summary()['import_ready'])
        r=self.recover(); self.config(r)
        result=r.observe_restored_balance(self.balance(),request_sequence=r.request_started())
        self.assertTrue(result['candidate_verified_after_restoration'])
        self.assertFalse(result['import_ready']) # Runner cleanup and live read not verified here.
        self.assertFalse(result['account_updated'])
        self.assertEqual(read_private(self.path/'runtime.json')['observed_at'],observed)
        stored=read_private(self.path/'restored-balance.json')
        self.assertNotIn('balance',stored['response']['body']['data'])
        self.assertNotIn('unused',str(stored['response']))

    def test_same_process_cannot_claim_recovery_with_inflight_candidate(self):
        t=self.captured()
        with self.assertRaisesRegex(EzMcdError,'new_recovery_epoch_required'):self.config(t)
        self.assertEqual(t.state['state'],'RECOVERY_REQUIRED')

    def test_response_only_does_not_prove_config_restoration(self):
        self.captured(); r=self.recover(); self.config(r)
        self.assertEqual(r.state['state'],'RESTORE_SENT')
        self.assertFalse(r.summary()['restoration_verified'])
        r2=self.recover()
        with self.assertRaisesRegex(EzMcdError,'restore_response_not_observed'):
            r2.observe_restored_balance(self.balance(),request_sequence=r2.request_started())

    def test_old_balance_inflight_response_cannot_finish_recovery(self):
        self.captured(); r=self.recover(); old=r.request_started(); self.config(r)
        with self.assertRaisesRegex(EzMcdError,'restore_response_not_observed'):
            r.observe_restored_balance(self.balance(),request_sequence=old)
        self.assertFalse(r.summary()['restoration_verified'])

    def test_stale_valid_hmac_cannot_finish_recovery(self):
        self.captured(); r=self.recover(); self.config(r)
        with self.assertRaisesRegex(EzMcdError,'stale_restoration_request'):
            r.observe_restored_balance(self.balance(at=NOW),request_sequence=r.request_started())

    def test_bad_signature_and_wrong_response_member_leave_recovery_pending(self):
        self.captured(); r=self.recover(); self.config(r)
        for alter in ('hmac','member','status'):
            s=self.balance()
            if alter=='hmac':s['request']['headers']['x-hmac-digest']='broken'
            elif alter=='member':s['response']['body']['data']['userId']='another-member'
            else:s['response']['status']=401
            with self.subTest(alter=alter),self.assertRaises(EzMcdError):
                r.observe_restored_balance(s,request_sequence=r.request_started())
        self.assertEqual(r.state['state'],'RESTORE_SENT')

    def test_body_method_query_and_similar_host_are_rejected(self):
        t=self.prepare(); self.config(t)
        for k,v in [('body','x'),('method','POST'),('path',BALANCE+'?x=1'),
                    ('path',BALANCE+'/'),('host','api.mcd.cn.attacker.example')]:
            req=self.request();req[k]=v
            with self.subTest(k=k,v=v),self.assertRaises(EzMcdError):t.capture_runtime(req)
        self.assertFalse((self.path/'runtime.json').exists())

    def test_full_identity_device_and_header_ambiguity_checks(self):
        t=self.prepare();self.config(t)
        for k in ('sid','token','meddyid','ct','v','language','sv'):
            req=self.request();req['headers'][k]='other'
            with self.subTest(k=k),self.assertRaises(EzMcdError):t.capture_runtime(req)
        req=self.request();req['headers']['SID']='synthetic-session'
        with self.assertRaises(EzMcdError):t.capture_runtime(req)
        req=self.request();req['headers']['d']=''
        with self.assertRaises(EzMcdError):t.capture_runtime(req)
        self.assertFalse((self.path/'runtime.json').exists())

    def test_different_risk_context_between_phases_does_not_prevent_config_restore_but_blocks_import(self):
        self.captured();r=self.recover();self.config(r)
        b=self.balance();b['request']['headers']['d']='another-official-risk-context'
        result=r.observe_restored_balance(b,request_sequence=r.request_started())
        self.assertTrue(result['restoration_verified'])
        self.assertFalse(result['candidate_verified_after_restoration'])
        self.assertFalse(r.state['runtime_context_matches_restore'])

    def test_runtime_context_mismatch_stops_collection_and_reports_field_names_only(self):
        t=self.prepare();self.config(t);self.now+=1
        request=self.request();request['headers']['token']='different-private-token'
        with self.assertRaises(EzMcdError) as caught:t.capture_runtime(request)
        self.assertIn('mismatch_token',caught.exception.code)
        self.assertNotIn('different-private-token',caught.exception.code)
        self.assertEqual(t.state['state'],'RECOVERY_REQUIRED')
        self.assertFalse(t.can_override);self.assertFalse(t.state['runtime_saved'])

    def test_rotated_same_member_session_proves_restore_but_never_allows_runtime_import(self):
        self.captured();r=self.recover();self.config(r)
        sample=self.balance();h=sample['request']['headers']
        h['sid']='rotated-own-session';h['token']='rotated-own-device'
        h.update(sign_v4(h,*KEYS[:2],datetime.fromtimestamp(self.now,timezone.utc),'GET',BALANCE))
        result=r.observe_restored_balance(sample,request_sequence=r.request_started())
        self.assertTrue(result['restoration_verified'])
        self.assertFalse(result['candidate_verified_after_restoration'])
        self.assertEqual(r.state['restored_context_fields_changed'],['sid','token'])

    def test_past_future_or_wrong_path_signature_cannot_create_observation(self):
        t=self.prepare();self.config(t)
        for at in (NOW-1,NOW+1):
            with self.assertRaisesRegex(EzMcdError,'sample_outside_capture'):t.capture_runtime(self.request(at=at))
        req=self.request()
        req['headers']['x-mcd-sign']=encode('/bff/order/orders',ObservedRuntime('A'*32,'b'*16),
            milliseconds=NOW*1000,random_value=11,counter=1,constants=KEYS[2])
        with self.assertRaises(ValueError):t.capture_runtime(req)

    def test_expiry_or_clock_rollback_turns_off_override(self):
        for now in (NOW-1,NOW+121):
            self.path=self.root/str(now);t=self.prepare();self.now=now
            with self.assertRaisesRegex(EzMcdError,'capture_expired'):self.config(t)
            self.assertFalse(t.can_override)
            self.assertEqual(t.state['state'],'RECOVERY_REQUIRED')
            self.now=NOW

    def test_repeated_runtime_is_not_replaced_or_renewed(self):
        t=self.captured();first=(self.path/'runtime.json').read_bytes();self.now+=1
        with self.assertRaisesRegex(EzMcdError,'runtime_not_collecting'):t.capture_runtime(self.request())
        self.assertEqual((self.path/'runtime.json').read_bytes(),first)

    def test_new_capture_never_reuses_existing_transaction(self):
        self.prepare()
        with self.assertRaisesRegex(EzMcdError,'transaction_exists_resume_recovery'):self.prepare()

    def test_account_change_does_not_prevent_public_config_recovery(self):
        self.captured();other=deepcopy(self.account);other['profile']['fields']['sid']='changed'
        r=Transaction(self.path,other,clock=lambda:self.now)
        self.assertEqual(self.config(r),ORIGINAL)
        with self.assertRaises(EzMcdError):r.observe_restored_balance(self.balance(),request_sequence=r.request_started())
        self.assertFalse(r.summary()['restoration_verified'])

    def test_changed_official_config_stops_collection_then_passes_untouched_in_recovery(self):
        t=self.prepare();changed=json.loads(ORIGINAL);changed['data']['unrelated']['kept']=['new official value']
        raw=json.dumps(changed).encode()
        with self.assertRaisesRegex(EzMcdError,'official_config_changed'):self.config(t,raw)
        self.assertFalse(t.can_override)
        r=self.recover()
        self.assertEqual(self.config(r,raw),raw)
        self.assertTrue(r.state['official_config_changed'])

    def test_candidate_cannot_escape_a_failed_durable_claim(self):
        t=self.prepare()
        with patch('transaction.write_private',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):self.config(t)
        self.assertFalse(t.can_override)
        self.assertFalse(read_private(self.path/'state.json')['candidate_may_have_applied'])

    def test_corrupted_snapshot_and_external_state_change_block_release(self):
        t=self.prepare();s=read_private(self.path/'state.json');s['deadline']+=1
        write_private(self.path/'state.json',s)
        with self.assertRaisesRegex(EzMcdError,'transaction_changed'):self.config(t)
        write_private(self.path/'state.json',t.state)
        c=read_private(self.path/'config.json');c['candidate']=c['original']
        write_private(self.path/'config.json',c)
        with self.assertRaisesRegex(EzMcdError,'config_snapshot_changed'):self.recover()

    def test_unknown_rules_duplicate_keys_and_nonfinite_config_refuse_before_files(self):
        variants=[b'{"success":true,"success":false}',b'{"code":NaN}',b'x'*65537]
        for alter in ('wildcard','method','extra','ratio','enabled'):
            c=json.loads(ORIGINAL);cfg=c['data']['tdSignConfig'];rule=cfg['rules'][0]
            if alter=='wildcard':rule['path']='/**'
            elif alter=='method':rule['method']='GET'
            elif alter=='extra':rule['unknown']=True
            elif alter=='ratio':rule['ratio']=True
            else:cfg['enabled']=False
            variants.append(json.dumps(c).encode())
        for value in variants:
            with self.assertRaises(EzMcdError):Transaction.prepare(self.path,self.account,value)
        self.assertFalse(self.path.exists())

    def test_config_host_path_status_method_scope(self):
        t=self.prepare()
        for key,value in [('host',CONFIG_HOST+'.example'),('path',CONFIG_PATH+'?x=1'),
                          ('status',302),('method','POST')]:
            with self.subTest(key=key),self.assertRaises(EzMcdError):self.config(t,**{key:value})
        self.assertEqual(t.state['state'],'PREPARED')

    def test_completed_transaction_never_reopens_capture(self):
        self.captured();r=self.recover();self.config(r)
        r.observe_restored_balance(self.balance(),request_sequence=r.request_started())
        r2=self.recover();self.assertEqual(self.config(r2),ORIGINAL)
        with self.assertRaises(EzMcdError):r2.capture_runtime(self.request())

    def test_private_files_exclude_policy_and_have_restrictive_mode(self):
        self.captured()
        for p in self.path.glob('*.json'):
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            self.assertNotIn('max_order_cents',p.read_text())
        self.assertEqual(self.path.stat().st_mode&0o777,0o700)


if __name__=='__main__':
    unittest.main()
