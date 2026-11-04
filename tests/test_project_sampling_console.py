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
        self.console = SamplingConsole(stdin=self.input, stderr=self.output, balance_only=True)

    def test_enter_is_required_and_prestart_guidance_does_not_claim_protection(self):
        self.console.ready('synthetic', 120)
        self.assertIn('本轮不修改系统代理', self.output.getvalue())
        self.assertIn('按回车开始', self.output.getvalue())
        self.assertEqual(self.input.tell(), 1)

    def test_piped_input_or_hidden_prompts_fail_before_reading_input(self):
        for input, output in ((io.StringIO('\n'), self.output), (self.input, io.StringIO())):
            with self.assertRaisesRegex(EzMcdError, 'interactive_terminal_required'):
                SamplingConsole(stdin=input, stderr=output, balance_only=True).ready('synthetic', 120)
            self.assertEqual(input.tell(), 0)

    def test_eof_nonempty_input_and_interrupt_cancel_before_start(self):
        for value in ('', 'q\n', 'not-ready\n'):
            with self.assertRaisesRegex(EzMcdError, 'cancelled_before_start'):
                SamplingConsole(stdin=Terminal(value), stderr=self.output, balance_only=True).ready('synthetic', 120)
        self.input.readline = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaisesRegex(EzMcdError, 'cancelled_before_start'):
            self.console.ready('synthetic', 120)

    def test_guard_stage_only_requests_wallet_before_submission(self):
        self.console({'stage': 'GUARD_ACTIVE'})
        self.assertIn('先进入麦钱包余额页', self.output.getvalue())
        self.assertIn('暂时不要提交订单', self.output.getvalue())

    def test_even_stale_runtime_events_never_invite_submission(self):
        self.console({'stage': 'BALANCE_CAPTURED', 'capture_window_active': True, 'seconds_remaining': 65})
        self.assertNotIn('提交一次', self.output.getvalue())
        self.assertIn('本轮正在收尾', self.output.getvalue())

    def test_runtime_preparation_is_suspended_without_reading_input(self):
        with self.assertRaisesRegex(EzMcdError,'runtime_capture_suspended'):
            SamplingConsole(stdin=self.input,stderr=self.output).ready('synthetic',120)
        self.assertEqual(self.input.tell(),0)

    def test_local_cleanup_does_not_claim_it_restored_network_settings(self):
        self.console({'stage':'CLEANUP_RESULT','system_proxy_modified':False,
                      'system_proxy_unchanged':True,'errors':[]})
        self.assertIn('系统代理未修改',self.output.getvalue())
        self.assertNotIn('代理已恢复',self.output.getvalue())

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
        self.assertIn('限时停止采样', self.output.getvalue())

    def test_managed_mode_explains_automatic_lifecycle_and_manual_fallback(self):
        console=SamplingConsole(stdin=self.input,stderr=self.output,manage_app=True,balance_only=True)
        console.ready('synthetic',120)
        console({'stage':'APP_PREPARING'})
        console({'stage':'GUARD_ACTIVE'})
        console({'stage':'CLOSE_OFFICIAL_APP_NOW'})
        console({'stage':'APP_MANUAL_CLOSE_REQUIRED','next':'secret diagnostic'})
        text=self.output.getvalue()
        self.assertIn('工具正在打开官方 App',text)
        self.assertIn('工具正在请求退出麦当劳',text)
        self.assertIn('不再保证阻断',text)
        self.assertNotIn('secret diagnostic',text)

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
        self.assertIn('账号导入核验尚未完成', self.output.getvalue())
        self.assertNotIn('=true', self.output.getvalue())
        self.assertNotIn('账号已更新', self.output.getvalue())

    def test_incomplete_capture_and_unknown_event_text_do_not_leak_diagnostics(self):
        self.console({'stage': 'CAPTURE_FINISHED', 'sample_import_ready': False, 'capture_state': 'balance_rejected',
            'next_action': 'secret-session-value'})
        self.console({'stage': 'unknown', 'next': 'secret-session-value'})
        self.assertIn('不要导入部分文件', self.output.getvalue())
        self.assertNotIn('secret-session-value', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
