from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ezmcd.accounts import connect_request_text, update_runtime_request_text
from ezmcd.cli import main
from ezmcd.errors import EzMcdError
from ezmcd.models import Policy
from ezmcd.request_import import LIMIT, parse_request, read_request_file, read_request_clipboard
from ezmcd.service import Service
from ezmcd.storage import Store
from test_project_accounts import sample, KEYS
from test_project_runtime_update import evidence, CONSTANTS, NOW


def curl(request, extra=()):
    args = ["curl", "--compressed", "--insecure", "-X", request["method"], "https://" + request["host"] + request["path"]]
    for k, v in request["headers"].items():
        args += ["-H", k + ": " + v]
    return shlex.join(args + list(extra))


def raw(request, body=""):
    headers = {"Host": request["host"], **request["headers"]}
    return f'{request["method"]} {request["path"]} HTTP/1.1\r\n' + "\r\n".join(k+": "+v for k, v in headers.items()) + "\r\n\r\n" + body


class RequestParserTests(unittest.TestCase):
    def test_curl_and_raw_balance_export_preserve_only_required_headers(self):
        request = sample()["request"]
        request["headers"].update({"Cookie": "discard-private-cookie", "User-Agent": "ignored-agent"})
        for text in (curl(request), raw(request), curl(request).replace(" -H ", " \\\n  -H ")):
            got = parse_request(text, purpose="balance")
            self.assertEqual(got["headers"]["sid"], request["headers"]["sid"])
            self.assertEqual(got["path"], request["path"])
            self.assertEqual(got["body"], "")
            self.assertNotIn("discard-private-cookie", json.dumps(got))
            self.assertNotIn("ignored-agent", json.dumps(got))

    def test_long_options_and_explicit_https_port(self):
        value = 'curl --request=GET --url=https://api.mcd.cn:443/bff/member/mwallet/balance --header="Host: api.mcd.cn:443" --header="sid: synthetic"'
        self.assertEqual(parse_request(value, purpose="balance")["headers"], {"sid": "synthetic"})
        value = 'GET https://api.mcd.cn/bff/member/mwallet/balance HTTP/2\nHost: api.mcd.cn\n\n'
        self.assertEqual(parse_request(value, purpose="balance")["method"], "GET")

    def test_wrong_origin_method_endpoint_query_and_host_override_stop(self):
        for url in ('http://api.mcd.cn/bff/member/mwallet/balance',
                    'https://api.mcd.cn.example/bff/member/mwallet/balance',
                    'https://user@api.mcd.cn/bff/member/mwallet/balance',
                    'https://api.mcd.cn:444/bff/member/mwallet/balance',
                    'https://api.mcd.cn/bff/member/mwallet/balance?x=1',
                    'https://api.mcd.cn/bff/member/mwallet/balance#ignored',
                    'https://api.mcd.cn/bff/order/orders'):
            with self.subTest(url=url), self.assertRaises(EzMcdError):
                parse_request('curl '+shlex.quote(url), purpose="balance")
        request = sample()["request"]
        for changes in ({"method": "POST"}, {"method": "HEAD"}, {"host": "evil.example"}):
            with self.assertRaises(EzMcdError): parse_request(curl(request | changes), purpose="balance")
        with self.assertRaises(EzMcdError):
            parse_request(curl(request, ('-H', 'Host: evil.example')), purpose="balance")

    def test_duplicate_headers_and_controls_are_not_silently_merged(self):
        request = sample()["request"]
        for value in ("SID: second", "bad name: value", "X-test: line\nnext", "X-test: \x7f"):
            with self.assertRaises(EzMcdError):
                parse_request(curl(request, ('-H', value)), purpose="balance")
        with self.assertRaises(EzMcdError):
            parse_request(raw(request).replace('Host:', 'host: api.mcd.cn\nHost:', 1), purpose="balance")

    def test_unrecognized_options_file_references_and_extra_commands_are_not_executed(self):
        base = curl(sample()["request"])
        suffixes = [' --config /private/file', ' --next https://evil.example', ' --output /private/file',
                    ' --resolve api.mcd.cn:443:127.0.0.1', ' --proxy http://evil.example',
                    ' -H @/private/file', ' --data-binary @/private/file', ' ; touch /private/file',
                    ' | sh', ' && echo private', ' https://evil.example', ' -X GET', ' --header']
        with patch('subprocess.run') as execute:
            for suffix in suffixes:
                with self.subTest(suffix=suffix), self.assertRaises(EzMcdError):
                    parse_request(base + suffix, purpose="balance")
            execute.assert_not_called()

    def test_runtime_order_body_and_literal_shell_syntax_are_discarded(self):
        request = evidence()["request"]
        body = '$(touch /private/never-created); `private-command` {"amount":999}'
        request["headers"]["Cookie"] = "private-unused-cookie"
        with patch('subprocess.run') as execute, patch('pathlib.Path.read_text') as read:
            for text in (curl(request, ('--data-raw', body)), raw(request, body)):
                got = parse_request(text, purpose="runtime")
                self.assertEqual(got["body"], "")
                self.assertNotIn("private-", json.dumps(got))
                self.assertEqual(got["headers"]["x-mcd-sign"], request["headers"]["X-Mcd-Sign"])
            execute.assert_not_called(); read.assert_not_called()

    def test_get_body_and_ambiguous_body_encoding_are_rejected(self):
        request = sample()["request"]
        for text in (curl(request, ('--data-raw', '')), raw(request, 'private body'),
                     curl(request, ('-H', 'Content-Length: 1')), curl(request, ('-H', 'Transfer-Encoding: chunked'))):
            with self.assertRaises(EzMcdError): parse_request(text, purpose="balance")

    def test_bounds_malformed_quotes_and_mixed_requests(self):
        for text in ('', 'x'*(LIMIT+1), 'curl "', 'curl https://api.mcd.cn\x00', 'HTTP/1.1 200 OK',
                     raw(sample()["request"]) + 'GET /another HTTP/1.1\nHost: api.mcd.cn'):
            with self.assertRaises(EzMcdError): parse_request(text, purpose="balance")
        with self.assertRaises(EzMcdError): parse_request(curl(sample()["request"]), purpose="unknown")

    def test_request_files_require_private_regular_files_and_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); path=root/'request.txt'; path.write_text(curl(sample()["request"])); path.chmod(0o600)
            self.assertTrue(read_request_file(path).startswith('curl'))
            path.chmod(0o644)
            with self.assertRaisesRegex(EzMcdError,'permissions'): read_request_file(path)
            path.chmod(0o600); link=root/'link'; link.symlink_to(path)
            with self.assertRaisesRegex(EzMcdError,'permissions'): read_request_file(link)
            fifo=root/'fifo'; os.mkfifo(fifo,0o600)
            with self.assertRaisesRegex(EzMcdError,'permissions'): read_request_file(fifo)
            path.write_text('x'*(LIMIT+1))
            with self.assertRaisesRegex(EzMcdError,'too_large'): read_request_file(path)

    def test_clipboard_requires_explicit_interactive_macos_entry_and_never_executes_content(self):
        with patch('sys.platform','linux'), patch('subprocess.run') as run:
            with self.assertRaisesRegex(EzMcdError,'requires_macos'): read_request_clipboard()
            run.assert_not_called()
        with patch('sys.platform','darwin'), patch('sys.stdin.isatty',return_value=False), patch('subprocess.run') as run:
            with self.assertRaisesRegex(EzMcdError,'private_terminal'): read_request_clipboard()
            run.assert_not_called()
        private=curl(sample()["request"]).encode()
        with patch('sys.platform','darwin'), patch('sys.stdin.isatty',return_value=True), \
                patch('subprocess.run', return_value=Mock(stdout=private)) as run:
            self.assertEqual(read_request_clipboard(),private.decode())
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.args[0],['/usr/bin/pbpaste','-Prefer','txt'])
            self.assertEqual(run.call_args.kwargs['env']['LC_ALL'],'en_US.UTF-8')
            self.assertNotIn(private.decode(),str(run.call_args))
        with patch('sys.platform','darwin'), patch('sys.stdin.isatty',return_value=True), \
                patch('subprocess.run', side_effect=subprocess.TimeoutExpired('private-command',5)):
            with self.assertRaisesRegex(EzMcdError,'^clipboard_request_unreadable$'): read_request_clipboard()


class RequestConnectionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name)); self.addCleanup(patch.stopall)
        patch('ezmcd.accounts.constants',return_value=KEYS).start()
        self.wallet=patch('ezmcd.provider.McdProvider.wallet',return_value={'balance_cents':100}).start()

    def test_request_only_connect_revalidates_live_and_saves_no_export_or_cookie(self):
        request=sample()['request']; request['headers']['Cookie']='private-cookie'
        result=connect_request_text(self.store,'mine',curl(request),'unused')
        self.assertEqual(result['session_status'],'verified_now')
        self.assertFalse(result['policy']['allow_checkout'])
        self.wallet.assert_called_once()
        record=self.store.account('mine'); saved=json.dumps(record)
        for value in ('private-cookie',request['headers']['authorization'],request['headers']['x-hmac-digest'],'curl '):
            self.assertNotIn(value,saved)
        self.assertEqual(len(list(self.store.root.rglob('*.json'))),1)
        self.assertNotIn(request['headers']['sid'],json.dumps(result))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0],0)

    def test_bad_signature_never_calls_network_and_rejected_session_is_not_saved(self):
        request=sample()['request']; request['headers']['sid']='wrong-session'
        with self.assertRaisesRegex(EzMcdError,'invalid_official_sample_signature'):
            connect_request_text(self.store,'mine',curl(request),'unused')
        self.wallet.assert_not_called(); self.assertEqual(self.store.accounts(),[])
        self.wallet.side_effect=EzMcdError('session_expired_reconnect_locally')
        with self.assertRaisesRegex(EzMcdError,'session_expired'):
            connect_request_text(self.store,'mine',curl(sample()['request']),'unused')
        self.assertEqual(self.store.accounts(),[])

    def test_reconnect_preserves_policy_purchase_log_refresh_and_same_session_runtime(self):
        text=curl(sample()['request'])
        connect_request_text(self.store,'mine',text,'unused')
        Service(self.store).set_policy('mine',Policy(allow_checkout=True,max_order_cents=1700))
        self.store.claim('prior-attempt','consume')
        def enrich(record):
            record['runtime']={'observed_at':NOW-20,'device_hex':'A'*32,'environment_hex':'b'*16}
            record['profile']['fields']['refreshToken']='keep-private-refresh'
            return record
        self.store.modify_account('mine',enrich)
        before=self.store.account('mine')
        connect_request_text(self.store,'mine',text,'unused',update=True)
        after=self.store.account('mine')
        self.assertEqual(after['policy'],before['policy']); self.assertEqual(after['runtime'],before['runtime'])
        self.assertEqual(after['profile']['fields'],before['profile']['fields'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM writes').fetchone()[0],1)

    def test_other_member_cannot_replace_alias_or_reset_ledger(self):
        connect_request_text(self.store,'mine',curl(sample()['request']),'unused')
        before=self.store.account('mine'); request=sample()['request']; request['headers']['meddyid']='other-member'
        with self.assertRaisesRegex(EzMcdError,'cannot_replace_alias'):
            connect_request_text(self.store,'mine',curl(request),'unused',update=True)
        self.assertEqual(self.store.account('mine'),before)

    def test_runtime_request_import_verifies_same_session_and_discards_order(self):
        connect_request_text(self.store,'mine',curl(sample()['request']),'unused')
        before=self.store.account('mine'); self.wallet.reset_mock()
        with patch('ezmcd.accounts.constants',return_value=CONSTANTS), patch('ezmcd.accounts.time.time',return_value=NOW):
            result=update_runtime_request_text(self.store,'mine',curl(evidence()['request'],('--data-raw','private-order-never-replay')))
            self.assertTrue(result['runtime_updated']); self.assertFalse(result['order_created'])
            after=self.store.account('mine')
            self.assertEqual(after['profile'],before['profile']); self.assertEqual(after['runtime']['observed_at'],NOW-10)
            self.assertNotIn('private-order',json.dumps(after))
            self.wallet.assert_called_once()
            changed=evidence()['request']; changed['headers']['SID']='other-member'
            with self.assertRaisesRegex(EzMcdError,'account_mismatch'):
                update_runtime_request_text(self.store,'mine',raw(changed,'private-order'))
            self.assertEqual(self.store.account('mine'),after)

    def test_cli_request_file_uses_same_validation_and_prints_only_public_result(self):
        path=self.store.root/'request.txt'; path.write_text(raw(sample()['request'])); path.chmod(0o600)
        args=['ezmcd','--data-dir',str(self.store.root),'account','connect','mine','--request-file',str(path),'--binary','unused']
        output=io.StringIO()
        with patch('sys.argv',args),patch('sys.stdout',output):self.assertEqual(main(),0)
        result=json.loads(output.getvalue()); self.assertEqual(result['account'],'mine')
        self.assertNotIn('synthetic-session',output.getvalue())

    def test_cli_clipboard_reads_only_with_explicit_flag_and_explains_cleanup(self):
        args=['ezmcd','--data-dir',str(self.store.root),'account','connect','mine','--clipboard','--binary','unused']
        with patch('sys.argv',args),patch('sys.stdout',new_callable=io.StringIO) as output, \
                patch('ezmcd.request_import.read_request_clipboard',return_value=curl(sample()['request'])) as read:
            self.assertEqual(main(),0); read.assert_called_once()
            self.assertIn('clipboard_clear_reminder',json.loads(output.getvalue()))
        with patch('sys.argv',args+['--runtime-sample','unused']),patch('sys.stderr',new_callable=io.StringIO) as error, \
                patch('ezmcd.request_import.read_request_clipboard') as read:
            self.assertEqual(main(),2); read.assert_not_called()
            self.assertIn('import_runtime_separately',error.getvalue())
        with patch('sys.argv',['ezmcd','--data-dir',str(self.store.root),'account','list']), \
                patch('sys.stdout',new_callable=io.StringIO), patch('ezmcd.request_import.read_request_clipboard') as read:
            self.assertEqual(main(),0); read.assert_not_called()

    def test_cli_runtime_file_never_replays_body_or_resets_login_policy(self):
        connect_request_text(self.store,'mine',curl(sample()['request']),'unused')
        before=self.store.account('mine')
        path=self.store.root/'runtime.txt'; path.write_text(raw(evidence()['request'],'private-order-body')); path.chmod(0o600)
        args=['ezmcd','--data-dir',str(self.store.root),'account','runtime','mine','--request-file',str(path)]
        with patch('sys.argv',args),patch('sys.stdout',new_callable=io.StringIO) as output, \
                patch('ezmcd.accounts.constants',return_value=CONSTANTS),patch('ezmcd.accounts.time.time',return_value=NOW):
            self.assertEqual(main(),0)
            result=json.loads(output.getvalue()); self.assertTrue(result['runtime_updated'])
            self.assertFalse(result['order_created']); self.assertNotIn('private-order-body',output.getvalue())
        self.assertEqual(self.store.account('mine')['profile'],before['profile'])
        self.assertEqual(self.store.account('mine')['policy'],before['policy'])

    def test_cli_errors_never_echo_copied_text_or_run_its_command(self):
        args=['ezmcd','--data-dir',str(self.store.root),'account','connect','mine','--clipboard','--binary','unused']
        with patch('sys.argv',args),patch('sys.stderr',new_callable=io.StringIO) as error, \
                patch('ezmcd.request_import.read_request_clipboard',return_value='curl --config private-secret'), \
                patch('subprocess.run') as run:
            self.assertEqual(main(),2); run.assert_not_called()
            self.assertNotIn('private-secret',error.getvalue())


if __name__ == '__main__':
    unittest.main()
