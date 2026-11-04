from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import sys
import time
import unittest
from unittest.mock import patch

from ezmcd.errors import EzMcdError
from ezmcd.models import fingerprint
from ezmcd.protocol.signing import sign_v4
from ezmcd.sampling_connect import import_connection
from ezmcd.sampling_maintenance import AccountMaintenanceCapture, import_maintenance
from ezmcd.sampling_guard import RuntimeGuard, BALANCE, REFRESH
from ezmcd.sampling_console import SamplingConsole
from ezmcd.storage import Store, read_private, write_private
from test_project_accounts import KEYS, sample
import test_project_sampling_connect as connect_fixture
from test_project_sampling_console import Terminal
import test_project_sampling_guard as guard_fixture
import test_project_sampling as capture_fixture


class MaintenanceImportTests(unittest.TestCase):
    make_capture = connect_fixture.ConnectionImportTests.make_capture
    edit = connect_fixture.ConnectionImportTests.edit

    def setUp(self):
        connect_fixture.ConnectionImportTests.setUp(self)
        import_connection(self.store, 'mine', self.directory)
        old = self.store.account('mine')
        old['profile']['fields'].update(riskDeviceId='synthetic-risk', refreshToken='synthetic-refresh')
        old['runtime'] = {'observed_at': time.time()-100, 'device_hex': 'A'*32, 'environment_hex': 'b'*16}
        old['policy'].update(allow_checkout=True, max_order_cents=1234)
        self.store.update_account(old)
        self.store.claim('synthetic-attempt', 'consume')
        self.maintenance_capture()
        self.wallet.reset_mock()

    def maintenance_capture(self, *, session='synthetic-session', member='synthetic-member', device='synthetic-device'):
        self.make_capture()
        account = self.store.account('mine')
        self.edit('config', lambda c: c.update(schema_version=4, purpose='account_maintenance',
            identity=account['identity'], account_fingerprint=fingerprint(account)))
        balance = sample(); headers = balance['request']['headers']
        headers.update(sid=session, meddyid=member, token=device, d='synthetic-risk')
        headers.update(sign_v4(headers, *KEYS[:2], datetime.now(timezone.utc), 'GET', BALANCE))
        write_private(self.directory/'balance.json', balance)

    def account_bytes(self):
        return (self.store.root/'accounts'/(self.identity+'.json')).read_bytes()

    def test_same_session_preserves_runtime_age_refresh_policy_and_journal(self):
        old = self.store.account('mine')
        result = import_maintenance(self.store, 'mine', self.directory)
        new = self.store.account('mine')
        self.assertTrue(result['account_updated']); self.assertFalse(result['account_created'])
        self.assertEqual(new['policy'], old['policy']); self.assertEqual(new['runtime'], old['runtime'])
        self.assertEqual(new['profile']['fields']['refreshToken'], 'synthetic-refresh')
        self.assertFalse(result['runtime_collected'] or result['order_created'] or result['payment_attempted'])
        self.assertNotIn('synthetic-session', json.dumps(result)); self.wallet.assert_called_once()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0], 1)

    def test_changed_session_keeps_only_pending_same_device_runtime_and_drops_token(self):
        self.maintenance_capture(session='new-session')
        old = self.store.account('mine')
        result = import_maintenance(self.store, 'mine', self.directory)
        new = self.store.account('mine')
        self.assertIsNone(new['runtime']); self.assertTrue(result['runtime_recovery_available'])
        self.assertEqual(new['runtime_candidate']['evidence'], old['runtime'])
        self.assertNotIn('refreshToken', new['profile']['fields'])
        self.assertEqual(new['policy'], old['policy'])

    def test_changed_device_or_expired_evidence_cannot_become_candidate(self):
        self.maintenance_capture(session='new-session', device='different-device')
        result = import_maintenance(self.store, 'mine', self.directory)
        self.assertFalse(result['runtime_available'] or result['runtime_recovery_available'])
        old = self.store.account('mine')
        old['runtime'] = {'observed_at': time.time()-90000, 'device_hex': 'A'*32, 'environment_hex': 'b'*16}
        self.store.update_account(old); self.maintenance_capture(session='third-session', device='different-device')
        result = import_maintenance(self.store, 'mine', self.directory)
        self.assertFalse(result['runtime_recovery_available'])

    def test_same_session_stale_evidence_is_not_renewed(self):
        old = self.store.account('mine'); old['runtime']['observed_at'] = time.time()-90000
        self.store.update_account(old); self.maintenance_capture()
        result = import_maintenance(self.store, 'mine', self.directory)
        self.assertFalse(result['runtime_available'])
        self.assertEqual(self.store.account('mine')['runtime'], old['runtime'])

    def test_same_session_changed_risk_context_invalidates_old_runtime(self):
        balance = read_private(self.directory/'balance.json'); headers = balance['request']['headers']
        headers['d'] = 'changed-risk-context'
        headers.update(sign_v4(headers, *KEYS[:2], datetime.now(timezone.utc), 'GET', BALANCE))
        write_private(self.directory/'balance.json', balance)
        result = import_maintenance(self.store, 'mine', self.directory)
        self.assertFalse(result['runtime_available'] or result['runtime_recovery_available'])
        self.assertIsNone(self.store.account('mine')['runtime'])

    def test_stopped_refresh_can_reconnect_without_reusing_rejected_token(self):
        old = self.store.account('mine'); old['session_refresh'] = {'state': 'STOPPED'}
        self.store.update_account(old); self.maintenance_capture(session='new-session')
        result = import_maintenance(self.store, 'mine', self.directory)
        self.assertEqual(result['session_status'], 'verified_now')
        self.assertNotIn('refreshToken', self.store.account('mine')['profile']['fields'])
        self.assertEqual(self.wallet.call_count, 1)

    def test_restart_repeat_reads_once_without_rewriting_or_resetting_policy(self):
        import_maintenance(self.store, 'mine', self.directory)
        old = self.store.account('mine'); old['policy']['allow_checkout'] = False
        self.store.update_account(old); before = self.account_bytes()
        result = import_maintenance(Store(self.store.root), 'mine', self.directory)
        self.assertTrue(result['already_imported']); self.assertFalse(result['account_updated'])
        self.assertEqual(before, self.account_bytes()); self.assertEqual(self.wallet.call_count, 2)
        self.assertFalse(result['policy']['allow_checkout'])

    def test_wallet_failure_preserves_account_and_resumes_same_capture(self):
        before = self.account_bytes()
        self.wallet.side_effect = EzMcdError('session_expired_reconnect_locally')
        with self.assertRaises(EzMcdError): import_maintenance(self.store, 'mine', self.directory)
        self.assertEqual(before, self.account_bytes()); self.wallet.side_effect = None
        self.assertTrue(import_maintenance(self.store, 'mine', self.directory)['account_updated'])

    def test_lost_output_is_recovered_without_another_update(self):
        from ezmcd.accounts import _enroll
        def lost(*args, **kwargs):
            _enroll(*args, **kwargs)
            raise OSError('lost result')
        with patch('ezmcd.sampling_maintenance._enroll', side_effect=lost), self.assertRaises(OSError):
            import_maintenance(self.store, 'mine', self.directory)
        before = self.account_bytes()
        self.assertTrue(import_maintenance(self.store, 'mine', self.directory)['already_imported'])
        self.assertEqual(before, self.account_bytes())

    def test_changed_member_alias_scope_cleanup_and_signature_stop_before_network(self):
        for change in (
            ('config', lambda c: c.update(alias='other')),
            ('config', lambda c: c.update(identity='f'*64)),
            ('config', lambda c: c.update(account_fingerprint='bad')),
            ('config', lambda c: c.update(schema_version=3, purpose='account_connect', identity=None)),
            ('config', lambda c: c.update(refresh_session='unexpected')),
            ('audit', lambda c: c.update(captured_identity='f'*64)),
            ('audit', lambda c: c.update(allowed_session_refreshes=1)),
            ('cleanup', lambda c: c.update(app_launch_pending=True)),
            ('balance', lambda c: c['request']['headers'].update(sid='altered'))):
            self.maintenance_capture(); self.edit(*change)
            with self.subTest(change=change[0]), self.assertRaises(EzMcdError):
                import_maintenance(self.store, 'mine', self.directory)
        self.maintenance_capture(member='other-member')
        with self.assertRaisesRegex(EzMcdError, 'member_mismatch'):
            import_maintenance(self.store, 'mine', self.directory)
        self.wallet.assert_not_called()

    def test_changed_or_revoked_account_cannot_be_overwritten_by_old_capture(self):
        for revoked in (False, True):
            old = self.store.account('mine'); old['revoked'] = False
            self.store.update_account(old); self.maintenance_capture()
            old['revoked'] = revoked; old['profile']['fields']['sid'] = 'later-session'
            self.store.update_account(old); before = self.account_bytes()
            with self.assertRaises(EzMcdError): import_maintenance(self.store, 'mine', self.directory)
            self.assertEqual(before, self.account_bytes())
        self.wallet.assert_not_called()

    def test_new_member_mode_cannot_import_a_maintenance_capture(self):
        with self.assertRaisesRegex(EzMcdError, 'binding_missing'):
            import_connection(self.store, 'mine', self.directory)
        self.wallet.assert_not_called()

    def test_account_lock_prevents_update_before_any_request(self):
        with self.store.lock(self.identity), self.assertRaisesRegex(EzMcdError, 'account_busy'):
            import_maintenance(self.store, 'mine', self.directory)
        self.wallet.assert_not_called()


class MaintenanceGuardTests(unittest.TestCase):
    flow = guard_fixture.GuardTests.flow
    balance = guard_fixture.GuardTests.balance
    runtime = guard_fixture.GuardTests.runtime

    def setUp(self):
        guard_fixture.GuardTests.setUp(self)
        self.config.update(schema_version=4, purpose='account_maintenance', refresh_session=None)
        self.guard = RuntimeGuard(self.config, clock=lambda: self.now)

    def test_only_bound_member_balance_is_saved_and_no_runtime_or_refresh(self):
        self.balance(meddyid='other-member')
        self.assertFalse(self.guard.audit['balance_saved'])
        self.balance(); self.assertTrue(self.guard.audit['balance_saved'])
        self.guard.requestheaders(self.runtime())
        refresh = self.flow('POST', REFRESH, {'sid':'synthetic-session','token':'synthetic-device'})
        self.guard.requestheaders(refresh)
        self.assertTrue(refresh.killed); self.assertFalse(self.guard.audit['runtime_saved'])
        self.assertEqual(self.guard.audit['captured_identity'], self.config['identity'])
        self.assertNotIn('allowed_session_refreshes', self.guard.audit)

    def test_unbound_or_refresh_enabled_maintenance_is_rejected(self):
        for change in ({'identity':None}, {'identity':'bad'}, {'refresh_session':'bad'}, {'schema_version':3}):
            with self.assertRaisesRegex(ValueError, 'invalid_connection_capture_scope'):
                RuntimeGuard(self.config | change)


class MaintenanceLifecycleTests(unittest.TestCase):
    run_capture = capture_fixture.SamplingTests.run_capture
    managed_app = capture_fixture.SamplingTests.managed_app

    def test_balance_completes_with_original_account_binding_before_proxy_restore(self):
        capture_fixture.SamplingTests.setUp(self)
        self.capture.__class__ = AccountMaintenanceCapture; self.capture.binary = 'unused'
        write_private(self.capture.directory/'audit.json', {'runtime_saved':False,'balance_saved':True})
        app = self.managed_app(); result = self.run_capture()
        config = read_private(self.capture.directory/'config.json')
        self.assertEqual(config['account_fingerprint'], fingerprint(self.account))
        self.assertEqual(config['identity'], self.account['identity']); self.assertIsNone(config['refresh_session'])
        self.assertTrue(result['sample_import_ready']); self.assertFalse(result['account_updated'])
        self.assertEqual(app.request_close.call_count, 2)
        self.assertEqual(read_private(self.capture.directory/'cleanup.json')['proxy_after'], self.before)


class MaintenanceCliTests(unittest.TestCase):
    setUp = MaintenanceImportTests.setUp
    make_capture = MaintenanceImportTests.make_capture
    edit = MaintenanceImportTests.edit
    maintenance_capture = MaintenanceImportTests.maintenance_capture
    account_bytes = MaintenanceImportTests.account_bytes

    def test_wallet_maintenance_cli_and_resume_route_to_same_importer(self):
        from ezmcd.cli import main
        for resume in (False, True):
            args = ['ezmcd','--data-dir',str(self.store.root)]
            args += ['sampling-import','mine',str(self.directory),'--connect','--update'] if resume else ['sampling-connect','mine','--update']
            out, err = io.StringIO(), Terminal()
            with patch.object(sys,'argv',args), patch.object(sys,'stdin',Terminal('\n')), patch.object(sys,'stderr',err), redirect_stdout(out), \
                    patch('ezmcd.sampling_maintenance.AccountMaintenanceCapture') as capture:
                capture.return_value.run.return_value = {'sample_import_ready':True,'capture_directory':str(self.directory)}
                self.assertEqual(main(), 0)
                self.assertTrue(json.loads(out.getvalue())['account_updated' if not resume else 'already_imported'])
                if resume: capture.assert_not_called()
                else:
                    self.assertEqual(capture.call_args.kwargs['binary'], self.binary)
                    self.assertTrue(capture.call_args.kwargs['report'].update_account)
            if not resume:
                before = self.account_bytes()
            else:
                self.assertEqual(before, self.account_bytes())

    def test_failed_update_outputs_recovery_mode_and_private_errors_are_redacted(self):
        from ezmcd.cli import main
        args = ['ezmcd','--data-dir',str(self.store.root),'sampling-connect','mine','--update']
        out = io.StringIO()
        with patch.object(sys,'argv',args), patch.object(sys,'stdin',Terminal('\n')), patch.object(sys,'stderr',Terminal()), redirect_stdout(out), \
                patch('ezmcd.sampling_maintenance.AccountMaintenanceCapture') as capture, \
                patch('ezmcd.sampling_maintenance.import_maintenance',side_effect=OSError('secret')):
            capture.return_value.run.return_value = {'sample_import_ready':True,'capture_directory':str(self.directory)}
            self.assertEqual(main(), 2)
        result = json.loads(out.getvalue())
        self.assertEqual(result['resume_command'][-2:], ['--connect','--update'])
        self.assertFalse(result['account_updated']); self.assertNotIn('secret', out.getvalue())

    def test_console_uses_update_success_and_preserves_policy_language(self):
        out = Terminal(); console = SamplingConsole(stdin=Terminal('\n'), stderr=out, balance_only=True, update_account=True)
        console.ready('mine', 120); console({'stage':'CAPTURE_FINISHED','sample_import_ready':True,'capture_state':'complete'})
        self.assertIn('同一会员', out.getvalue()); self.assertNotIn('=true', out.getvalue())
        console.finished({'account_updated':True})
        self.assertIn('[ACCOUNT_UPDATED]',out.getvalue())
        self.assertNotIn('account_created=true', out.getvalue())

if __name__ == '__main__': unittest.main()
