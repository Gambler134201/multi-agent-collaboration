"""Offline native hooks, permissions and real mock-ACP subprocess tests."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workbuddy_native_tools as native
import workbuddy_executor as wb

NODE = os.environ.get('MODEL_DESK_TEST_NODE') or shutil.which('node')


@unittest.skipUnless(NODE, 'Node is required for native hook subprocess checks')
class NativeToolsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.run = self.root / '.model-desk/runs/native-fixture'
        self.job = native.prepare(self.run, self.root, 'native-fixture', ['a.py'], [], 'edit')
        self.sid = 'fixture-parent-session'
        self.hook('SessionStart')

    def hook(self, event, tool=None, args=None, call='fixture-call', response=None, sid=None):
        value = {'hook_event_name': event, 'session_id': sid or self.sid, 'tool_name': tool,
                 'tool_input': args or {}, 'tool_use_id': call, 'cwd': str(self.root),
                 'tool_response': response}
        result = subprocess.run([NODE, str(native.GUARD), str(self.job)], input=json.dumps(value),
                                text=True, encoding='utf-8', capture_output=True)
        return result.returncode, json.loads(result.stdout)

    def test_new_profile_keeps_native_permissions_and_no_external_mcp(self):
        args = wb.command({'executable': NODE, 'cli': 'fixture'}, 'glm-test', native_job=self.job)
        self.assertIn('PowerShell', args[args.index('--tools') + 1])
        self.assertNotIn('Agent', args[args.index('--tools') + 1].split(','))
        self.assertIn('--strict-mcp-config', args)
        self.assertEqual(args[args.index('--setting-sources') + 1], '')
        self.assertEqual(args[args.index('--permission-mode') + 1], 'default')
        self.assertNotIn('--dangerously-skip-permissions', args)
        self.assertEqual(wb.command({'executable': NODE, 'cli': 'fixture'})[6], '')

    def test_parent_session_and_code_revision_are_verified(self):
        self.assertEqual(native.verify(self.job, self.sid)['probe'], 'ok')
        with self.assertRaises(ValueError):
            native.verify(self.job, 'other-session')
        data = json.loads(self.job.read_text(encoding='utf-8'))
        data['revision'] = 'stale'
        self.job.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            native.load(self.job)

    def test_write_is_scoped_and_read_is_project_bound(self):
        for tool, args in [('Read', {'file_path': 'a.py'}), ('Glob', {'pattern': '*.py'}),
                           ('Write', {'file_path': 'a.py', 'content': 'fixture'})]:
            code, result = self.hook('PreToolUse', tool, args)
            self.assertEqual(code, 0)
            self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        self.hook('PreToolUse', 'Write', {'file_path': 'outside.py', 'content': 'bad'})
        self.assertTrue(native.verify(self.job, self.sid)['writes_paused'])
        _, result = self.hook('PreToolUse', 'PowerShell', {'command': 'echo retry'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_records_auth_paths_nested_agents_and_sandbox_escape_are_denied(self):
        variants = [('Write', {'file_path': '.model-desk/task.json', 'content': 'bad'}),
                    ('Read', {'file_path': '.env'}), ('Read', {'file_path': '../foreign.py'}),
                    ('Agent', {'prompt': 'child'}), ('Skill', {'skill': 'unreviewed'}),
                    ('Artifact', {'path': 'a.py'}),
                    ('PowerShell', {'command': 'echo x', 'dangerouslyDisableSandbox': True}),
                    ('PowerShell', {'command': 'Remove-Item -Recurse .'}),
                    ('Bash', {'command': 'git push origin main'}),
                    ('WebFetch', {'url': 'http://127.0.0.1:8765/api/state'})]
        for tool, args in variants:
            with self.subTest(tool=tool, args=args):
                flag = self.run / 'workbuddy-native-denied.flag'
                if flag.exists():
                    flag.unlink()  # isolated fixtures only; never reset a live task
                _, result = self.hook('PreToolUse', tool, args)
                self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_readonly_task_never_gets_write_or_terminal_tools(self):
        run = self.root / '.model-desk/runs/readonly'
        job = native.prepare(run, self.root, 'readonly', [], [], 'chat')
        self.assertNotIn('PowerShell', native.load(job)['tools'])
        self.assertNotIn('Write', native.load(job)['tools'])

    def test_search_rewrites_exclusions_and_filters_record_metadata(self):
        _, result = self.hook('PreToolUse', 'Grep', {'pattern': 'fixture'})
        self.assertIn('.model-desk', result['hookSpecificOutput']['updatedInput']['glob'])
        _, result = self.hook('PostToolUse', 'Glob', {'pattern': '**/*'}, response={
            'content': [str(self.root / 'a.py'), str(self.job), str(self.root / '.env')]})
        self.assertEqual(result['hookSpecificOutput']['updatedToolOutput']['content'], [str(self.root / 'a.py')])

    def test_permission_matches_session_call_tool_and_exact_input(self):
        args = {'command': 'python -B -m unittest', 'description': '中文测试'}
        self.hook('PreToolUse', 'PowerShell', args)
        call = {'toolCallId': 'fixture-call', '_meta': {'codebuddy.ai/toolName': 'PowerShell'}, 'rawInput': args}
        self.assertTrue(native.permission(self.job, self.sid, call))
        for change in [{'toolCallId': 'foreign'}, {'rawInput': {'command': 'other'}},
                       {'_meta': {'codebuddy.ai/toolName': 'SandboxIntercept'}}]:
            self.assertFalse(native.permission(self.job, self.sid, {**call, **change}))

    def test_permission_approves_once_and_denial_does_not_retry(self):
        self.hook('PreToolUse', 'PowerShell', {'command': 'echo fixture'})
        client = object.__new__(wb.NativeChatClient)
        client.native_job, client.session_id, client.diagnostics, client.tainted = self.job, self.sid, [], False
        client.process = Mock(stdin=io.StringIO())
        request = {'id': 8, 'params': {'sessionId': self.sid,
            'options': [{'kind': 'allow_once', 'optionId': 'allow'}],
            'toolCall': {'toolCallId': 'fixture-call', '_meta': {'codebuddy.ai/toolName': 'PowerShell'},
                         'rawInput': {'command': 'echo fixture'}}}}
        client._native_permission(request)
        self.assertEqual(json.loads(client.process.stdin.getvalue())['result']['outcome']['optionId'], 'allow')
        request['params']['sessionId'] = 'foreign'
        with self.assertRaises(wb.ProtocolError):
            client._native_permission(request)
        self.assertTrue(client.tainted)

    def test_terminal_selftest_evidence_never_persists_output_or_credentials(self):
        args = {'command': 'python -B -m unittest'}
        self.hook('PreToolUse', 'PowerShell', args)
        self.hook('PostToolUse', 'PowerShell', args, response={
            'content': 'private output never copied', 'rawResponse': {'exitCode': 0}})
        proof = native.verify(self.job, self.sid)
        self.assertEqual(proof['events'][-1]['exit_code'], 0)
        self.assertTrue(proof['events'][-1]['ok'])
        self.assertNotIn('private output', json.dumps(proof))

    def test_mode_and_job_cannot_silently_upgrade_old_tasks(self):
        self.assertEqual(native.mode({}), native.LEGACY)
        with self.assertRaises(ValueError):
            native.mode({'workbuddy_tool_mode': 'unrestricted'})
        with self.assertRaises(FileExistsError):
            native.prepare(self.run, self.root, 'native-fixture', ['a.py'], [], 'edit')

    def test_real_mock_acp_wire_runs_hooks_permission_and_shell_exit(self):
        # Python stands in for ACP only. The hooks are the real Node guard;
        # the local command is harmless and no native host/model is contacted.
        self.job = native.prepare(self.root / '.model-desk/runs/wire', self.root, 'wire', ['a.py'], [], 'edit')
        script = self.root / 'wire.py'
        script.write_text(r'''
import json,sys,shlex,subprocess
sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
settings=json.loads(sys.argv[sys.argv.index('--settings')+1])
node=__NODE__
sid='wire-native-parent'
def hook(event,**kw):
 command=settings['hooks'][event][0]['hooks'][0]['command']
 args=shlex.split(command);args[0]=node
 result=subprocess.run(args,input=json.dumps({'hook_event_name':event,'session_id':sid,**kw}),capture_output=True,text=True,encoding='utf-8')
 assert result.returncode==0,result.stderr
 return json.loads(result.stdout)
def send(v):print(json.dumps(v),flush=True)
for line in sys.stdin:
 q=json.loads(line);method=q.get('method')
 if method=='initialize':r={'protocolVersion':1}
 elif method=='session/new':
  hook('SessionStart')
  r={'sessionId':sid,'models':{'currentModelId':'glm-test','availableModels':[{'modelId':'glm-test'}]}}
 elif method=='session/set_config_option':r={'configOptions':[{'id':'model','currentValue':'glm-test'}]}
 elif method=='session/prompt':
  args={'command':'python -B -m unittest'}
  hook('PreToolUse',tool_name='PowerShell',tool_input=args,tool_use_id='wire-shell')
  send({'jsonrpc':'2.0','id':'approval','method':'session/request_permission','params':{
   'sessionId':sid,'options':[{'kind':'allow_once','optionId':'allow'}],
   'toolCall':{'toolCallId':'wire-shell','rawInput':args,'_meta':{'codebuddy.ai/toolName':'PowerShell'}}}})
  approval=json.loads(sys.stdin.readline());assert approval['result']['outcome']['optionId']=='allow'
  result=subprocess.run([sys.executable,'-B','-c','print("fixture-command-ok")'],capture_output=True)
  hook('PostToolUse',tool_name='PowerShell',tool_input=args,tool_use_id='wire-shell',tool_response={'rawResponse':{'exitCode':result.returncode}})
  send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':sid,'update':{
   'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'fixture done'}}}})
  r={'stopReason':'end_turn'}
 else:raise RuntimeError('unexpected wire method')
 send({'jsonrpc':'2.0','id':q['id'],'result':r})
'''.replace('__NODE__', repr(str(NODE))), encoding='utf-8')
        client = wb.NativeChatClient({'executable': sys.executable, 'cli': str(script)}, self.root, self.root,
                                     isolated=True, model_id='glm-test', native_job=self.job)
        try:
            client.connect()
            result = client.chat('glm-test', 'offline fixture', {'paid_allowed': True, 'model_scope': 'all',
                'enforce_paid_budget': False, 'enforce_model_call_limit': False})
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['native_tool_proof']['events'][-1]['exit_code'], 0)
            self.assertEqual(len(native.public_calls(result['native_tool_proof'])), 1)
            self.assertEqual(native.public_calls(result['native_tool_proof'])[0]['status'], 'completed')
            self.assertFalse(result['accepted'])
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
