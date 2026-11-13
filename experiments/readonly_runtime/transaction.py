"""Experimental offline-first config transaction; no networking or App control.

Not connected to the public CLI. Resuming an existing transaction can only
restore the exact original public config. An account is never written here.
"""
from copy import deepcopy
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from ezmcd.accounts import profile_from_balance_sample
from ezmcd.capture import sample_headers
from ezmcd.errors import EzMcdError
from ezmcd.models import fingerprint
from ezmcd.protocol.signing import BUILD, constants
from ezmcd.protocol.v5 import decode_verified_sample
from ezmcd.storage import read_private, write_private, secure_directory

CONFIG_HOST = 'img.mcd.cn'
CONFIG_PATH = '/app/main/assets/requestConfig101.json'
BALANCE = '/bff/member/mwallet/balance'
MAX_BYTES = 65536


def fail(code):
    raise EzMcdError('readonly_capture_' + code)


def unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            fail('duplicate_config_key')
        result[k] = v
    return result


def parse_config(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_BYTES:
        fail('config_size')
    try:
        payload = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
            parse_constant=lambda _: fail('nonfinite_config'))
    except (ValueError, UnicodeError, RecursionError):
        fail('invalid_config_json')
    if (not isinstance(payload, dict) or payload.get('success') is not True
            or type(payload.get('code')) is not int or payload['code'] != 200
            or not isinstance(payload.get('data'), dict)):
        fail('config_schema')
    config = payload['data'].get('tdSignConfig')
    if not isinstance(config, dict) or set(config) != {'enabled', 'rules'} or config['enabled'] is not True:
        fail('unsupported_sign_config')
    rules = config['rules']
    if not isinstance(rules, list) or not 1 <= len(rules) <= 100:
        fail('unsupported_sign_config')
    for r in rules:
        # Current observed shape only. Do not reason about unknown wildcard
        # priority, missing/default ratios or config extensions at runtime.
        if (not isinstance(r, dict) or set(r) != {'method', 'path', 'ratio'}
                or r['method'] != 'POST' or not isinstance(r['path'], str)
                or not re.fullmatch(r'/[A-Za-z0-9_/-]{1,255}', r['path'])
                or type(r['ratio']) not in (int, float) or not math.isfinite(r['ratio'])
                or not 0 <= r['ratio'] <= 100):
            fail('unsupported_sign_rule')
    return payload


def candidate_bytes(original):
    payload = parse_config(original)
    payload['data']['tdSignConfig']['rules'].append(
        {'method': 'GET', 'path': BALANCE, 'ratio': 100})
    result = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    if len(result) > MAX_BYTES:
        fail('config_size')
    return result


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def bound_headers(profile, request, version, *, allow_new_risk=False):
    if (not isinstance(request, dict)
            or (request.get('method'), request.get('host'), request.get('path'), request.get('body'))
                != ('GET', 'api.mcd.cn', BALANCE, '')):
        fail('not_exact_balance_get')
    headers = sample_headers(request.get('headers'))
    fields = profile.get('fields', {})
    expected = {name: fields.get(field) for name, field in (
        ('sid', 'sid'), ('token', 'deviceToken'), ('d', 'riskDeviceId'))}
    expected.update({k: profile.get('client_headers', {}).get(k) for k in ('ct', 'v', 'language')})
    if allow_new_risk:
        expected['d']=headers.get('d')
    if (profile.get('build_sha256') != BUILD or expected.get('ct') != '101'
            or any(not isinstance(v, str) or not v for v in expected.values())
            or any(headers.get(k) != v for k, v in expected.items()) or headers.get('sv') != version
            or ('meddyid' in headers and headers['meddyid'] != fields.get('meddyId'))):
        mismatches=[key for key,value in expected.items()
            if not isinstance(value,str) or not value or headers.get(key)!=value]
        if profile.get('build_sha256')!=BUILD:mismatches.append('build')
        if expected.get('ct')!='101':mismatches.append('ct')
        if headers.get('sv')!=version:mismatches.append('sv')
        if 'meddyid' in headers and headers['meddyid']!=fields.get('meddyId'):mismatches.append('meddyid')
        # Field names are safe diagnostics; never include either actual value.
        fail('account_or_device_mismatch_'+','.join(sorted(set(mismatches))))
    return headers


class Transaction:
    """One sampler process owns this object under an external store lock.

    Recovery requires a NEW process epoch after the previous sampler's pending
    responses and the previous App process have been stopped. Constructor-only
    intent never counts as a verified recovery.
    """
    def __init__(self, directory, account, *, clock=time.time):
        self.directory = Path(directory)
        self.account = deepcopy(account)
        self.clock = clock
        self.epoch = uuid.uuid4().hex
        self.can_override = False
        self.sequence = 0
        self.restore_response_sequence = None
        self.restore_response_time = None
        self.state = read_private(self.directory/'state.json')
        self._validate_saved()
        if self.state['state'] != 'RESTORED':
            self._save(state='RECOVERY_REQUIRED', recovery_epoch=self.epoch)

    @classmethod
    def prepare(cls, directory, account, original, *, seconds=120, clock=time.time):
        if (type(seconds) is not int or not 30 <= seconds <= 120 or account.get('revoked')
                or account.get('provider') != 'mcd-cn' or not account.get('identity')
                or account.get('session_refresh', {}).get('state') in ('CLAIMED','CANDIDATE','STOPPED')):
            fail('invalid_scope')
        candidate = candidate_bytes(original)
        now = clock()
        if not math.isfinite(now):
            fail('invalid_clock')
        # A side-effect-free boundary check before creating any transaction.
        p = account.get('profile', {})
        fields = p.get('fields', {})
        if (p.get('build_sha256') != BUILD or not fields.get('meddyId')
                or any(not fields.get(k) for k in ('sid','deviceToken','riskDeviceId'))):
            fail('incomplete_account')
        constants(account['binary'])
        directory = Path(directory)
        if directory.exists() or directory.is_symlink():
            fail('transaction_exists_resume_recovery')
        secure_directory(directory)
        write_private(directory/'config.json', {'original': original.decode('utf-8'),
            'candidate': candidate.decode('utf-8')})
        write_private(directory/'state.json', {'schema_version': 1, 'state': 'PREPARED',
            'account_fingerprint': fingerprint(account), 'created_at': now, 'deadline': now+seconds,
            'original_sha256': sha(original), 'candidate_sha256': sha(candidate),
            'candidate_may_have_applied': False, 'candidate_responses': 0,
            'runtime_saved': False, 'restore_observed': False})
        # Only the preparing process can release a candidate, with no saved flag
        # that another process could use to resume overriding after a crash.
        obj = object.__new__(cls)
        obj.directory, obj.account, obj.clock = directory, deepcopy(account), clock
        obj.epoch, obj.can_override, obj.sequence = uuid.uuid4().hex, True, 0
        obj.restore_response_sequence = obj.restore_response_time = None
        obj.state = read_private(directory/'state.json')
        obj._validate_saved()
        return obj

    def _validate_saved(self):
        s = self.state
        if (s.get('schema_version') != 1
                or s.get('state') not in {'PREPARED','MAY_HAVE_APPLIED','RECOVERY_REQUIRED','RESTORE_SENT','RESTORED'}
                or type(s.get('created_at')) not in (int,float) or type(s.get('deadline')) not in (int,float)
                or not math.isfinite(s['created_at']) or not 30 <= s['deadline']-s['created_at'] <= 120):
            fail('transaction_binding_changed')
        # Public configuration recovery must remain available if the project
        # account changed. Such a run cannot collect or accept credentials.
        self.account_matches = s.get('account_fingerprint') == fingerprint(self.account)
        config = read_private(self.directory/'config.json')
        original, candidate = config['original'].encode('utf-8'), config['candidate'].encode('utf-8')
        if (sha(original) != s.get('original_sha256') or sha(candidate) != s.get('candidate_sha256')
                or candidate_bytes(original) != candidate):
            fail('config_snapshot_changed')
        self.original, self.candidate = original, candidate

    def _save(self, **changes):
        if read_private(self.directory/'state.json') != self.state:
            self.can_override = False
            fail('transaction_changed')
        updated = self.state | changes
        try:
            write_private(self.directory/'state.json', updated)
        except Exception:
            self.can_override = False
            raise
        self.state = updated

    def request_started(self):
        self.sequence += 1
        return self.sequence

    def config_response(self, *, method, host, path, status, raw):
        """Called at response time, so delayed responses use current mode.

        The wrapper must keep a local failure response attached until this
        returns, then replace the body. No account-bearing config requests pass.
        """
        if (method, host, path, status) != ('GET', CONFIG_HOST, CONFIG_PATH, 200):
            fail('config_route_or_status')
        # Fail before changing the client if the official bytes changed. The
        # original is retained for a deliberate recovery even if CDN later drifts.
        self.sequence += 1
        now = self.clock()
        if self.can_override and self.state['state'] in ('PREPARED','MAY_HAVE_APPLIED'):
            if not isinstance(raw, bytes) or raw != self.original:
                self.require_recovery()
                fail('official_config_changed')
            if not self.state['created_at'] <= now <= self.state['deadline']:
                self.can_override = False
                self._save(state='RECOVERY_REQUIRED')
                fail('capture_expired_restore_required')
            # Durable intent precedes releasing bytes; even a crash immediately
            # afterward leaves an explicit unresolved restoration obligation.
            self._save(state='MAY_HAVE_APPLIED', candidate_may_have_applied=True,
                candidate_responses=self.state['candidate_responses']+1,
                last_candidate_at=now, collector_epoch=self.epoch)
            return self.candidate
        if self.state['state'] == 'RESTORED':
            return raw
        # Same-process restoration is intentionally insufficient: a response
        # already in flight could still apply the candidate afterward.
        if self.state.get('collector_epoch') == self.epoch:
            fail('new_recovery_epoch_required')
        # Recovery never manufactures an older configuration over a changed
        # official response. Forward the validated current public bytes intact.
        parse_config(raw)
        self.restore_response_sequence, self.restore_response_time = self.sequence, now
        self._save(state='RESTORE_SENT', restore_observed=False, recovery_epoch=self.epoch,
            restoration_config_sha256=sha(raw), official_config_changed=raw != self.original,
            restore_response_at=now)
        return raw

    def capture_runtime(self, request):
        if (not self.can_override or self.state['state'] != 'MAY_HAVE_APPLIED'
                or self.state['runtime_saved'] or not self.account_matches):
            fail('runtime_not_collecting')
        now = self.clock()
        if not self.state['created_at'] <= now <= self.state['deadline']:
            self.require_recovery()
            fail('capture_expired_restore_required')
        try:
            headers = bound_headers(self.account['profile'], request, 'v5',allow_new_risk=True)
        except EzMcdError:
            # Stop on the first context mismatch. Letting the App retry a
            # deliberately blocked wallet request adds no useful evidence.
            self.require_recovery()
            raise
        observed, inputs = decode_verified_sample(BALANCE, headers.get('x-mcd-sign'),
            constants(self.account['binary'])[2])
        stamp = inputs['milliseconds']/1000
        if not self.state['last_candidate_at'] <= stamp <= now:
            fail('sample_outside_capture')
        # Drop all unneeded headers and any response. No original request will
        # be replayed, and no synthetic order-shaped sample is constructed.
        kept = {k: v for k,v in headers.items() if k in {
            'sid','token','d','ct','v','language','sv','meddyid','x-mcd-sign'}}
        sample = {'request': {'method':'GET','host':'api.mcd.cn','path':BALANCE,'body':'','headers':kept},
            'request_blocked_before_upstream': True, 'source':'official_balance_get_v5',
            'observed_at': stamp, 'collector_epoch': self.epoch}
        write_private(self.directory/'runtime.json', sample)
        self.can_override = False
        self._save(state='RECOVERY_REQUIRED', runtime_saved=True,
            runtime_fingerprint=fingerprint(sample), observed_at=stamp)
        return {'runtime_saved': True, 'recovery_required': True, 'account_updated': False}

    def require_recovery(self):
        self.can_override = False
        if self.state['state'] != 'RESTORED':
            self._save(state='RECOVERY_REQUIRED')

    def observe_restored_balance(self, sample, *, request_sequence):
        if (not self.account_matches or self.can_override or self.state['state'] != 'RESTORE_SENT'
                or self.state.get('collector_epoch') == self.epoch
                or self.restore_response_sequence is None
                or type(request_sequence) is not int
                or not self.restore_response_sequence < request_sequence <= self.sequence):
            fail('restore_response_not_observed')
        # Restoring App configuration is independent of importing credentials.
        # A freshly signed successful v4 read by the same member proves the
        # GET override is gone even if that member's App session has rotated.
        # The importer still binds every session/device field to its candidate.
        profile = profile_from_balance_sample(sample, self.account['binary'])
        if profile['fields']['meddyId'] != self.account['profile']['fields']['meddyId']:
            fail('restored_member_mismatch')
        headers = sample_headers(sample['request']['headers'])
        if headers.get('x-mcd-sign'):
            fail('ambiguous_restored_signature')
        old_fields=self.account['profile']['fields']
        changed=[key for key,field in (('sid','sid'),('token','deviceToken'),('d','riskDeviceId'))
            if headers.get(key)!=old_fields.get(field)]
        changed += [key for key in ('ct','v','language')
            if headers.get(key)!=self.account['profile'].get('client_headers',{}).get(key)]
        stamp = parsedate_to_datetime(headers['authorization'].split('#')[4]).timestamp()
        now = self.clock()
        if not math.floor(self.restore_response_time) <= stamp <= now:
            fail('stale_restoration_request')
        # A GET arriving before config restore cannot be used as completion,
        # even if its response arrives afterward and contains a valid v4 HMAC.
        minimal = deepcopy(sample)
        minimal['response']['body'] = {'success': True, 'data': {'userId': profile['fields']['meddyId']}}
        write_private(self.directory/'restored-balance.json', minimal)
        context_matches=False
        if self.state['runtime_saved']:
            runtime=read_private(self.directory/'runtime.json')
            if fingerprint(runtime)!=self.state['runtime_fingerprint']:
                fail('runtime_evidence_changed')
            first=runtime['request']['headers']
            context_matches=all(first.get(k)==headers.get(k) for k in ('sid','token','d','ct','v','language'))
        self._save(state='RESTORED', restore_observed=True, restored_at=now,
            restoration_fingerprint=fingerprint(minimal),runtime_context_matches_restore=context_matches,
            restored_context_fields_changed=changed)
        return self.summary()

    def summary(self):
        done = self.state['state'] == 'RESTORED' and self.state['restore_observed'] is True
        return {'state': self.state['state'], 'runtime_saved': self.state['runtime_saved'],
            'restoration_verified': done,
            'candidate_may_have_applied': self.state['candidate_may_have_applied'],
            'account_updated': False, 'import_ready': False,
            'candidate_verified_after_restoration': done and self.state['runtime_saved']
                and self.state.get('runtime_context_matches_restore') is True,
            'order_created': False, 'payment_attempted': False, 'system_proxy_modified': False}
