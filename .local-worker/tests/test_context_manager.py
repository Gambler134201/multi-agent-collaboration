"""Stage C tests: scope-centred project index and large tool-result offloading.

Everything here is offline. The relay tests use a real loopback gateway plus a
loopback mock provider; no request ever reaches a real model, and no paid
generation is used.
"""
import copy
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import budget_relay as relay
import context_manager as cm
import dsh_worker
import model_registry as registry
import token_meter as meter


def big_tool_output(lines=4000, exit_code=1):
    text = ''.join('build log line %d with some payload\n' % index for index in range(lines))
    return text + 'Traceback (most recent call last):\nValueError: boom\nexit code: %d\n' % exit_code


class TempProjectCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.run = self.root / 'run'
        self.run.mkdir()

    def tearDown(self):
        self.temp.cleanup()


class IndexTests(TempProjectCase):
    def test_changed_content_with_same_size_and_timestamp_invalidates_index(self):
        source = self.project / 'same.py'
        source.write_text('def a(): pass\n', encoding='utf-8')
        path = self.run / 'index.json'
        first, _ = cm.ensure_index(self.project, ['same.py'], path)
        original = source.stat()
        source.write_text('def b(): pass\n', encoding='utf-8')
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        second, reused = cm.ensure_index(self.project, ['same.py'], path)
        self.assertFalse(reused)
        self.assertNotEqual(first['hash'], second['hash'])

    def test_scope_symbols_and_safe_language_degradation(self):
        (self.project / 'pkg').mkdir()
        (self.project / 'pkg' / 'mod.py').write_text(
            'class A:\n'
            '    def m(self, x: int) -> str:\n'
            '        return str(x)\n'
            '\n'
            "def f(a, password='sk-secret-123456', *args, **kw) -> int | None:\n"
            '    return None\n',
            encoding='utf-8')
        (self.project / 'data.xyz').write_text('opaque payload', encoding='utf-8')
        (self.project / 'broken.py').write_text('def (:\n', encoding='utf-8')
        (self.project / 'page.md').write_text('# Title\n\ntext\n', encoding='utf-8')
        index = cm.build_index(self.project, ['pkg/mod.py', 'data.xyz', 'broken.py', 'page.md'])
        entries = {entry['path']: entry for entry in index['files']}

        module = entries['pkg/mod.py']
        self.assertTrue(module['scope'])
        self.assertEqual(module['extraction'], 'ast')
        symbols = {symbol['name']: symbol for symbol in module['symbols']}
        self.assertEqual(symbols['A']['kind'], 'class')
        self.assertEqual(symbols['m']['kind'], 'method')
        self.assertIn('x', symbols['m']['signature'])
        self.assertIn('-> str', symbols['m']['signature'])
        self.assertTrue(symbols['f']['signature'].startswith('def f('))
        self.assertIn('*args', symbols['f']['signature'])
        # Credential-looking defaults are never rendered into the index.
        self.assertNotIn('sk-secret-123456', json.dumps(index, ensure_ascii=False))

        self.assertTrue(entries['data.xyz']['degraded'])
        self.assertEqual(entries['data.xyz']['reason'], 'unsupported_language')
        self.assertEqual(entries['data.xyz']['extraction'], 'file_index')
        self.assertTrue(entries['broken.py']['degraded'])
        self.assertEqual(entries['broken.py']['reason'], 'parse_failed')
        self.assertEqual(entries['page.md']['symbols'][0]['kind'], 'heading')

    def test_dependencies_tasks_caches_and_sensitive_files_are_excluded(self):
        for relative in ('venv/lib.py', 'runs/task/task.json', 'node_modules/pkg.js',
                         '__pycache__/mod.pyc', '.git/config'):
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('dependency-marker ' + relative, encoding='utf-8')
        (self.project / 'models.json').write_text(
            '{"api_key": "sk-live-abcdef123456"}', encoding='utf-8')
        (self.project / '.env').write_text('SERVICE_TOKEN=abc123456\n', encoding='utf-8')
        index = cm.build_index(self.project, ['models.json', '.env'])
        blob = json.dumps(index, ensure_ascii=False)
        paths = {entry['path'] for entry in index['files']}
        listing = set(index['listing'])
        for excluded in ('venv/lib.py', 'runs/task/task.json', 'node_modules/pkg.js',
                         '__pycache__/mod.pyc', '.git/config', 'models.json', '.env'):
            self.assertNotIn(excluded, paths)
            self.assertNotIn(excluded, listing)
        self.assertNotIn('sk-live-abcdef123456', blob)
        self.assertNotIn('SERVICE_TOKEN', blob)
        self.assertNotIn('abc123456', blob)
        self.assertGreaterEqual(index['excluded']['sensitive_files_skipped'], 2)
        self.assertIn('models.json', index['excluded']['sensitive_scope_files'])
        self.assertIn('.env', index['excluded']['sensitive_scope_files'])

    def test_sensitive_or_missing_scope_files_do_not_force_rebuild(self):
        (self.project / 'credentials.json').write_text('{"token": "x"}', encoding='utf-8')
        (self.project / 'a.py').write_text('def one():\n    pass\n', encoding='utf-8')
        index_path = self.run / 'context' / 'project-index.json'
        scope = ['a.py', 'credentials.json']
        index, reused = cm.ensure_index(self.project, scope, index_path)
        self.assertFalse(reused)
        self.assertIn('credentials.json', index['excluded']['sensitive_scope_files'])
        again, reused_again = cm.ensure_index(self.project, scope, index_path)
        self.assertTrue(reused_again)
        wider = scope + ['not-yet-created.py']
        _, reused_wider = cm.ensure_index(self.project, wider, index_path)
        self.assertFalse(reused_wider)
        _, reused_still = cm.ensure_index(self.project, wider, index_path)
        self.assertTrue(reused_still)

    def test_index_never_contains_file_bodies_or_env_values(self):
        marker = 'BODY_SECRET_MARKER_9f31'
        (self.project / 'app.py').write_text(
            '"""module docstring %s"""\n\n\ndef run(flag=True):\n    return "%s"\n' % (marker, marker),
            encoding='utf-8')
        index = cm.build_index(self.project, ['app.py'])
        blob = json.dumps(index, ensure_ascii=False)
        self.assertNotIn(marker, blob)
        self.assertIn('run', blob)

    def test_scope_outside_project_is_ignored(self):
        outside = self.root / 'outside.py'
        outside.write_text('def leak():\n    pass\n', encoding='utf-8')
        index = cm.build_index(self.project, [str(outside)])
        self.assertEqual(index['scope'], [])
        self.assertNotIn('outside.py', {entry['path'] for entry in index['files']})

    def test_changed_hash_invalidates_cached_index(self):
        target = self.project / 'a.py'
        target.write_text('def one():\n    pass\n', encoding='utf-8')
        index_path = self.run / 'context' / 'project-index.json'
        first, reused = cm.ensure_index(self.project, ['a.py'], index_path)
        self.assertFalse(reused)
        again, reused_again = cm.ensure_index(self.project, ['a.py'], index_path)
        self.assertTrue(reused_again)
        self.assertEqual(again['hash'], first['hash'])
        summary_path = index_path.with_name('project-index-summary.json')
        self.assertEqual(json.loads(summary_path.read_text(encoding='utf-8'))['hash'], first['hash'])
        self.assertTrue(json.loads(summary_path.read_text(encoding='utf-8'))['reused'])
        target.write_text('def two_extended():\n    pass\n', encoding='utf-8')
        third, reused_third = cm.ensure_index(self.project, ['a.py'], index_path)
        self.assertFalse(reused_third)
        self.assertNotEqual(third['hash'], first['hash'])
        self.assertIn('two_extended', json.dumps(third, ensure_ascii=False))
        self.assertEqual(cm.read_index_summary(self.run)['hash'], third['hash'])

    def test_index_requires_no_git_and_no_subprocess(self):
        source = Path(cm.__file__).read_text(encoding='utf-8')
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('import git', source)
        (self.project / 'a.py').write_text('def one():\n    pass\n', encoding='utf-8')
        index = cm.build_index(self.project, ['a.py'])
        self.assertTrue(index['hash'])


class OffloadTests(TempProjectCase):
    def test_single_line_json_tool_result_has_a_bounded_preview(self):
        content = json.dumps({'stdout': '中文' * 40000, 'exit_code': 1}, ensure_ascii=False)
        body = {'messages': [{'role': 'tool', 'tool_call_id': 'large-json', 'content': content}]}
        result = cm.prepare_request(body, self.run, preview_bytes=4000)
        preview = body['messages'][0]['content']
        self.assertEqual(result['offloaded_count'], 1)
        self.assertLess(len(preview.encode('utf-8')), 5500)
        self.assertIn('exit code=1', preview)
        self.assertEqual(body['messages'][0]['tool_call_id'], 'large-json')

    def test_marker_inside_source_code_does_not_skip_offloading(self):
        content = 'source code containing ' + cm.OFFLOAD_MARKER + '\n' + ('source\n' * 10000)
        body = {'messages': [{'role': 'tool', 'tool_call_id': 'source', 'content': content}]}
        result = cm.prepare_request(body, self.run)
        self.assertEqual(result['offloaded_count'], 1)

    def make_messages(self, tool_text=None):
        tool_text = tool_text if tool_text is not None else big_tool_output()
        return [
            {'role': 'system', 'content': 'system goal'},
            {'role': 'user', 'content': 'user goal'},
            {'role': 'assistant', 'content': None,
             'tool_calls': [{'id': 'call_1', 'type': 'function',
                             'function': {'name': 'run'}}]},
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': tool_text},
            {'role': 'tool', 'tool_call_id': 'call_2', 'content': 'tiny result'},
        ]

    def test_large_tool_result_saved_with_preview_and_readable_full_text(self):
        original = big_tool_output()
        messages = self.make_messages(original)
        metrics = cm.offload_messages(messages, run=self.run, min_bytes=1000)
        self.assertEqual(metrics['offloaded_count'], 1)
        self.assertEqual(metrics['not_trimmed_count'], 0)
        record = metrics['records'][0]
        self.assertEqual(record['tool_call_id'], 'call_1')
        self.assertEqual(record['action'], 'offloaded')
        path = Path(record['path'])
        self.assertTrue(path.is_file())
        preview = messages[3]['content']
        self.assertTrue(preview.startswith(cm.OFFLOAD_MARKER))
        self.assertIn(str(path), preview)
        self.assertIn(str(record['bytes']), preview)
        self.assertIn('exit code=1', preview)
        self.assertIn('截取说明', preview)
        self.assertLess(len(preview.encode('utf-8')), record['bytes'])
        # Only tool content changed; goals, order and protocol are intact.
        self.assertEqual(messages[0]['content'], 'system goal')
        self.assertEqual(messages[1]['content'], 'user goal')
        self.assertEqual(messages[2]['tool_calls'][0]['id'], 'call_1')
        self.assertEqual(messages[3]['tool_call_id'], 'call_1')
        self.assertEqual(messages[4]['content'], 'tiny result')
        self.assertEqual(cm.read_offloaded(path)['text'], original)
        self.assertEqual(cm.read_offloaded(path, offset=2, limit=1)['text'],
                         original.split('\n')[1])
        listed = cm.list_offloaded(self.run)
        self.assertEqual([row['path'] for row in listed], [str(path)])

    def test_all_large_tool_results_offloaded_not_only_the_last(self):
        messages = self.make_messages()
        messages.insert(4, {'role': 'tool', 'tool_call_id': 'call_3',
                            'content': big_tool_output(lines=2500, exit_code=2)})
        metrics = cm.offload_messages(messages, run=self.run, min_bytes=1000)
        self.assertEqual(metrics['offloaded_count'], 2)
        self.assertIn('exit code=1', messages[3]['content'])
        self.assertIn('exit code=2', messages[4]['content'])
        self.assertEqual(len(cm.list_offloaded(self.run)), 2)

    def test_repeat_request_reuses_stable_file_and_identical_preview(self):
        first = self.make_messages()
        second = copy.deepcopy(first)
        first_metrics = cm.offload_messages(first, run=self.run, min_bytes=1000)
        second_metrics = cm.offload_messages(second, run=self.run, min_bytes=1000)
        self.assertEqual(first_metrics['created_count'], 1)
        self.assertEqual(second_metrics['created_count'], 0)
        self.assertEqual(second_metrics['reused_count'], 1)
        self.assertEqual(first_metrics['records'][0]['path'], second_metrics['records'][0]['path'])
        self.assertEqual(first[3]['content'], second[3]['content'])
        self.assertEqual(len(cm.list_offloaded(self.run)), 1)

    def test_already_offloaded_message_is_not_double_wrapped(self):
        messages = self.make_messages()
        cm.offload_messages(messages, run=self.run, min_bytes=1000)
        preview = messages[3]['content']
        metrics = cm.offload_messages(messages, run=self.run, min_bytes=1000)
        self.assertEqual(metrics['already_offloaded_count'], 1)
        self.assertEqual(metrics['offloaded_count'], 0)
        self.assertEqual(messages[3]['content'], preview)

    def test_multimodal_and_unknown_tool_content_kept_and_annotated(self):
        multimodal = [{'type': 'text', 'text': 'done ' + 'x' * 9000},
                      {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]
        unknown = {'exit_code': 1, 'stdout': 'y' * 9000}
        messages = [{'role': 'tool', 'tool_call_id': 'call_1', 'content': multimodal},
                    {'role': 'tool', 'tool_call_id': 'call_2', 'content': unknown}]
        metrics = cm.offload_messages(messages, run=self.run, min_bytes=100)
        self.assertEqual(metrics['not_trimmed_count'], 2)
        self.assertEqual(metrics['offloaded_count'], 0)
        self.assertIs(messages[0]['content'], multimodal)
        self.assertIs(messages[1]['content'], unknown)
        self.assertEqual({row['reason'] for row in metrics['records']}, {'non_text_tool_content'})
        self.assertFalse(cm.default_output_dir(self.run).exists())

    def test_offload_writes_only_under_the_run_directory(self):
        before = sorted(str(path) for path in self.project.rglob('*'))
        messages = self.make_messages()
        metrics = cm.offload_messages(messages, run=self.run, min_bytes=1000)
        after = sorted(str(path) for path in self.project.rglob('*'))
        self.assertEqual(before, after)
        self.assertFalse(Path(metrics['records'][0]['path']).is_relative_to(self.project))

    def test_prepare_request_records_before_after_bytes_and_index_size(self):
        (self.project / 'a.py').write_text('def one():\n    pass\n', encoding='utf-8')
        cm.ensure_index(self.project, ['a.py'], cm.default_index_path(self.run))
        body = {'messages': self.make_messages()}
        metrics = cm.prepare_request(body, self.run, min_bytes=1000)
        self.assertLess(metrics['input_bytes_after'], metrics['input_bytes_before'])
        self.assertGreater(metrics['saved_bytes'], 0)
        fields = cm.context_fields(metrics)
        self.assertEqual(fields['context_offloaded_count'], 1)
        self.assertGreater(fields['context_index']['bytes'], 0)
        self.assertEqual(fields['context_index']['hash'],
                         cm.load_index(cm.default_index_path(self.run))['hash'])
        self.assertIn('token', fields['context_note'])
        self.assertEqual(fields['context_bytes_before'], metrics['input_bytes_before'])


class PromptTests(unittest.TestCase):
    def test_verify_prompt_rejects_silent_truncation(self):
        with self.assertRaises(ValueError):
            cm.verify_prompt('truncated text', 'full task text and acceptance')
        with self.assertRaises(ValueError):
            cm.verify_prompt(None, 'task')
        self.assertEqual(cm.verify_prompt('full text\nmore', 'full text'), 'full text\nmore')
        self.assertEqual(cm.verify_prompt('only constraints', ''), 'only constraints')

    def test_prompt_fields_report_preservation(self):
        fields = cm.prompt_fields('base task', 'base task\n约束')
        self.assertTrue(fields['prompt_preserved'])
        self.assertGreater(fields['prompt_bytes'], fields['prompt_base_bytes'])
        self.assertEqual(len(fields['prompt_sha256']), 64)


class RelayOffloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        meter.reset_probe_cache()
        meter.reset_metrics_cache()
        meter.reset_calibration()
        self.settings = {'enabled': True, 'paid_allowed': True, 'enforce_paid_budget': False,
                         'enforce_model_call_limit': False, 'task_budget_cny': 0,
                         'daily_budget_cny': 0, 'max_model_calls': 30, 'timeout': 5}

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        meter.reset_probe_cache()
        meter.reset_metrics_cache()
        meter.reset_calibration()
        self.temp.cleanup()

    def start_upstream(self, with_usage=True):
        received = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                payload = {'choices': [{'message': {'content': 'OK'}}]}
                if with_usage:
                    payload['usage'] = {'prompt_tokens': 500, 'completion_tokens': 3,
                                        'total_tokens': 503, 'prompt_cache_hit_tokens': 0,
                                        'prompt_cache_miss_tokens': 500}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, received

    def model(self, port):
        return {'id': 'custom', 'name': 'custom', 'provider': 'custom', 'model': 'm',
                'api_base': f'http://127.0.0.1:{port}/v1', 'api_key': 'k', 'bypass_proxy': True,
                'max_input_tokens': 1000000, 'max_output_tokens': 64000,
                'pricing_cny': {'cache_hit_input': 1, 'cache_miss_input': 1, 'output': 8}}

    def task(self, task_id):
        run = registry.task_dir(task_id)
        registry.patch_task(run, id=task_id, model_calls=0, route={'budget_cny': 0})
        return run

    def send(self, gateway, body):
        request = Request(gateway.url + '/chat/completions',
                          data=json.dumps(body, ensure_ascii=False).encode(),
                          headers={'Content-Type': 'application/json',
                                   'Authorization': 'Bearer ' + gateway.token})
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return response.read()

    def wait_for(self, run, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        state = {}
        while time.monotonic() < deadline:
            state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
            if predicate(state):
                return state
            time.sleep(.05)
        return state

    def messages(self, tool_text=None):
        tool_text = tool_text if tool_text is not None else big_tool_output()
        return [{'role': 'system', 'content': '系统目标与验收'},
                {'role': 'user', 'content': '用户约束'},
                {'role': 'assistant', 'content': None,
                 'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'run'}}]},
                {'role': 'tool', 'tool_call_id': 'call_1', 'content': tool_text},
                {'role': 'tool', 'tool_call_id': 'call_2', 'content': 'small result'}]

    def test_relay_offloads_big_tool_result_and_settles_usage(self):
        upstream, received = self.start_upstream()
        run = self.task('relay-offload')
        model = self.model(upstream.server_port)
        original = self.messages()
        original_body = {'model': 'm', 'messages': copy.deepcopy(original)}
        settings_before = dict(self.settings)
        with relay.Relay(run, model, self.settings) as gateway:
            self.send(gateway, {'model': 'm', 'messages': copy.deepcopy(original)})

        self.assertEqual(len(received), 1)
        sent = received[0]['messages']
        self.assertEqual([message['role'] for message in sent],
                         ['system', 'user', 'assistant', 'tool', 'tool'])
        self.assertEqual(sent[0]['content'], '系统目标与验收')
        self.assertEqual(sent[1]['content'], '用户约束')
        self.assertEqual(sent[2]['tool_calls'][0]['id'], 'call_1')
        self.assertEqual(sent[3]['tool_call_id'], 'call_1')
        self.assertTrue(sent[3]['content'].startswith(cm.OFFLOAD_MARKER))
        self.assertIn('exit code=1', sent[3]['content'])
        self.assertEqual(sent[4]['content'], 'small result')
        self.assertLess(len(sent[3]['content'].encode('utf-8')),
                        len(original_body['messages'][3]['content'].encode('utf-8')))

        state = self.wait_for(run, lambda value: value.get('cost_settled_calls') == 1)
        self.assertEqual(state['context_offloaded_count'], 1)
        self.assertGreater(state['context_bytes_before'], state['context_bytes_after'])
        self.assertEqual(state['context_created_count'], 1)
        self.assertIn('token', state['context_note'])
        self.assertIsInstance(state['meter_input_tokens'], int)
        # Metering uses the processed (smaller) request, not the original body.
        self.assertLess(state['meter_input_tokens'],
                        meter.measure(model, original_body)['input']['tokens'])
        # Cost settlement is still driven by the provider's reported usage.
        self.assertEqual(state['cost_settled_calls'], 1)
        self.assertGreater(state['cost_settled_cny'], 0)
        record = state['context_outputs'][0]
        self.assertEqual(cm.read_offloaded(record['path'])['text'], original[3]['content'])
        self.assertEqual(self.settings, settings_before, 'relay must not change switches/budgets')

    def test_repeated_identical_request_is_context_stable(self):
        upstream, received = self.start_upstream()
        run = self.task('relay-repeat')
        model = self.model(upstream.server_port)
        original = self.messages()
        with relay.Relay(run, model, self.settings) as gateway:
            self.send(gateway, {'model': 'm', 'messages': copy.deepcopy(original)})
            self.send(gateway, {'model': 'm', 'messages': copy.deepcopy(original)})
        self.assertEqual(len(received), 2)
        self.assertEqual(received[0]['messages'], received[1]['messages'])
        state = self.wait_for(run, lambda value: value.get('cost_settled_calls') == 2)
        self.assertEqual(state['cost_settled_calls'], 2)
        # The second pass reused the original record and did not regenerate it.
        self.assertEqual(state['context_created_count'], 0)
        self.assertEqual(state['context_reused_count'], 1)
        outputs = cm.list_offloaded(run)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(cm.read_offloaded(outputs[0]['path'])['text'], original[3]['content'])

    def test_rejected_multimodal_request_is_not_mangled_or_persisted(self):
        upstream, received = self.start_upstream()
        run = self.task('relay-multimodal')
        model = self.model(upstream.server_port)
        body = {'model': 'm', 'messages': [
            {'role': 'user', 'content': [{'type': 'image_url',
                                          'image_url': {'url': 'data:image/png;base64,AAAA'}}]}]}
        with relay.Relay(run, model, self.settings) as gateway:
            with self.assertRaises(HTTPError) as caught:
                self.send(gateway, body)
        self.assertEqual(caught.exception.code, 402)
        self.assertIn('文本', json.loads(caught.exception.read())['error']['message'])
        self.assertEqual(received, [])
        self.assertFalse(cm.default_output_dir(run).exists())

    def test_oversized_request_is_rejected_explicitly_after_offload(self):
        upstream, received = self.start_upstream()
        run = self.task('relay-oversize')
        model = self.model(upstream.server_port)
        body = {'model': 'm', 'messages': [
            {'role': 'system', 'content': 'x' * 200000},
            {'role': 'user', 'content': 'hi'}]}
        with patch.object(relay, 'PROTOCOL_BYTE_CEILING', 50000):
            with relay.Relay(run, model, self.settings) as gateway:
                with self.assertRaises(HTTPError) as caught:
                    self.send(gateway, body)
        self.assertEqual(caught.exception.code, 402)
        self.assertIn('协议安全上限', json.loads(caught.exception.read())['error']['message'])
        self.assertEqual(received, [])

    def test_cancelled_task_is_blocked_before_any_offload_or_send(self):
        upstream, received = self.start_upstream()
        run = self.task('relay-cancel')
        (run / 'cancelled.flag').write_text('1', encoding='utf-8')
        model = self.model(upstream.server_port)
        with relay.Relay(run, model, self.settings) as gateway:
            with self.assertRaises(HTTPError) as caught:
                self.send(gateway, {'model': 'm', 'messages': self.messages()})
        self.assertEqual(caught.exception.code, 402)
        self.assertIn('取消', json.loads(caught.exception.read())['error']['message'])
        self.assertEqual(received, [])

    def test_readonly_and_sandbox_switches_are_unchanged(self):
        self.assertEqual(dsh_worker.sandbox_mode('chat'), 'read-only')
        self.assertEqual(dsh_worker.sandbox_mode('edit'), 'danger-full-access')
        self.assertFalse(self.settings['enforce_paid_budget'])
        self.assertFalse(self.settings['enforce_model_call_limit'])


class ProjectRecordExclusionTests(TempProjectCase):
    """Workbench record trees stay out of the code index and dispatch context."""

    def test_model_desk_directory_is_excluded_from_the_listing(self):
        records = self.project / '.model-desk'
        (records / 'runs' / 'task-1').mkdir(parents=True)
        (records / 'records.db').write_bytes(b'not-a-real-db')
        (self.project / 'app.py').write_text('def app(): return 1\n', encoding='utf-8')
        self.assertTrue(cm.is_excluded_dir('.model-desk'))
        listing, _skipped, _truncated = cm._project_listing(self.project)
        self.assertIn('app.py', listing)
        self.assertFalse(any('.model-desk' in name for name in listing))

    def test_index_metadata_declares_the_excluded_directory(self):
        path = self.run / 'index.json'
        index, _ = cm.ensure_index(self.project, [], path)
        self.assertIn('.model-desk', index['excluded']['directories'])

    def test_executor_constraints_never_claim_a_hard_sandbox(self):
        text = cm.execution_constraints(False)
        self.assertIn('.model-desk', text)
        self.assertIn('不是硬隔离', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
