import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from ezmcd.errors import EzMcdError
from ezmcd.sampling_console import SamplingConsole


class Terminal(io.StringIO):
    def isatty(self):
        return True


class SamplingConsoleTests(unittest.TestCase):
    def setUp(self):
        self.input = Terminal('\n')
        self.output = Terminal()
        self.console = SamplingConsole(stdin=self.input, stderr=self.output)

    def test_enter_is_required_and_prestart_guidance_does_not_claim_protection(self):
        self.console.ready('synthetic', 120)
        self.assertIn('现在尚未启用代理', self.output.getvalue())
        self.assertIn('按回车开始', self.output.getvalue())
        self.assertEqual(self.input.tell(), 1)

    def test_piped_input_or_hidden_prompts_fail_before_reading_input(self):
        for input, output in ((io.StringIO('\n'), self.output), (self.input, io.StringIO())):
            with self.assertRaisesRegex(EzMcdError, 'interactive_terminal_required'):
                SamplingConsole(stdin=input, stderr=output).ready('synthetic', 120)
            self.assertEqual(input.tell(), 0)

    def test_eof_nonempty_input_and_interrupt_cancel_before_start(self):
        for value in ('', 'q\n', 'not-ready\n'):
            with self.assertRaisesRegex(EzMcdError, 'cancelled_before_start'):
                SamplingConsole(stdin=Terminal(value), stderr=self.output).ready('synthetic', 120)
        self.input.readline = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaisesRegex(EzMcdError, 'cancelled_before_start'):
            self.console.ready('synthetic', 120)

    def test_guard_stage_only_requests_wallet_before_submission(self):
        self.console({'stage': 'GUARD_ACTIVE'})
        self.assertIn('先进入麦钱包余额页', self.output.getvalue())
        self.assertIn('暂时不要提交订单', self.output.getvalue())

    def test_current_balance_stage_shows_bounded_time_and_one_blocked_submission(self):
        self.console({'stage': 'BALANCE_CAPTURED', 'capture_window_active': True, 'seconds_remaining': 65})
        self.assertIn('65 秒', self.output.getvalue())
        self.assertIn('在当前保护窗口内提交一次', self.output.getvalue())
        self.assertIn('不要重试或付款', self.output.getvalue())

    def test_stale_incomplete_or_malformed_balance_stage_never_instructs_submission(self):
        for update in ({}, {'capture_window_active': False, 'seconds_remaining': 60},
            {'capture_window_active': True, 'seconds_remaining': 0},
            {'capture_window_active': True, 'seconds_remaining': True},
            {'capture_window_active': True, 'seconds_remaining': '60'}):
            output = Terminal()
            SamplingConsole(stderr=output)({'stage': 'BALANCE_CAPTURED', **update})
            self.assertIn('不要再提交', output.getvalue())
            self.assertNotIn('提交一次', output.getvalue())

    def test_close_stage_retains_block_until_app_exits(self):
        self.console({'stage': 'CLOSE_OFFICIAL_APP_NOW'})
        self.assertIn('退出前阻断继续保留', self.output.getvalue())

    def test_cleanup_failure_is_not_presented_as_restored(self):
        for event in ({'proxy_restored': False, 'errors': []},
                      {'proxy_restored': True, 'errors': ['proxy_verification']}, {}):
            output = Terminal()
            SamplingConsole(stderr=output)({'stage': 'CLEANUP_RESULT', **event})
            self.assertIn('尚未全部通过', output.getvalue())
            self.assertNotIn('代理已恢复', output.getvalue())
        self.console({'stage': 'CLEANUP_RESULT', 'proxy_restored': True, 'errors': []})
        self.assertIn('代理已恢复', self.output.getvalue())
        self.assertIn('不能再沿用', self.output.getvalue())

    def test_captured_evidence_does_not_claim_import_already_finished(self):
        self.console({'stage': 'CAPTURE_FINISHED', 'sample_import_ready': True, 'capture_state': 'complete'})
        self.assertIn('请继续等待最终结果', self.output.getvalue())
        self.assertIn('account_updated=true', self.output.getvalue())
        self.assertNotIn('账号已更新', self.output.getvalue())

    def test_incomplete_capture_and_unknown_event_text_do_not_leak_diagnostics(self):
        self.console({'stage': 'CAPTURE_FINISHED', 'sample_import_ready': False, 'capture_state': 'balance_rejected',
            'next_action': 'secret-session-value'})
        self.console({'stage': 'unknown', 'next': 'secret-session-value'})
        self.assertIn('不要导入部分文件', self.output.getvalue())
        self.assertNotIn('secret-session-value', self.output.getvalue())


class SamplingConsoleCliTests(unittest.TestCase):
    def run_cli(self, value, *, interactive=True, record=None):
        from ezmcd.cli import main
        with tempfile.TemporaryDirectory() as root:
            args=['ezmcd','--data-dir',root,'sampling-capture','synthetic','--update-account']
            if interactive:
                args.append('--interactive')
            output, error = io.StringIO(), Terminal()
            events = []
            input = Terminal(value)
            original = input.readline
            def read():
                events.append('prompt')
                return original()
            input.readline = read
            with patch.object(sys,'argv',args), patch.object(sys,'stdin',input), patch.object(sys,'stderr',error), \
                    patch('ezmcd.storage.Store.account', return_value=record or {'provider':'mcd-cn'}), \
                    patch('ezmcd.sampling.RuntimeCapture') as capture, \
                    patch('ezmcd.sampling_import.import_capture', return_value={'account_updated':True}) as importer, \
                    redirect_stdout(output):
                def run():
                    events.append('capture')
                    console = capture.call_args.kwargs['report']
                    if console:
                        console({'stage':'CAPTURE_FINISHED','sample_import_ready':True,'capture_state':'complete'})
                    return {'runtime_saved':True,'balance_saved':True,'capture_directory':str(Path(root)/'capture')}
                capture.return_value.run.side_effect=run
                code=main()
                return code, output.getvalue(), error.getvalue(), events, capture.return_value.run.call_count, importer.call_count

    def test_cli_waits_before_capture_keeps_final_stdout_json_and_imports_once(self):
        code, output, error, events, captures, imports = self.run_cli('\n')
        self.assertEqual((code, events, captures, imports), (0, ['prompt','capture'], 1, 1))
        self.assertTrue(json.loads(output)['account_updated'])
        self.assertIn('请继续等待最终结果', error)
        self.assertNotIn('按回车', output)

    def test_cli_cancel_has_no_capture_or_import(self):
        code, output, error, events, captures, imports = self.run_cli('q\n')
        self.assertEqual((code, events, captures, imports), (2, ['prompt'], 0, 0))
        self.assertEqual(output, '')
        self.assertIn('sampling_cancelled_before_start', error)

    def test_invalid_or_revoked_account_does_not_ask_user_to_prepare(self):
        for record in ({'provider':'demo'}, {'provider':'mcd-cn','revoked':True}):
            code, output, error, events, captures, imports = self.run_cli('\n', record=record)
            self.assertEqual((code, events, captures, imports), (2, [], 0, 0))
            self.assertIn('active_official_account_required', error)

    def test_noninteractive_cli_keeps_existing_machine_readable_behavior(self):
        code, output, error, events, captures, imports = self.run_cli('', interactive=False)
        self.assertEqual((code, events, captures, imports), (0, ['capture'], 1, 1))
        self.assertTrue(json.loads(output)['account_updated'])
        self.assertEqual(error, '')


if __name__ == '__main__':
    unittest.main()
