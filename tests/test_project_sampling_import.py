from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ezmcd.accounts import connect_samples, revoke_local
from ezmcd.cli import main
from ezmcd.errors import EzMcdError
from ezmcd.models import fingerprint
from ezmcd.protocol.signing import sign_v4
from ezmcd.protocol.v5 import ObservedRuntime, encode
from ezmcd.sampling_import import import_capture
from ezmcd.storage import Store, read_private, write_private
from test_project_accounts import KEYS, sample


class CaptureImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name).resolve())
        self.keys = KEYS[:2] + (tuple(range(64)), ())
        self.now = time.time() - 1
        for target, value in (
                ('ezmcd.accounts.constants', self.keys),
                ('ezmcd.provider.McdProvider.wallet', {'balance_cents': 100}),
                ('ezmcd.transport.Transport.verify_runtime',
                 {'http_status': 200, 'response': {'success': True, 'data': {'list': []}}})):
            p = patch(target, return_value=value)
            mocked = p.start()
            self.addCleanup(p.stop)
            if target.endswith('.wallet'):
                self.wallet = mocked
            if target.endswith('.verify_runtime'):
                self.verify = mocked
        path = self.store.root / 'seed.json'
        write_private(path, sample())
        connect_samples(self.store, 'mine', path, 'unused')
        account = self.store.account('mine')
        account['profile']['fields']['refreshToken'] = 'synthetic-refresh'
        account['policy']['allow_checkout'] = True
        account['policy']['max_order_cents'] = 1234
        self.store.update_account(account)
        self.wallet.reset_mock()
        self.store.claim('synthetic-attempt', 'consume')
        self.directory = self.store.root / 'captures' / ('a' * 32)
        self.make_capture()

    def make_capture(self, session='synthetic-session', member='synthetic-member', timestamp=None):
        balance = sample()
        headers = balance['request']['headers']
        headers.update(sid=session, meddyid=member)
        headers.update(sign_v4(headers, *KEYS[:2], datetime.now(timezone.utc), 'GET', balance['request']['path']))
        stamp = self.now if timestamp is None else timestamp
        runtime = {'request': {'method': 'POST', 'host': 'api.mcd.cn', 'path': '/bff/order/orders',
            'body': '', 'headers': {'sid': session, 'token': 'synthetic-device',
                'x-mcd-sign': encode('/bff/order/orders', ObservedRuntime('A' * 32, 'b' * 16),
                    milliseconds=int(stamp * 1000), random_value=3, counter=1, constants=self.keys[2])}}}
        account = self.store.account('mine')
        proxy = {'Enabled': 'No', 'Server': 'localhost', 'Port': '1234', 'Authenticated Proxy Enabled': '0'}
        files = {
            'config': {'schema_version': 2, 'output': str(self.directory), 'binary': account['binary'],
                'identity': account['identity'], 'account_fingerprint': fingerprint(account)},
            'audit': {'balance_saved': True, 'runtime_saved': True, 'blocked_requests': 2},
            'cleanup': {'official_app_closed': True, 'cleanup_errors': [], 'proxy_before': proxy,
                'proxy_after': proxy, 'persistent_certificate_retained': True},
            'balance': balance, 'runtime': runtime}
        for name, data in files.items():
            write_private(self.directory / (name + '.json'), data)

    def account_bytes(self):
        identity = self.store.account('mine')['identity']
        return (self.store.root / 'accounts' / (identity + '.json')).read_bytes()

    def edit(self, name, change):
        path = self.directory / (name + '.json')
        value = read_private(path)
        change(value)
        write_private(path, value)

    def test_success_preserves_policy_history_and_original_observation(self):
        before = deepcopy(self.store.account('mine'))
        result = import_capture(self.store, 'mine', self.directory)
        current = self.store.account('mine')
        self.assertTrue(result['account_updated'])
        self.assertFalse(result['already_imported'])
        self.assertEqual(current['policy'], before['policy'])
        self.assertEqual(current['profile']['fields']['refreshToken'], 'synthetic-refresh')
        self.assertEqual(current['runtime']['observed_at'], int(self.now * 1000) / 1000)
        self.assertTrue(result['runtime_available'])
        self.assertFalse(result['order_created'] or result['payment_attempted'] or result['cart_changed'])
        self.assertNotIn('synthetic-session', json.dumps(result))
        self.assertNotIn('synthetic-refresh', json.dumps(result))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0], 1)
        self.wallet.assert_called_once()
        self.verify.assert_called_once()

    def test_changed_session_drops_old_refresh_but_not_spending_policy(self):
        self.make_capture(session='synthetic-new-session')
        import_capture(self.store, 'mine', self.directory)
        current = self.store.account('mine')
        self.assertEqual(current['profile']['fields']['sid'], 'synthetic-new-session')
        self.assertNotIn('refreshToken', current['profile']['fields'])
        self.assertTrue(current['policy']['allow_checkout'])

    def test_restart_repeat_only_reads_and_does_not_rewrite_account(self):
        import_capture(self.store, 'mine', self.directory)
        before = self.account_bytes()
        result = import_capture(Store(self.store.root), 'mine', self.directory)
        self.assertTrue(result['already_imported'])
        self.assertFalse(result['account_updated'])
        self.assertEqual(before, self.account_bytes())
        self.assertEqual(self.wallet.call_count, 2)
        self.assertEqual(self.verify.call_count, 2)

    def test_response_loss_after_atomic_commit_recovers_from_receipt(self):
        from ezmcd.accounts import _enroll
        def lost(*args, **kwargs):
            _enroll(*args, **kwargs)
            raise OSError('synthetic interrupted output')
        with patch('ezmcd.sampling_import._enroll', side_effect=lost):
            with self.assertRaises(OSError):
                import_capture(self.store, 'mine', self.directory)
        before = self.account_bytes()
        self.assertTrue(import_capture(self.store, 'mine', self.directory)['already_imported'])
        self.assertEqual(before, self.account_bytes())

    def test_verification_failure_preserves_old_session_then_can_resume(self):
        self.make_capture(session='synthetic-new-session')
        before = self.account_bytes()
        self.verify.return_value = {'http_status': 401, 'response': {'success': False}}
        with self.assertRaisesRegex(EzMcdError, 'runtime_readonly_verification_failed'):
            import_capture(self.store, 'mine', self.directory)
        self.assertEqual(before, self.account_bytes())
        self.verify.return_value = {'http_status': 200, 'response': {'success': True, 'data': {'list': []}}}
        self.assertTrue(import_capture(self.store, 'mine', self.directory)['account_updated'])

    def test_balance_failure_never_commits_or_probes_runtime(self):
        before = self.account_bytes()
        self.wallet.side_effect = EzMcdError('session_expired_reconnect_locally')
        with self.assertRaises(EzMcdError):
            import_capture(self.store, 'mine', self.directory)
        self.assertEqual(before, self.account_bytes())
        self.verify.assert_not_called()

    def test_bad_cleanup_or_incomplete_capture_rejected_before_network(self):
        for name, change, error in (
                ('cleanup', lambda x: x.update(official_app_closed=False), 'cleanup_needs_attention'),
                ('cleanup', lambda x: x['proxy_after'].update(Enabled='Yes'), 'cleanup_needs_attention'),
                ('cleanup', lambda x: x.update(cleanup_errors=['proxy_restoration']), 'cleanup_needs_attention'),
                ('audit', lambda x: x.update(runtime_saved=False), 'capture_incomplete'),
                ('audit', lambda x: x.update(blocked_requests=1), 'capture_incomplete')):
            with self.subTest(name=name, change=change):
                self.make_capture()
                self.edit(name, change)
                with self.assertRaisesRegex(EzMcdError, error):
                    import_capture(self.store, 'mine', self.directory)
        self.wallet.assert_not_called()
        self.verify.assert_not_called()
        write_private(self.directory / 'audit.json', [])
        with self.assertRaisesRegex(EzMcdError, 'capture_incomplete'):
            import_capture(self.store, 'mine', self.directory)

    def test_capture_bound_to_original_account_and_client(self):
        for key, value in (('identity', 'other-member'), ('binary', '/other-client')):
            self.make_capture()
            self.edit('config', lambda x: x.update({key: value}))
            with self.assertRaisesRegex(EzMcdError, 'sampling_member_mismatch'):
                import_capture(self.store, 'mine', self.directory)
        self.make_capture(member='different-member')
        with self.assertRaisesRegex(EzMcdError, 'sampling_member_mismatch'):
            import_capture(self.store, 'mine', self.directory)
        self.wallet.assert_not_called()

    def test_revocation_or_any_account_change_prevents_import(self):
        account = self.store.account('mine')
        account['policy']['max_order_cents'] += 1
        self.store.update_account(account)
        with self.assertRaisesRegex(EzMcdError, 'sampling_account_changed'):
            import_capture(self.store, 'mine', self.directory)
        revoke_local(self.store, 'mine')
        with self.assertRaisesRegex(EzMcdError, 'active_official_account_required'):
            import_capture(self.store, 'mine', self.directory)
        self.wallet.assert_not_called()

    def test_legacy_capture_requires_manual_explicit_import(self):
        self.edit('config', lambda x: x.pop('account_fingerprint'))
        with self.assertRaisesRegex(EzMcdError, 'capture_binding_missing'):
            import_capture(self.store, 'mine', self.directory)
        self.wallet.assert_not_called()

    def test_directory_escape_symlink_and_public_samples_rejected(self):
        link = self.directory.with_name('b' * 32)
        link.symlink_to(self.directory, target_is_directory=True)
        for path in (link, self.store.root, self.store.root / '..' / self.directory.name):
            with self.assertRaisesRegex(EzMcdError, 'capture_directory_invalid'):
                import_capture(self.store, 'mine', path)
        (self.directory / 'balance.json').chmod(0o644)
        with self.assertRaisesRegex(EzMcdError, 'private_file_permissions_required'):
            import_capture(self.store, 'mine', self.directory)
        self.wallet.assert_not_called()

    def test_expired_or_mixed_session_samples_never_commit(self):
        before = self.account_bytes()
        self.make_capture(timestamp=time.time() - 86401)
        with self.assertRaisesRegex(EzMcdError, 'runtime_sample_expired'):
            import_capture(self.store, 'mine', self.directory)
        self.make_capture()
        self.edit('runtime', lambda x: x['request']['headers'].update(sid='other'))
        with self.assertRaisesRegex(EzMcdError, 'runtime_sample_account_mismatch'):
            import_capture(self.store, 'mine', self.directory)
        self.assertEqual(before, self.account_bytes())
        self.wallet.assert_not_called()

    def test_receipt_cannot_restore_changed_credentials_or_modified_samples(self):
        import_capture(self.store, 'mine', self.directory)
        self.edit('runtime', lambda x: x['request'].update(body='changed'))
        with self.assertRaisesRegex(EzMcdError, 'sampling_account_changed'):
            import_capture(self.store, 'mine', self.directory)
        self.assertEqual(self.wallet.call_count, 1)

    def test_repeat_preserves_later_policy_changes_without_credential_rollback(self):
        import_capture(self.store, 'mine', self.directory)
        record = self.store.account('mine')
        record['policy']['allow_checkout'] = False
        self.store.update_account(record)
        before = self.account_bytes()
        self.assertTrue(import_capture(self.store, 'mine', self.directory)['already_imported'])
        self.assertEqual(before, self.account_bytes())

    def test_expiry_during_live_verification_does_not_commit(self):
        before = self.account_bytes()
        def verify():
            self.now += 86402
            return {'http_status': 200, 'response': {'success': True, 'data': {'list': []}}}
        self.verify.side_effect = verify
        with patch('ezmcd.sampling_import.time.time', side_effect=lambda: self.now + 1):
            with self.assertRaisesRegex(EzMcdError, 'runtime_sample_expired'):
                import_capture(self.store, 'mine', self.directory)
        self.assertEqual(before, self.account_bytes())

    def test_cli_import_and_repeat_return_only_public_account_results(self):
        argv = ['ezmcd', '--data-dir', str(self.store.root), 'sampling-import', 'mine', str(self.directory)]
        for updated in (True, False):
            output = io.StringIO()
            with patch.object(sys, 'argv', argv), redirect_stdout(output):
                self.assertEqual(main(), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result['account_updated'], updated)
            self.assertNotIn('synthetic-session', output.getvalue())

    def test_suspended_cli_capture_never_imports_even_if_a_mock_claims_success(self):
        argv = ['ezmcd', '--data-dir', str(self.store.root), 'sampling-capture', 'mine', '--update-account']
        for complete in (False, True):
            with patch.object(sys, 'argv', argv), patch('ezmcd.sampling.RuntimeCapture') as capture, \
                    patch('ezmcd.sampling_import.import_capture', return_value={'account_updated': True}) as importer, \
                    redirect_stdout(io.StringIO()), patch('sys.stderr',io.StringIO()):
                capture.return_value.run.return_value = {'runtime_saved': complete, 'balance_saved': complete,
                    'account_updated': False, 'capture_directory': str(self.directory)}
                self.assertEqual(main(), 2)
                capture.assert_not_called();importer.assert_not_called()

    def test_concurrent_change_before_commit_is_not_overwritten(self):
        from ezmcd.accounts import _enroll
        def changed(*args, **kwargs):
            record = self.store.account('mine')
            record['policy']['allow_checkout'] = False
            self.store.update_account(record)
            return _enroll(*args, **kwargs)
        with patch('ezmcd.sampling_import._enroll', side_effect=changed):
            with self.assertRaisesRegex(EzMcdError, 'sampling_account_changed'):
                import_capture(self.store, 'mine', self.directory)
        self.assertFalse(self.store.account('mine')['policy']['allow_checkout'])
        self.wallet.assert_not_called()

    def test_failed_atomic_save_keeps_original_then_readonly_retry_succeeds(self):
        before = self.account_bytes()
        with patch('ezmcd.storage.os.replace', side_effect=OSError('synthetic disk error')):
            with self.assertRaises(OSError):
                import_capture(self.store, 'mine', self.directory)
        self.assertEqual(before, self.account_bytes())
        self.assertTrue(import_capture(self.store, 'mine', self.directory)['account_updated'])

    def test_malformed_successful_v5_response_never_commits(self):
        before = self.account_bytes()
        for payload, error in (({'success': True, 'data': {}}, 'runtime_verification_response_unrecognized'),
                               (None, 'runtime_readonly_verification_failed')):
            self.verify.return_value = {'http_status': 200, 'response': payload}
            with self.assertRaisesRegex(EzMcdError, error):
                import_capture(self.store, 'mine', self.directory)
        self.assertEqual(before, self.account_bytes())

    def test_changed_credentials_after_success_do_not_reactivate_receipt(self):
        import_capture(self.store, 'mine', self.directory)
        record = self.store.account('mine')
        record['profile']['fields']['sid'] = 'synthetic-later-login'
        self.store.update_account(record)
        before = self.account_bytes()
        with self.assertRaisesRegex(EzMcdError, 'sampling_account_changed'):
            import_capture(self.store, 'mine', self.directory)
        self.assertEqual(before, self.account_bytes())
        self.assertEqual(self.wallet.call_count, 1)

    def test_expired_repeat_does_not_renew_or_rewrite_observation(self):
        import_capture(self.store, 'mine', self.directory)
        before = self.account_bytes()
        with patch('ezmcd.sampling_import.time.time', return_value=self.now + 86402):
            with self.assertRaisesRegex(EzMcdError, 'runtime_sample_expired'):
                import_capture(self.store, 'mine', self.directory)
        self.assertEqual(before, self.account_bytes())
        self.assertEqual(self.wallet.call_count, 1)

    def add_session(self):
        source=fingerprint({'sid':'synthetic-old-session','token':'synthetic-device'})
        headers=read_private(self.directory/'balance.json')['request']['headers']
        self.edit('config',lambda x:x.update(refresh_session=source))
        self.edit('audit',lambda x:x.update(allowed_session_refreshes=1,refresh_succeeded=True,session_saved=True))
        write_private(self.directory/'session.json',{'schema_version':1,
            'identity':self.store.account('mine')['identity'],'source_session':source,
            'target_session':fingerprint({k:headers[k] for k in ('sid','token')}),
            'fields':{'sid':headers['sid'],'refreshToken':'synthetic-captured-refresh'}})

    def test_captured_refresh_commits_with_new_session_and_repeats_readonly(self):
        self.make_capture(session='synthetic-new-session')
        self.add_session()
        with patch('ezmcd.transport.Transport.send',side_effect=AssertionError('No refresh POST')):
            result=import_capture(self.store,'mine',self.directory)
            self.assertTrue(result['refresh_token_captured'])
            self.assertFalse(result['refresh_token_redemption_tested'])
            current=self.store.account('mine')
            self.assertEqual(current['profile']['fields']['refreshToken'],'synthetic-captured-refresh')
            self.assertTrue(current['policy']['allow_checkout'])
            before=self.account_bytes()
            again=import_capture(Store(self.store.root),'mine',self.directory)
        self.assertTrue(again['already_imported'])
        self.assertEqual(before,self.account_bytes())
        self.assertNotIn('synthetic-captured-refresh',json.dumps(result))
        self.assertEqual(self.wallet.call_count,2)
        self.assertEqual(self.verify.call_count,2)

    def test_captured_refresh_failure_does_not_partially_replace_account(self):
        self.make_capture(session='synthetic-new-session')
        self.add_session()
        before=self.account_bytes()
        self.verify.return_value={'http_status':401,'response':{'success':False}}
        with self.assertRaises(EzMcdError): import_capture(self.store,'mine',self.directory)
        self.assertEqual(before,self.account_bytes())

    def test_missing_or_invalid_session_rejected_before_network(self):
        changes=(('session',lambda x:x.update(identity='other-member')),
            ('session',lambda x:x.update(schema_version=True)),
            ('session',lambda x:x.update(source_session='wrong-source')),
            ('session',lambda x:x.update(target_session='wrong-device')),
            ('session',lambda x:x['fields'].update(sid='wrong-session')),
            ('session',lambda x:x['fields'].update(refreshToken='bad\nvalue')),
            ('session',lambda x:x['fields'].update(refreshToken=None)),
            ('session',lambda x:x.update(cookie='must-not-save')),
            ('config',lambda x:x.pop('refresh_session')),
            ('audit',lambda x:x.update(session_saved=False)),
            ('audit',lambda x:x.update(refresh_succeeded=False)),
            ('audit',lambda x:x.update(allowed_session_refreshes=2)))
        before=self.account_bytes()
        for name,change in changes:
            with self.subTest(name=name,change=change):
                self.make_capture();self.add_session();self.edit(name,change)
                with self.assertRaisesRegex(EzMcdError,'sampling_session_evidence_invalid'):
                    import_capture(self.store,'mine',self.directory)
        self.make_capture();self.add_session()
        (self.directory/'session.json').unlink()
        with self.assertRaisesRegex(EzMcdError,'sampling_session_evidence_invalid'):
            import_capture(self.store,'mine',self.directory)
        self.assertEqual(before,self.account_bytes())
        self.wallet.assert_not_called();self.verify.assert_not_called()

    def test_changed_refresh_evidence_cannot_overwrite_successful_import(self):
        self.add_session()
        import_capture(self.store,'mine',self.directory)
        before=self.account_bytes()
        self.edit('session',lambda x:x['fields'].update(refreshToken='synthetic-substituted'))
        with self.assertRaisesRegex(EzMcdError,'sampling_account_changed'):
            import_capture(self.store,'mine',self.directory)
        self.assertEqual(before,self.account_bytes())
        self.assertEqual(self.wallet.call_count,1)

    def test_session_file_permissions_and_symlink_rejected(self):
        self.add_session()
        path=self.directory/'session.json'
        path.chmod(0o644)
        with self.assertRaisesRegex(EzMcdError,'private_file_permissions_required'):
            import_capture(self.store,'mine',self.directory)
        path.unlink();path.symlink_to(self.directory/'missing.json')
        with self.assertRaises(EzMcdError): import_capture(self.store,'mine',self.directory)
        self.wallet.assert_not_called();self.verify.assert_not_called()

    def test_session_absent_preserves_legacy_receipt_fingerprint(self):
        files={k:read_private(self.directory/(k+'.json')) for k in ('balance','runtime')}
        result=import_capture(self.store,'mine',self.directory)
        self.assertFalse(result['refresh_token_captured'])
        self.assertEqual(self.store.account('mine')['sampling_import']['evidence_fingerprint'],fingerprint(files))

    def test_guard_generated_session_imports_without_rotating_token(self):
        from test_project_sampling_guard import flow_for, http
        from ezmcd.sampling_guard import RuntimeGuard, BALANCE, REFRESH
        from types import SimpleNamespace
        source={'sid':'synthetic-old-session','token':'synthetic-device'}
        self.edit('config',lambda x:x.update(refresh_session=fingerprint(source),deadline=time.time()+120))
        config=read_private(self.directory/'config.json')
        guard=RuntimeGuard(config)
        with patch.dict('sys.modules',{'mitmproxy':SimpleNamespace(http=http)}):
            refresh=flow_for('POST','https://api.mcd.cn'+REFRESH,source)
            guard.requestheaders(refresh)
            refresh.response=http.Response.make(200,b'{"success":true,"data":{"sid":"synthetic-session","refreshToken":"synthetic-official-refresh"}}')
            guard.response(refresh)
            balance=read_private(self.directory/'balance.json')
            flow=flow_for('GET','https://api.mcd.cn'+BALANCE,balance['request']['headers'])
            guard.requestheaders(flow)
            flow.response=http.Response.make(200,b'{"success":true}')
            guard.response(flow)
            runtime=read_private(self.directory/'runtime.json')['request']
            probe=flow_for('POST','https://api.mcd.cn'+runtime['path'])
            guard.requestheaders(probe)
            order=flow_for('POST','https://api.mcd.cn'+runtime['path'],runtime['headers'])
            guard.requestheaders(order)
            self.assertTrue(order.killed)
        with patch('ezmcd.transport.Transport.send',side_effect=AssertionError('No rotation')):
            self.assertTrue(import_capture(self.store,'mine',self.directory)['refresh_token_captured'])
        self.assertEqual(self.store.account('mine')['profile']['fields']['refreshToken'],'synthetic-official-refresh')


if __name__ == '__main__':
    unittest.main()
