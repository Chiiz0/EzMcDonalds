"""Experimental candidate importer: original account/policy/journal remain bound."""
from copy import deepcopy
from pathlib import Path
from email.utils import parsedate_to_datetime
import math
import re
import time

from ezmcd.errors import EzMcdError
from ezmcd.models import fingerprint
from ezmcd.provider import McdProvider
from ezmcd.protocol.signing import constants
from ezmcd.protocol.v5 import decode_verified_sample
from ezmcd.sampling_import import verify_capture_runtime
from ezmcd.storage import read_private, write_private
from transaction import BALANCE, bound_headers, Transaction


def apply_evidence(store,alias,directory):
    directory=Path(directory).absolute()
    if (directory.parent!=store.root/'readonly-captures' or directory.is_symlink()
            or not re.fullmatch(r'[0-9a-f]{32}',directory.name)):
        raise EzMcdError('readonly_capture_directory_invalid')
    saved=read_private(directory/'transaction/state.json')
    runtime_sample=read_private(directory/'transaction/runtime.json')
    balance_sample=read_private(directory/'transaction/restored-balance.json')
    cleanup=read_private(directory/'cleanup.json')
    if (saved.get('state')!='RESTORED' or saved.get('restore_observed') is not True
            or saved.get('runtime_saved') is not True
            or saved.get('runtime_context_matches_restore') is not True
            or saved.get('collector_epoch')==saved.get('recovery_epoch')
            or saved.get('runtime_fingerprint')!=fingerprint(runtime_sample)
            or saved.get('restoration_fingerprint')!=fingerprint(balance_sample)
            or any(cleanup.get(k) is not True for k in ('official_app_closed','capture_processes_stopped',
                'interphase_shutdown_verified','system_proxy_unchanged','temporary_leaf_keys_removed'))
            or cleanup.get('system_proxy_modified') is not False or cleanup.get('errors')!=[]):
        raise EzMcdError('readonly_capture_cleanup_or_evidence_incomplete')
    receipt={'capture_id':directory.name,'source':'official_balance_get_v5',
        'evidence_fingerprint':fingerprint({'runtime':runtime_sample,'balance':balance_sample,'cleanup':cleanup})}
    account=store.account(alias)

    def candidate(record,*,repeated=False):
        if (record.get('revoked') or record['provider']!='mcd-cn'
                or record.get('session_refresh',{}).get('state') in ('CLAIMED','CANDIDATE','STOPPED')):
            raise EzMcdError('readonly_capture_account_not_ready')
        if not repeated and fingerprint(record)!=saved.get('account_fingerprint'):
            raise EzMcdError('readonly_capture_account_changed')
        # Re-validate stored config snapshots as well as both actual signatures.
        Transaction(directory/'transaction',record)
        headers=bound_headers(record['profile'],runtime_sample.get('request'),'v5',allow_new_risk=True)
        new_profile=deepcopy(record['profile'])
        new_profile['fields']['riskDeviceId']=headers['d']
        observed,inputs=decode_verified_sample(BALANCE,headers.get('x-mcd-sign'),constants(record['binary'])[2])
        stamp=inputs['milliseconds']/1000
        if (stamp!=runtime_sample.get('observed_at') or stamp!=saved.get('observed_at')
                or not saved['last_candidate_at']<=stamp<=saved['deadline']
                or not 0<=time.time()-stamp<=86400
                or (record.get('runtime') and record['runtime']['observed_at']>stamp)):
            raise EzMcdError('readonly_capture_observation_invalid_or_expired')
        from ezmcd.accounts import profile_from_balance_sample
        restored_headers=bound_headers(new_profile,balance_sample.get('request'),'v4')
        restored_stamp=parsedate_to_datetime(restored_headers['authorization'].split('#')[4]).timestamp()
        if not math.floor(saved['restore_response_at'])<=restored_stamp<=saved['restored_at']<=time.time():
            raise EzMcdError('readonly_capture_restoration_timing_invalid')
        profile=profile_from_balance_sample(balance_sample,record['binary'])
        if profile['fields']['meddyId']!=record['profile']['fields']['meddyId']:
            raise EzMcdError('readonly_capture_member_changed')
        result=deepcopy(record)
        result['profile']=new_profile
        result['runtime']={'device_hex':observed.device_hex,'environment_hex':observed.environment_hex,
            'observed_at':stamp}
        result.pop('runtime_candidate',None)
        result['readonly_runtime_import']=receipt|{'runtime_fingerprint':fingerprint(result['runtime']),
            'profile_fingerprint':fingerprint(result['profile']),
            'device_context_field_source':'matching official v5 and restored v4 wallet requests',
            'device_context_field':'riskDeviceId'}
        McdProvider(result).wallet()
        verify_capture_runtime(result)
        if fingerprint(store.account(alias))!=fingerprint(record):
            raise EzMcdError('readonly_capture_account_changed_during_verification')
        if not repeated:
            archive={'account_fingerprint':fingerprint(record),
                'runtime':record.get('runtime'),'runtime_candidate':record.get('runtime_candidate'),
                'runtime_use':record.get('runtime_use'),
                'risk_device_id':record['profile']['fields'].get('riskDeviceId')}
            archive_path=directory/'runtime-before.json'
            if archive_path.exists():
                if read_private(archive_path)!=archive:
                    raise EzMcdError('readonly_capture_previous_runtime_archive_changed')
            else:write_private(archive_path,archive)
        return result

    previous=account.get('readonly_runtime_import',{})
    repeated=(all(previous.get(k)==v for k,v in receipt.items())
        and previous.get('runtime_fingerprint')==fingerprint(account.get('runtime'))
        and previous.get('profile_fingerprint')==fingerprint(account.get('profile')))
    if repeated:
        with store.lock(account['identity']):
            if store.account(alias)!=account:raise EzMcdError('readonly_capture_account_changed')
            candidate(account,repeated=True)
    else:
        store.modify_account(alias,candidate)
    return {'account':alias,'account_updated':not repeated,'already_imported':repeated,
        'source':'official_balance_get_v5','observation_time_preserved':True,
        'session_preserved':True,'policy_preserved':True,'journal_preserved':True,
        'verification':['wallet_balance_get','v5_order_list_get'],
        'order_created':False,'payment_attempted':False,'order_post_acceptance_verified':False}
