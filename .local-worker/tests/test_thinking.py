from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import thinking_config as thinking
import model_registry as registry
import dsh_worker
import budget_relay
import workflow_settings


class ThinkingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.local = {'id': 'test', 'provider': 'local', 'api_base': 'http://127.0.0.1:1234/v1', 'model': 'qwen'}
        self.local['thinking_info'] = {**self.local, 'adapter': 'strata', 'supported': ['default', 'off', 'low', 'medium', 'xhigh'], 'default_effort': 'xhigh', 'default_enabled': True, 'status': 'verified'}
        self.paid = {'provider': 'deepseek', 'api_base': 'https://api.deepseek.com', 'model': 'deepseek-flash'}

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def test_default_inherits_engine_without_conflicting_overrides(self):
        body = {'reasoning_effort': 'low', 'thinking': {'type': 'disabled'}, 'chat_template_kwargs': {'enable_thinking': False, 'reasoning_effort': 'medium', 'other': 1}}
        info = thinking.apply(body, self.local)
        self.assertEqual(info['default_effort'], 'xhigh')
        self.assertNotIn('reasoning_effort', body)
        self.assertNotIn('thinking', body)
        self.assertEqual(body['chat_template_kwargs'], {'other': 1})

    def test_local_levels_enable_disable_and_exact_values(self):
        for level in ['off', 'low', 'medium', 'xhigh']:
            body = {}
            thinking.apply(body, {**self.local, 'thinking_level': level})
            self.assertEqual(body['reasoning_effort'], 'none' if level == 'off' else level)
            self.assertEqual(body['chat_template_kwargs']['enable_thinking'], level != 'off')
            if level != 'off':
                self.assertEqual(body['chat_template_kwargs']['reasoning_effort'], level)
        with self.assertRaises(ValueError):
            thinking.validate({**self.local, 'thinking_level': 'high'})

    def test_deepseek_levels_do_not_invent_medium_or_xhigh(self):
        for level in ['default', 'off', 'low', 'high', 'max']:
            body = {}
            thinking.apply(body, {**self.paid, 'thinking_level': level})
            if level == 'default':
                self.assertEqual(body, {})
            else:
                self.assertEqual(body['thinking']['type'], 'disabled' if level == 'off' else 'enabled')
                self.assertEqual(body.get('reasoning_effort'), None if level == 'off' else level)
        for level in ['medium', 'xhigh', True, {}]:
            with self.assertRaises(ValueError):
                thinking.validate({**self.paid, 'thinking_level': level})

    def test_stale_endpoint_metadata_is_not_used(self):
        model = {**self.local, 'api_base': 'http://127.0.0.1:4567/v1', 'thinking_level': 'low'}
        with self.assertRaises(ValueError):
            thinking.validate(model)

    def test_thinking_change_invalidates_continuation(self):
        a = dsh_worker.fingerprint(self.local, 'chat', 'project')
        b = dsh_worker.fingerprint({**self.local, 'thinking_level': 'low'}, 'chat', 'project')
        self.assertNotEqual(a, b)

    def test_registry_persists_level_and_keeps_key_and_presets(self):
        registry.write_json(registry.HERE / 'settings.json', {'model': 'qwen', 'api_base': self.local['api_base']})
        registry.list_models()
        with registry.registry_lock():
            data = registry.read_registry()
            data['models'][0]['thinking_info'] = {**self.local['thinking_info'], 'model': 'qwen'}
            registry.write_json(registry.REGISTRY, data)
        body = {'name': 'home', 'provider': 'local', 'api_base': self.local['api_base'], 'model': 'qwen', 'api_key': 'private-test', 'thinking_level': 'low', 'agent_preset': 'native'}
        registry.upsert_model(body, 'home')
        body.pop('thinking_level');body.pop('api_key')
        row = registry.upsert_model(body, 'home')
        self.assertEqual(row['thinking_level'], 'low')
        self.assertEqual(row['agent_preset'], 'native')
        self.assertEqual(registry.get_model('home')['api_key'], 'private-test')

    def test_probe_shared_settings_override_template_without_inference(self):
        shared = {}
        paths = []
        class Engine(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                paths.append(self.path)
                value = {'build_info': 'Strata 0.1.39', 'model_alias': 'qwen',
                    'chat_template': "{%- set r = reasoning_effort|default('xhigh') %}{% if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}{% endif %}"} if self.path == '/props' else {'defaults': shared}
                raw = json.dumps(value).encode()
                self.send_response(200);self.end_headers();self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Engine)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            model = {**self.local, 'api_base': f'http://127.0.0.1:{server.server_port}/v1', 'bypass_proxy': True}
            self.assertEqual(thinking.probe(model)['default_effort'], 'xhigh')
            shared['reasoning_effort'] = 'low'
            self.assertEqual(thinking.probe(model)['default_effort'], 'low')
            shared['reasoning_effort'] = 'none'
            self.assertFalse(thinking.probe(model)['default_enabled'])
            self.assertEqual(set(paths), {'/props', '/settings'})
        finally:
            server.shutdown();server.server_close()

    def test_gateway_sends_selected_local_level_to_upstream(self):
        captured = []
        class Engine(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200);self.end_headers();self.wfile.write(b'{"choices":[{"message":{"content":"OK"}}]}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Engine)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            model = {**self.local, 'api_base': f'http://127.0.0.1:{server.server_port}/v1', 'thinking_level': 'medium', 'max_input_tokens': 32768, 'max_output_tokens': 4096, 'bypass_proxy': True}
            model['thinking_info'] = {**self.local['thinking_info'], 'api_base': model['api_base']}
            run = registry.task_dir('thinking-wire')
            registry.patch_task(run, model_calls=0, route={'budget_cny': 0})
            with budget_relay.Relay(run, model, workflow_settings.DEFAULTS) as relay:
                body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'Hi'}], 'max_tokens': 100}
                request = Request(relay.url + '/chat/completions', data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + relay.token})
                with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                    response.read()
            self.assertEqual(captured[0]['reasoning_effort'], 'medium')
            self.assertTrue(captured[0]['chat_template_kwargs']['enable_thinking'])
            state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
            self.assertEqual(state['thinking_runtime']['selected'], 'medium')
        finally:
            server.shutdown();server.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
