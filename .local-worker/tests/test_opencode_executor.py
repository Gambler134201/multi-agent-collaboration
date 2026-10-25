"""Offline native-protocol, scope and shared-controller regression checks."""
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import opencode_worker as oc
import model_registry as registry
import execution_engines as engines
import execution_endpoints as ep
import model_agent_defaults as defaults
import model_router as router
import workflow_settings


class OpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.file = self.project / 'input.txt'
        self.file.write_bytes('逆流\nnonce'.encode('utf-8'))
        self.settings = {**workflow_settings.DEFAULTS, 'enabled': True, 'paid_allowed': True,
                         'enforce_paid_budget': False, 'enforce_model_call_limit': False}
        self.model = {'id': 'free', 'name': 'OpenCode Free', 'provider': 'custom', 'model': oc.MODEL, 'default_agent': 'opencode',
            'api_base': 'https://opencode.ai/inference/openai/v1', 'api_key_set': True, 'enabled': True,
            'max_input_tokens': 32768, 'max_output_tokens': 4096,
            'pricing_cny': {'cache_hit_input': 0, 'cache_miss_input': 0, 'output': 0}}
        self.enterContext(patch.object(registry, 'HERE', self.root))

    def body(self, **extra):
        return {'project': str(self.project), 'files': ['input.txt'], 'kind': 'chat', **extra}

    def client(self, kind='chat'):
        # Request handlers can be exercised without spawning any process.
        c = object.__new__(oc.ACP)
        c.root, c.kind, c.allowed = self.project, kind, [self.file]
        c.session, c.active_prompt = 'session', True
        c.events, c.denials, c.calls, c.native_updates, c.answer = [], [], {}, [], []
        c.responses = []
        c.send = c.responses.append
        return c

    def test_engine_default_and_no_silent_substitution(self):
        self.assertEqual(engines.resolution({}, self.settings, self.model), ('opencode', 'model'))
        self.assertEqual(defaults.normalize_update('opencode'), 'opencode')
        with self.assertRaises(ValueError):
            engines.resolve({'engine': 'opencode'}, self.settings, {'provider': 'workbuddy'})

    def test_new_endpoint_disabled_and_other_defaults_unchanged(self):
        state = ep.snapshot()
        self.assertFalse(state['endpoints']['opencode']['enabled'])
        self.assertTrue(state['endpoints']['dsh']['enabled'])
        self.assertFalse((self.root / ep.CONFIG_NAME).exists())

    def test_endpoint_disable_enforced_and_selection_saved(self):
        with self.assertRaises(ValueError):
            ep.gate('opencode', self.model, self.settings)
        ep.update('opencode', {'enabled': True}, self.settings)
        self.assertEqual(ep.gate('opencode', self.model, self.settings), 'opencode')
        ep.update('opencode', {'enabled': False}, self.settings)
        with self.assertRaises(ValueError):
            ep.gate('opencode', self.model, self.settings)

    def test_call_and_money_caps_never_bypassed(self):
        oc.validate_task(self.body(), self.settings, self.model)
        for setting in ('enforce_model_call_limit', 'enforce_paid_budget'):
            with self.subTest(setting=setting), self.assertRaises(ValueError):
                oc.validate_task(self.body(), {**self.settings, setting: True}, self.model)

    def test_exact_native_model_and_no_commands_or_reuse(self):
        with self.assertRaises(ValueError):
            oc.validate_task(self.body(), self.settings, {**self.model, 'model': 'auto'})
        for field in ('continue_task', 'reuse_session', 'test_command'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                oc.validate_task(self.body(**{field: 'x'}), self.settings, self.model)

    def test_file_scope_rejects_outside_records_config_and_wildcards(self):
        for path in ('../outside.txt', '.model-desk/task.json', 'models.json', '.env', 'a*.txt'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                oc.targets(self.project, [path])

    def test_native_policy_only_exact_files_and_denies_integrations(self):
        config = oc.native_config(self.project, ['input.txt'], 'chat', 'probe')
        p = config['policies']
        self.assertIn({'action': 'tool.use', 'resource': '*', 'effect': 'deny'}, p)
        self.assertIn({'action': 'integration.use', 'resource': '*', 'effect': 'deny'}, p)
        self.assertFalse(any(x['effect'] == 'allow' and x['resource'].startswith(('edit:', 'shell:', 'subagent:')) for x in p))
        edit = oc.native_config(self.project, ['input.txt'], 'edit', 'probe')
        self.assertIn({'action': 'tool.use', 'resource': 'edit:input.txt', 'effect': 'allow'}, edit['policies'])

    def test_read_file_rpc_preserves_unicode_and_sha(self):
        c = self.client()
        c.handle_request({'id': 1, 'method': 'fs/read_text_file', 'params': {'sessionId': 'session', 'path': str(self.file)}})
        self.assertEqual(c.responses[0]['result']['content'], '逆流\nnonce')
        self.assertEqual(c.events[0]['sha256'], oc.digest(self.file))

    def test_readonly_rpc_cannot_write(self):
        c = self.client()
        c.handle_request({'id': 1, 'method': 'fs/write_text_file', 'params': {'sessionId': 'session', 'path': str(self.file), 'content': 'bad'}})
        self.assertIn('error', c.responses[0])
        self.assertEqual(self.file.read_text(encoding='utf-8'), '逆流\nnonce')

    def test_edit_rpc_exact_file_and_unknown_method_denied(self):
        c = self.client('edit')
        c.handle_request({'id': 1, 'method': 'fs/write_text_file', 'params': {'sessionId': 'session', 'path': str(self.file), 'content': '新内容'}})
        self.assertEqual(self.file.read_text(encoding='utf-8'), '新内容')
        c.handle_request({'id': 2, 'method': 'terminal/create', 'params': {'sessionId': 'session'}})
        self.assertIn('error', c.responses[-1])

    def test_session_mismatch_and_permission_asks_denied(self):
        c = self.client()
        c.handle_request({'id': 1, 'method': 'fs/read_text_file', 'params': {'sessionId': 'other', 'path': str(self.file)}})
        self.assertIn('error', c.responses[0])
        c.handle_request({'id': 2, 'method': 'session/request_permission', 'params': {'sessionId': 'session'}})
        self.assertEqual(c.responses[1]['result']['outcome']['outcome'], 'cancelled')

    def test_child_or_wrong_session_update_rejected(self):
        c = self.client()
        for params in ({'sessionId': 'other'}, {'sessionId': 'session', 'update': {'_meta': {'opencode/child-session': {'id': 'child'}}}}):
            with self.assertRaises(ValueError):
                c.notification({'method': 'session/update', 'params': params})

    def test_cancel_sends_only_owned_session_cancel(self):
        c = self.client()
        c.cancel, c.timeout, c.next_id = lambda: True, 0, 0
        import time
        c.started = time.monotonic()
        with self.assertRaises(oc.Cancelled):
            c.request('session/prompt', {})
        self.assertEqual(c.responses[-1]['method'], 'session/cancel')
        self.assertEqual(c.responses[-1]['params']['sessionId'], 'session')

    def test_router_gates_native_endpoint_and_keeps_authorization(self):
        ep.update('opencode', {'enabled': True}, self.settings)
        body = self.body(model_id='free', route_mode='manual', prompt='read', routing={'category': 'extract', 'complexity': 'simple'})
        with patch.object(engines, 'availability', return_value={'ready': True}):
            route = router.choose(body, [self.model], self.settings, [], 10)
            self.assertEqual(route['engine'], 'opencode')
            with self.assertRaises(ValueError):
                router.choose(body, [self.model], {**self.settings, 'paid_allowed': False}, [], 10)

    def test_shared_panel_records_native_engine_and_project(self):
        import panel_server as panel
        self.enterContext(patch.object(registry, 'REGISTRY', self.root / 'models.json'))
        model = {**self.model, 'secret': registry.protect('offline-fixture-key')}
        registry.write_json(registry.REGISTRY, {'models': [model], 'default_model': 'free'})
        ep.update('opencode', {'enabled': True}, self.settings)
        with patch.object(engines, 'availability', return_value={'ready': True}), \
             patch.object(workflow_settings, 'snapshot', return_value=self.settings), \
             patch.object(panel.EXECUTOR, 'submit') as submit:
            row = panel.submit_task(self.body(model_id='free', source='codex', prompt='read',
                    routing={'category': 'extract', 'complexity': 'simple'}))
            self.assertEqual(row['engine'], 'opencode')
            self.assertEqual(Path(row['project']), self.project.resolve())
            self.assertTrue(row['files_readonly'])
            self.assertEqual(row['delegation']['mode'], 'outcome')
            submit.assert_called_once_with(panel.execute_task, row['id'])

    def test_both_brain_and_mcp_paths_admit_endpoint_but_not_arbitrary_paths(self):
        import codex_mcp_server as mcp
        self.assertEqual(mcp.validate_request('/api/execution-endpoints/opencode', 'POST'),
                         '/api/execution-endpoints/opencode')
        with self.assertRaises(ValueError):
            ep.update('opencode', {'access_mode': 'full'}, self.settings)


if __name__ == '__main__':
    unittest.main()
