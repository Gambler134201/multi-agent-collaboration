import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workbuddy_tools as tools
import workbuddy_executor as wb


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run = self.root / '.model-desk/runs/fixture'
        self.job = tools.prepare(self.run, self.root, 'fixture', ['a.txt'], ['context.txt'])
        self.files = tools.FileTools(self.job)

    def readiness_client(self):
        client = object.__new__(wb.NativeChatClient)
        client.tool_job, client.session_id = self.job, 'fixture-session'
        client.project, client.models = str(self.root), [{'model_id': 'fixture-model'}]
        client.prompt_sent, client.diagnostics = False, []
        client.cancel_check = lambda: False
        client.request = Mock(side_effect=lambda method, params, **kwargs:
            {'configOptions': [{'id': 'model', 'currentValue': 'fixture-model'}]}
            if method == 'session/set_config_option' else {'stopReason': 'end_turn'})
        return client

    def test_delayed_tools_list_finishes_before_the_only_prompt(self):
        client = self.readiness_client()
        timer = threading.Timer(.1, self.files.list_tools)
        timer.start()
        try:
            result = client.chat('fixture-model', 'fixture', {'paid_allowed': True, 'model_scope': 'all',
                'enforce_paid_budget': False, 'enforce_model_call_limit': False})
        finally:
            timer.join()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([c.args[0] for c in client.request.call_args_list],
                         ['session/set_config_option', 'session/prompt'])
        self.assertTrue(tools.verify(self.job)['tools_listed'])

    def test_not_listed_timeout_sends_no_prompt(self):
        self.files.save_proof()
        client = self.readiness_client()
        with self.assertRaisesRegex(wb.ProtocolError, '未发送提示词'):
            client.chat('fixture-model', 'fixture', {'paid_allowed': True, 'model_scope': 'all',
                'enforce_paid_budget': False, 'enforce_model_call_limit': False}, timeout=.05)
        client.request.assert_not_called()
        self.assertFalse(client.prompt_sent)

    def test_readiness_never_waits_past_foreign_or_corrupt_evidence(self):
        self.files.list_tools()
        p=self.run/'workbuddy-tools-proof.json'
        proof=json.loads(p.read_text(encoding='utf-8'));proof['task_id']='foreign'
        p.write_text(json.dumps(proof),encoding='utf-8')
        client=self.readiness_client()
        with self.assertRaises(ValueError):client.wait_file_tools(.1)
        client.request.assert_not_called()

    def test_readiness_observes_cancellation_before_native_request(self):
        client=self.readiness_client();client.cancel_check=lambda:True
        with self.assertRaises(wb.Cancelled):client.wait_file_tools(.1)
        client.request.assert_not_called()

    def test_utf8_write_read_and_unique_edit(self):
        self.files.call('write_file', {'path': 'a.txt', 'content': '你好\r\nline'})
        self.files.call('edit_file', {'path': 'a.txt', 'old_text': 'line', 'new_text': 'edited'})
        value = self.files.call('read_file', {'path': 'a.txt'})
        self.assertEqual(value['content'], '你好\r\nedited')
        self.assertEqual((self.root / 'a.txt').read_bytes(), value['content'].encode('utf-8'))
        self.assertEqual(len(self.files.events), 3)

    def test_read_missing_target_does_not_pause_creation(self):
        self.assertFalse(self.files.call('read_file', {'path': 'a.txt'})['exists'])
        self.files.call('write_file', {'path': 'a.txt', 'content': 'new'})

    def test_scope_refusal_pauses_all_mutations_but_allows_reads(self):
        (self.root / 'context.txt').write_text('read only', encoding='utf-8')
        with self.assertRaises(tools.ToolRefused):
            self.files.call('write_file', {'path': 'context.txt', 'content': 'bad'})
        with self.assertRaises(tools.ToolRefused):
            self.files.call('write_file', {'path': 'a.txt', 'content': 'evade'})
        self.assertEqual(self.files.call('read_file', {'path': 'context.txt'})['content'], 'read only')
        self.assertFalse((self.root / 'a.txt').exists())

    def test_no_directory_traversal_devices_ads_record_or_git_aliases(self):
        for path in ['../outside', '/a', 'C:\\a', '.model-desk/task.json',
                     '.MODEL-DESK/foo', '.git/config', 'NUL', 'a.txt:stream',
                     'x/../a.txt', 'x./a.txt', 'x /a.txt', 'a\x00.txt']:
            with self.subTest(path=path), self.assertRaises(tools.ToolRefused):
                tools.relative_path(path)

    def test_record_directory_is_never_a_prepared_target(self):
        with self.assertRaises(tools.ToolRefused):
            tools.prepare(self.root / '.model-desk/runs/other', self.root, 'other', ['.model-desk/records.db'])

    def test_ambiguous_edits_fail_without_mutating(self):
        path = self.root / 'a.txt'
        path.write_text('xx', encoding='utf-8')
        with self.assertRaises(tools.ToolRefused):
            self.files.call('edit_file', {'path': 'a.txt', 'old_text': 'x', 'new_text': 'bad'})
        self.assertEqual(path.read_text(), 'xx')

    def test_file_size_limit_does_not_truncate_existing_target(self):
        path = self.root / 'a.txt'
        path.write_text('xx', encoding='utf-8')
        with self.assertRaises(tools.ToolRefused):
            self.files.call('write_file', {'path': 'a.txt', 'content': 'x' * (tools.MAX_BYTES + 1)})
        self.assertEqual(path.read_text(), 'xx')

    def test_mcp_restart_preserves_refusal_instead_of_resetting_guard(self):
        with self.assertRaises(tools.ToolRefused):
            self.files.call('write_file', {'path': 'outside.txt', 'content': 'bad'})
        restarted = tools.FileTools(self.job)
        restarted.list_tools()
        with self.assertRaises(tools.ToolRefused):
            restarted.call('write_file', {'path': 'a.txt', 'content': 'evade'})
        self.assertFalse((self.root / 'a.txt').exists())

    def test_native_end_turn_does_not_turn_tool_refusal_into_completion(self):
        self.files.list_tools()
        client = object.__new__(wb.NativeChatClient)
        client.tool_job, client.session_id = self.job, 'test-session'
        client.project, client.models = str(self.root), [{'model_id': 'deepseek-v4.1-flash'}]
        client.prompt_sent = False
        client.diagnostics = []
        def request(method, params, **kwargs):
            if method == 'session/set_config_option':
                return {'configOptions': [{'id': 'model', 'currentValue': 'deepseek-v4.1-flash'}]}
            with self.assertRaises(tools.ToolRefused):
                self.files.call('write_file', {'path': 'outside.txt', 'content': 'bad'})
            return {'stopReason': 'end_turn'}
        client.request = request
        result = client.chat('deepseek-v4.1-flash', 'test', {'paid_allowed': True, 'model_scope': 'all',
            'enforce_paid_budget': False, 'enforce_model_call_limit': False})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['execution_error']['category'], 'file_tool_refused')
        self.assertFalse(result['accepted'])

    def test_links_cannot_alias_workbench_records(self):
        original = self.root / '.model-desk/private.txt'
        original.write_text('original', encoding='utf-8')
        os.link(original, self.root / 'a.txt')
        with self.assertRaises(tools.ToolRefused):
            self.files.call('write_file', {'path': 'a.txt', 'content': 'bad'})
        self.assertEqual(original.read_text(), 'original')

    def test_changed_job_and_cross_project_proof_are_refused(self):
        self.files.list_tools()
        self.assertTrue(tools.verify(self.job)['tools_listed'])
        proof_path = self.run / 'workbuddy-tools-proof.json'
        proof = json.loads(proof_path.read_text(encoding='utf-8'))
        proof['project'] = str(self.root.parent)
        proof_path.write_text(json.dumps(proof), encoding='utf-8')
        with self.assertRaises(ValueError):
            tools.verify(self.job)
        self.job.write_text(self.job.read_text(encoding='utf-8') + ' ', encoding='utf-8')
        with self.assertRaises(tools.ToolRefused):
            self.files.call('write_file', {'path': 'a.txt', 'content': 'bad'})

    def test_job_is_fresh_and_cannot_rebind_an_existing_execution(self):
        with self.assertRaises(FileExistsError):
            tools.prepare(self.run, self.root, 'fixture', ['context.txt'])
        with self.assertRaises(ValueError):
            tools.prepare(self.root / 'other-run', self.root, 'other-run', ['a.txt'])
        with self.assertRaises(ValueError):
            tools.prepare('relative-run', '.', 'relative-run', ['a.txt'])

    def test_terminal_network_and_nested_tools_are_not_exposed(self):
        self.assertEqual(self.files.list_tools(), ['read_file', 'write_file', 'edit_file'])
        for name in ['run_command', 'bash', 'request', 'delegate']:
            with self.subTest(name=name), self.assertRaises(tools.ToolRefused):
                self.files.call(name, {})

    def permission_client(self):
        self.files.list_tools()
        client = object.__new__(wb.NativeChatClient)
        client.tool_job, client.session_id, client.tainted = self.job, 'own-session', False
        client.diagnostics = []
        client.process = Mock(stdin=io.StringIO())
        return client

    def request(self, *, tool='mcp__modeldesk-files__write_file', path='a.txt', session='own-session'):
        return {'jsonrpc': '2.0', 'id': 'native-approval-1', 'method': 'session/request_permission',
                'params': {'sessionId': session, 'options': [{'kind': 'allow_once', 'optionId': 'allow'},
                    {'kind': 'allow_always', 'optionId': 'allow_always'}],
                    'toolCall': {'_meta': {'codebuddy.ai/toolName': tool},
                                 'rawInput': {'path': path, 'content': 'OK'}}}}

    def test_acp_permission_only_approves_once_for_own_scoped_mcp_tool(self):
        client = self.permission_client()
        client._file_permission(self.request())
        result = json.loads(client.process.stdin.getvalue())['result']['outcome']
        self.assertEqual(result, {'outcome': 'selected', 'optionId': 'allow'})
        self.assertFalse((self.root / 'a.txt').exists())

    def test_acp_refuses_foreign_tool_session_path_and_malformed_requests(self):
        variants = [self.request(tool='Bash'), self.request(tool='mcp__other__write_file'),
                    self.request(path='.model-desk/records.db'), self.request(path='outside.txt'),
                    self.request(session='foreign'), {'id': 9, 'params': {'sessionId': 'own-session', 'toolCall': []}}]
        for request in variants:
            client = self.permission_client()
            with self.subTest(request=request), self.assertRaises(wb.ProtocolError):
                client._file_permission(request)
            self.assertEqual(json.loads(client.process.stdin.getvalue())['result']['outcome'], {'outcome': 'cancelled'})
            self.assertTrue(client.tainted)

    def test_acp_requires_loaded_unmodified_tools_and_stops_after_refusal(self):
        client = self.permission_client()
        with self.assertRaises(tools.ToolRefused):
            self.files.call('write_file', {'path': 'outside.txt', 'content': 'bad'})
        with self.assertRaises(wb.ProtocolError):
            client._file_permission(self.request())

    def test_acp_does_not_approve_without_actual_tools_list_handshake(self):
        client = self.permission_client()
        (self.run / 'workbuddy-tools-proof.json').unlink()
        with self.assertRaises(wb.ProtocolError):
            client._file_permission(self.request())

    def test_always_or_sandbox_permissions_cannot_replace_native_mcp_once(self):
        for options in [[{'kind': 'allow_always', 'optionId': 'allow_always'}],
                        [{'kind': 'allow_once', 'optionId': 'allow_always'}],
                        [{'kind': 'allow', 'optionId': 'allow_once'}],
                        [{'kind': 'allow_once', 'optionId': 'allow'}] * 2]:
            client = self.permission_client()
            request = self.request()
            request['params']['options'] = options
            with self.subTest(options=options), self.assertRaises(wb.ProtocolError):
                client._file_permission(request)

    def test_only_scoped_file_server_disables_deferral_without_changing_native_defaults(self):
        for job in (None, self.job):
            process = Mock(stdout=io.StringIO(), stderr=io.StringIO())
            spawn = Mock(return_value=process)
            client = wb.NativeChatClient({'executable': sys.executable, 'cli': 'fixture'},
                self.root, self.root, isolated=True, popen=spawn, tool_job=job)
            try:
                args, kwargs = spawn.call_args
                self.assertEqual(args[0][args[0].index('--tools') + 1], '')
                self.assertIsNone(kwargs['env'].get('CODEBUDDY_DEFER_TOOL_LOADING'))
                self.assertEqual(len(client.mcp_servers), 1 if job else 0)
                if job:
                    self.assertEqual(client.mcp_servers[0]['_meta'], {'defer_loading': False})
            finally:
                client.close()

    def test_real_stdio_mcp_handshake_scope_and_refusal(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            params = StdioServerParameters(command=sys.executable,
                args=[str(Path(tools.__file__).resolve()), '--job', str(self.job)],
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    self.assertEqual({t.name for t in listed.tools}, set(tools.NAMES))
                    self.assertTrue(tools.verify(self.job)['tools_listed'])
                    result = await client.call_tool('write_file', {'path': 'a.txt', 'content': 'MCP_OK'})
                    self.assertFalse(result.isError)
                    result = await client.call_tool('read_file', {'path': 'a.txt'})
                    self.assertEqual(json.loads(result.content[0].text)['content'], 'MCP_OK')
                    denied = await client.call_tool('write_file', {'path': '.model-desk/private.txt', 'content': 'bad'})
                    self.assertTrue(denied.isError)
                    again = await client.call_tool('edit_file', {'path': 'a.txt', 'old_text': 'MCP_OK', 'new_text': 'bad'})
                    self.assertTrue(again.isError)
        asyncio.run(run())
        self.assertEqual((self.root / 'a.txt').read_text(), 'MCP_OK')
        self.assertFalse((self.root / '.model-desk/private.txt').exists())

    def test_full_acp_wire_accepts_native_option_id_and_executes_scoped_file_call(self):
        # Native 5.8.2 general MCP approval uses kind=allow_once, optionId=allow.
        # This child simulates ACP only; its file guard is the actual server code.
        script = self.root / 'native_wire_fixture.py'
        script.write_text(r'''
import json,sys
from pathlib import Path
# Match native ACP UTF-8 even when Windows Python pipes default to ANSI.
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
def send(value):
 print(json.dumps(value),flush=True)
for line in sys.stdin:
 q=json.loads(line);method=q.get('method')
 if method=='initialize':r={'protocolVersion':1}
 elif method=='session/new':
  config=q['params']['mcpServers'][0]
  sys.path.insert(0,str(Path(config['args'][0]).parent))
  from workbuddy_tools import FileTools
  files=FileTools(config['args'][-1]);files.list_tools()
  r={'sessionId':'wire-session','models':{'currentModelId':'deepseek-v4.1-flash',
    'availableModels':[{'modelId':'deepseek-v4.1-flash'}]}}
 elif method=='session/set_config_option':
  r={'configOptions':[{'id':'model','currentValue':'deepseek-v4.1-flash'}]}
 elif method=='session/prompt':
  send({'jsonrpc':'2.0','id':'native-approval','method':'session/request_permission',
   'params':{'sessionId':'wire-session','options':[
    {'kind':'allow_always','name':'Always Allow','optionId':'allow_always'},
    {'kind':'allow_once','name':'Allow','optionId':'allow'},
    {'kind':'reject_once','name':'Reject','optionId':'reject'}],
    'toolCall':{'_meta':{'codebuddy.ai/toolName':'mcp__modeldesk-files__write_file'},
     'rawInput':{'path':'a.txt','content':'WIRE_OK'}}}})
  approval=json.loads(sys.stdin.readline())
  assert approval['id']=='native-approval'
  assert approval['result']['outcome']=={'outcome':'selected','optionId':'allow'}
  files.call('write_file',{'path':'a.txt','content':'WIRE_OK'})
  send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'wire-session',
   'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'done'}}}})
  r={'stopReason':'end_turn'}
 else:sys.exit(2)
 send({'jsonrpc':'2.0','id':q['id'],'result':r})
''', encoding='utf-8')
        client = wb.NativeChatClient({'executable': sys.executable, 'cli': str(script)},
            self.root, self.root, isolated=True, model_id='deepseek-v4.1-flash', tool_job=self.job)
        try:
            client.connect()
            result = client.chat('deepseek-v4.1-flash', 'fixture', {'paid_allowed': True,
                'model_scope': 'all', 'enforce_paid_budget': False, 'enforce_model_call_limit': False})
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['adapter_diagnostics'][-1]['option_id'], 'allow')
            self.assertEqual((self.root / 'a.txt').read_bytes(), b'WIRE_OK')
            self.assertFalse(result['accepted'])
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
