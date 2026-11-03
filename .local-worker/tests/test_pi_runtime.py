"""Installed Pi + production worker/relay against a local scripted HTTP provider.

No live model calls, user Pi configuration, real keys or real project records.
"""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import model_registry as registry
import budget_relay
import context_manager
import pi_worker
import workflow_settings


@unittest.skipUnless(pi_worker.availability()['ready'], 'requires installed Pi 1.3.0')
class PiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='md-pi-runtime-')
        self.box = Path(self.temp.name)
        self.project = self.box / 'project'
        self.project.mkdir()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = self.box / 'controller'
        registry.HERE.mkdir()
        registry.REGISTRY, registry.RUNS = registry.HERE / 'models.json', registry.HERE / 'runs'
        self.calls, self.steps = [], []
        owner = self
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                index = len(owner.calls)
                owner.calls.append(body)
                step = owner.steps[index] if index < len(owner.steps) else None
                if step:
                    delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'tool-' + str(index),
                        'type': 'function', 'function': {'name': step[0], 'arguments': json.dumps(step[1])}}]}
                else:
                    delta = {'role': 'assistant', 'content': 'PI_TEST_FINISHED 中文\u2028LF\u2029ONLY'}
                chunks = [{'id': 'pi-fixture', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
                          {'id': 'pi-fixture', 'choices': [{'index': 0, 'delta': {},
                            'finish_reason': 'tool_calls' if step else 'stop'}],
                           'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15,
                                     'prompt_tokens_details': {'cached_tokens': 0}}}]
                data = ''.join('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data.encode())))
                self.end_headers()
                self.wfile.write(data.encode())
        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                if not isinstance(sys.exception(), ConnectionResetError):
                    super().handle_error(request, client_address)
        self.server = QuietServer(('127.0.0.1', 0), Provider)
        self.quiet_relay = patch.object(budget_relay, 'ThreadingHTTPServer', QuietServer)
        self.quiet_relay.start()
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.model = {'id': 'pi-fixture', 'name': 'offline Pi fixture', 'model': 'pi-fixture',
                      'provider': 'local', 'api_base': f'http://127.0.0.1:{self.server.server_port}/v1',
                      'api_key': 'mock-pi-test-key', 'bypass_proxy': True,
                      'max_input_tokens': 32768, 'max_output_tokens': 1024, 'thinking_level': 'default'}
        workflow_settings.save_settings({'enabled': False, 'paid_allowed': False, 'execution_agent': 'dsh',
                                         'enforce_paid_budget': False, 'enforce_model_call_limit': False})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.quiet_relay.stop()
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def run_pi(self, kind='edit', name='pi-runtime', parent=None, access='full', write_scope=None, readonly_files=(), task_policy=None, expected_status='completed'):
        run = self.project / '.model-desk' / 'runs' / name
        run.mkdir(parents=True)
        (run / 'prompt.txt').write_text('Execute the scripted fixture task.', encoding='utf-8')
        state = dict(id=name, engine='pi', kind=kind, execution_access_mode=access, project=str(self.project), files=['source.txt'] if kind == 'edit' else [],
                     model_id=self.model['id'], model=self.model['model'], timeout=60, route={'mode': 'manual'})
        if write_scope is not None:
            state.update(task_policy={'schema':1,'write_scope':write_scope},readonly_files=list(readonly_files))
        if task_policy is not None:
            state.update(task_policy=task_policy,readonly_files=list(readonly_files))
        if parent:
            for key in ('pi_session_id', 'pi_session_file', 'connection_fingerprint'):
                state[key] = parent[key]
        registry.write_json(run / 'task.json', state)
        with patch('thinking_config.prepare_for_task', return_value=({}, {'status': 'fixture'})), \
             patch('thinking_config.validate'), contextlib.redirect_stdout(io.StringIO()):
            exit_code = pi_worker.run_task(run, state, dict(self.model))
        record = registry.read_task(run)
        self.assertEqual(exit_code, 0 if expected_status=='completed' else 1, (record.get('error'), (run / 'pi.log').read_text(encoding='utf-8')[-6000:]))
        self.assertEqual(record['status'], expected_status)
        self.assertTrue(record['record_protection'])
        self.assertEqual(record['engine'], 'pi')
        if expected_status=='failed':
            import execution_result
            self.assertEqual(execution_result.project(record)['failure']['kind'],'permission_denied')
            self.assertEqual(record['record_proof']['guard_denial']['code'],'TOOL_PERMISSION_DENIED')
        return run, record

    def test_real_native_tool_errors_are_correctable_without_guard_refusal(self):
        self.steps=[('read',{'path':'missing.txt'}),
                    ('powershell',{'command':'exit 3'}),
                    ('write',{'path':'source.txt','content':'corrected'})]
        _,record=self.run_pi()
        self.assertEqual((self.project/'source.txt').read_text(),'corrected')
        self.assertNotIn('guard_denial',record['record_proof'])
        self.assertIn('missing.txt',self.tool_results()['tool-0'])

    def test_v3_real_native_filter_runs_before_a_direct_protected_target_is_denied(self):
        command="$d='.'; Get-ChildItem -Force -Exclude '.git' $d | Where-Object { $_.Name -ne '.model-desk' } | Select-Object -ExpandProperty Name"
        self.steps=[('powershell',{'command':command}),
                    ('write',{'path':'source.txt','content':'AFTER-FILTER'}),
                    ('powershell',{'command':'Set-Content .model-desk/forged.txt FORBIDDEN'})]
        _,record=self.run_pi(task_policy={'schema':3,'write_scope':'workspace','home':str(self.box/'home'),
                                         'deny_paths':[]},expected_status='failed')
        self.assertNotIn('已拒绝',self.tool_results()['tool-0'])
        self.assertEqual((self.project/'source.txt').read_text(),'AFTER-FILTER')
        self.assertFalse((self.project/'.model-desk/forged.txt').exists())
        self.assertEqual(record['record_proof']['guard_denial']['rule_id'],'protected_file')
        self.assertEqual(record['task_policy']['schema'],3)

    def test_project_permission_blocks_real_pi_outside_write(self):
        target = self.box / 'outside.txt'
        self.steps = [('write', {'path': str(target), 'content': 'forbidden'})]
        _, record = self.run_pi(access='project',expected_status='failed')
        self.assertFalse(target.exists())
        self.assertIn('项目外', self.tool_results()['tool-0'])
        self.assertEqual(record['sandbox_mode'], 'project-argument-guard')

    def test_task_policy_allows_real_native_helper_and_preserves_readonly(self):
        (self.project/'readonly.txt').write_text('READONLY',encoding='utf-8')
        self.steps=[('write',{'path':'helper.py','content':'assert True\n'}),
                    ('read',{'path':'helper.py'}),
                    ('write',{'path':'readonly.txt','content':'forbidden'})]
        _,record=self.run_pi(access='project',write_scope='workspace',readonly_files=['readonly.txt'],expected_status='failed')
        self.assertEqual((self.project/'helper.py').read_text(),'assert True\n')
        self.assertIn('assert True',self.tool_results()['tool-1'])
        self.assertIn('只读上下文',self.tool_results()['tool-2'])
        self.assertEqual((self.project/'readonly.txt').read_text(),'READONLY')
        self.assertEqual(record['task_policy']['write_scope'],'workspace')

    def test_explicit_files_policy_rejects_native_undeclared_file(self):
        self.steps=[('write',{'path':'helper.py','content':'forbidden'})]
        self.run_pi(access='full',write_scope='declared_files',expected_status='failed')
        self.assertFalse((self.project/'helper.py').exists())
        self.assertIn('可写范围',self.tool_results()['tool-0'])

    def test_declared_private_paths_with_real_pi_and_scripted_provider(self):
        home=self.box/'synthetic-home'
        private=home/'.codex'
        private.mkdir(parents=True)
        sentinel=private/'auth.json'
        sentinel.write_text('SYNTHETIC',encoding='utf-8')
        self.steps=[('write',{'path':'tokens.json','content':'BUSINESS'}),
                    ('read',{'path':'tokens.json'}),
                    ('write',{'path':str(sentinel),'content':'forbidden'})]
        _,record=self.run_pi(task_policy={'schema':2,'write_scope':'workspace','home':str(home),
                                         'deny_paths':[str(private)]},expected_status='failed')
        self.assertEqual((self.project/'tokens.json').read_text(),'BUSINESS')
        self.assertIn('BUSINESS',self.tool_results()['tool-1'])
        self.assertIn('受保护路径',self.tool_results()['tool-2'])
        self.assertEqual(sentinel.read_text(),'SYNTHETIC')
        self.assertEqual(record['task_policy']['schema'],2)

    def test_project_permission_does_not_treat_literal_output_as_outside_access(self):
        outside = str(self.box / 'outside.txt')
        self.steps = [('powershell', {'command': 'Write-Output "' + outside + '"'}),
                      ('powershell', {'command': 'Write-Output "git commit; rm -rf .model-desk"'}),
                      ('write', {'path': 'source.txt', 'content': 'still writable'})]
        self.run_pi(access='project')
        self.assertIn(outside, self.tool_results()['tool-0'])
        self.assertIn('git commit', self.tool_results()['tool-1'])
        self.assertEqual((self.project / 'source.txt').read_text(), 'still writable')
        self.assertFalse((self.box / 'outside.txt').exists())

    def test_full_permission_allows_real_pi_ordinary_outside_write(self):
        target = self.box / 'outside.txt'
        self.steps = [('write', {'path': str(target), 'content': 'allowed'})]
        _, record = self.run_pi(access='full')
        self.assertEqual(target.read_text(encoding='utf-8'), 'allowed')
        self.assertEqual(record['sandbox_mode'], 'danger-full-access')

    def tool_results(self):
        return {message['tool_call_id']: str(message.get('content'))
                for call in self.calls for message in call.get('messages', [])
                if message.get('role') == 'tool'}

    def test_tools_record_guard_and_relay_usage(self):
        records = self.project / '.model-desk'
        records.mkdir()
        sentinel = records / 'sentinel.txt'
        sentinel.write_text('original', encoding='utf-8')
        self.steps = [('write', {'path': 'source.txt', 'content': 'normal source'}),
                      ('write', {'path': '.model-desk/sentinel.txt', 'content': 'bad'}),
                      ('edit', {'path': '.model-desk/sentinel.txt', 'oldText': 'original', 'newText': 'bad'}),
                      ('powershell', {'command': "Set-Content source.txt bypass"}),
                      ('bash', {'command': 'echo bypass > bypass.txt'}),
                      ('write', {'path': 'bypass.txt', 'content': 'bypass'}),
                      ('read', {'path': 'source.txt'})]
        run, record = self.run_pi(expected_status='failed')
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'original')
        self.assertEqual((self.project / 'source.txt').read_text(encoding='utf-8'), 'normal source')
        for key in ('tool-1', 'tool-2', 'tool-3', 'tool-4', 'tool-5'):
            self.assertIn('该目录由工作台管理', self.tool_results()[key])
        self.assertFalse((self.project / 'bypass.txt').exists())
        self.assertIn('normal source', self.tool_results()['tool-6'])
        self.assertIn('source.txt', record['changed_files'])
        self.assertEqual(record['model_calls'], 8)
        self.assertEqual(record['usage']['total_tokens'], 120)
        self.assertIn('中文\u2028LF\u2029ONLY', record['answer'])
        self.assertNotIn('mock-pi-test-key', (run / 'pi.log').read_text(encoding='utf-8'))
        self.assertEqual(record['subagent_decision']['decision'], 'deny')
        # Check the real installed Pi's outgoing request, not only the builder.
        contents = [message.get('content', '') for message in self.calls[0]['messages']]
        texts = [content if isinstance(content, str) else ''.join(
            part.get('text', '') for part in content if isinstance(part, dict)) for content in contents]
        self.assertTrue(any(context_manager.SELF_TEST_NOTE in text for text in texts))
        self.assertTrue(any('本轮写入与终端工具暂停' in text for text in texts))

    def test_cache_cleanup_and_formatting_without_record_refusal(self):
        cache = self.project / 'cache'
        cache.mkdir()
        (cache / 'discard.txt').write_text('cache', encoding='utf-8')
        self.steps = [('powershell', {'command': 'Get-ChildItem . | Format-List'}),
                      ('powershell', {'command': 'Set-Location -LiteralPath "' + str(self.project) + '"; Remove-Item -LiteralPath cache -Recurse -Force; Get-ChildItem . | Select-Object Name'}),
                      ('write', {'path': 'source.txt', 'content': 'still writable'})]
        self.run_pi()
        self.assertFalse(cache.exists())
        self.assertEqual((self.project / 'source.txt').read_text(encoding='utf-8'), 'still writable')
        self.assertFalse(any('已拒绝' in result for result in self.tool_results().values()))

    def test_continuation_keeps_session_and_context(self):
        _, parent = self.run_pi(kind='chat', name='pi-parent')
        _, child = self.run_pi(kind='chat', name='pi-child', parent=parent)
        self.assertEqual(child['pi_session_id'], parent['pi_session_id'])
        self.assertEqual(child['pi_session_file'], parent['pi_session_file'])
        self.assertTrue(any(m.get('role') == 'assistant' for m in self.calls[-1]['messages']))

    def test_read_only_task_cannot_write_or_execute_terminal_tools(self):
        self.steps = [('write', {'path': 'source.txt', 'content': 'bad'}),
                      ('powershell', {'command': 'Set-Content source.txt bad'})]
        _, record = self.run_pi(kind='chat')
        self.assertFalse((self.project / 'source.txt').exists())
        names = {tool['function']['name'] for tool in self.calls[0]['tools']}
        self.assertNotIn('write', names)
        self.assertNotIn('powershell', names)
        self.assertEqual(record['sandbox_mode'], 'read-only')

    def test_paid_custom_model_uses_actual_relay_settlement(self):
        self.model.update(provider='custom', pricing_cny={'cache_miss_input': 2, 'cache_hit_input': 1, 'output': 4})
        workflow_settings.save_settings({'paid_allowed': True, 'model_scope': 'paid_only'})
        _, record = self.run_pi(kind='chat')
        self.assertEqual(record['model_calls'], 1)
        self.assertEqual(record['cost_settled_calls'], 1)
        self.assertAlmostEqual(record['cost_settled_cny'], 0.00004)
        self.assertEqual(record['cost_unknown_calls'], 0)


if __name__ == '__main__':
    unittest.main()
