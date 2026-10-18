"""Per-model default execution agent: priority, routing, locks and atomic patch.

All checks are offline.  They use a temporary registry and mocked executor
availability; no model/provider request is sent and no production setting is
read or written.
"""
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delegation_policy as policy
import execution_engines as engines
import model_agent_defaults as preferences
import model_registry as registry
import model_router as router
import panel_server as panel
import stage_store
import workflow_settings as settings
from test_stage_workflow import Base


READY = {'ready': True, 'name': 'fixture', 'message': ''}


def ready_agents(engine):
    if engine in engines.AGENTS:
        return dict(READY)
    return {'ready': False, 'message': '未知执行 Agent'}


class RouterPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.settings = {**settings.DEFAULTS, 'enabled': True, 'paid_allowed': True,
                         'enforce_paid_budget': False, 'enforce_model_call_limit': False,
                         'model_scope': 'all', 'execution_agent': 'dsh'}
        self.local = {
            'id': 'local', 'name': 'local', 'provider': 'local', 'model': 'qwen', 'enabled': True,
            'max_input_tokens': 262144, 'max_output_tokens': 8192,
            'preferred_tasks': ['单文件修改', '信息整理'], 'max_task_complexity': 'simple',
            'cost_preference': 'economy', 'default_agent': 'pi',
        }
        self.paid = {
            'id': 'paid', 'name': 'paid', 'provider': 'deepseek', 'api_base': 'https://api.deepseek.com',
            'model': 'deepseek-flash', 'enabled': True, 'api_key_set': True,
            'max_input_tokens': 1000000, 'max_output_tokens': 64000,
            'preferred_tasks': ['多文件修改', '复杂逻辑分析', '疑难问题排查'],
            'max_task_complexity': 'complex', 'cost_preference': 'balanced',
            'billing': {'balance': {'status': 'ready', 'is_available': True,
                                    'balance_infos': [{'currency': 'CNY', 'total_balance': '10'}]}},
            'default_agent': 'dsh',
        }
        self.body = {'kind': 'edit', 'route_mode': 'auto', 'prompt': 'fixture', 'files': ['a.py'],
                     'routing': {'category': 'code_small', 'complexity': 'simple',
                                 'expected_calls': 3}}

    def choose(self, body, models=None):
        with patch.object(engines, 'availability', side_effect=ready_agents):
            return router.choose(body, models or [self.local, self.paid], self.settings)

    def test_same_model_resolves_explicit_then_model_then_global(self):
        manual = {**self.body, 'route_mode': 'manual', 'model_id': 'local'}
        legacy = {key: value for key, value in self.local.items() if key != 'default_agent'}
        with patch.object(engines, 'availability', side_effect=ready_agents):
            model_route = router.choose(manual, [self.local], self.settings)
            explicit_route = router.choose({**manual, 'engine': 'dsh'}, [self.local], self.settings)
            self.settings['execution_agent'] = 'pi'
            inherited = router.choose({**manual, **{'engine': 'model_default'}}, [legacy], self.settings)
        self.assertEqual((model_route['engine'], model_route['agent_source']), ('pi', 'model'))
        self.assertEqual((explicit_route['engine'], explicit_route['agent_source']), ('dsh', 'explicit'))
        self.assertEqual((inherited['engine'], inherited['agent_source']), ('pi', 'global'))

    def test_auto_route_uses_each_final_candidates_own_agent(self):
        simple = self.choose(self.body)
        complex_body = {**self.body, 'routing': {**self.body['routing'], 'category': 'code_multi',
                                                  'complexity': 'complex'}}
        complex_route = self.choose(complex_body)
        explicit = self.choose({**self.body, 'engine': 'dsh'})
        self.assertEqual((simple['model_id'], simple['engine']), ('local', 'pi'))
        self.assertEqual((complex_route['model_id'], complex_route['engine']), ('paid', 'dsh'))
        # An explicit override still wins over the local model's saved Pi.
        self.assertEqual((explicit['model_id'], explicit['engine'], explicit['agent_source']),
                         ('local', 'dsh', 'explicit'))

    def test_legacy_missing_field_follows_global_and_invalid_value_is_refused(self):
        legacy = {key: value for key, value in self.local.items() if key != 'default_agent'}
        manual = {**self.body, 'route_mode': 'manual', 'model_id': 'local'}
        with patch.object(engines, 'availability', side_effect=ready_agents):
            self.settings['execution_agent'] = 'pi'
            route = router.choose(manual, [legacy], self.settings)
        self.assertEqual((route['engine'], route['agent_source']), ('pi', 'global'))
        self.assertEqual(preferences.stored(legacy), preferences.FOLLOW_GLOBAL)
        broken = {**self.local, 'default_agent': 'automatic'}
        with patch.object(engines, 'availability', side_effect=ready_agents):
            with self.assertRaisesRegex(ValueError, '模型默认执行 Agent 无效'):
                router.choose(manual, [broken], self.settings)
        with self.assertRaises(ValueError):
            preferences.normalize_update('automatic')

    def test_unavailable_model_agent_is_refused_without_falling_back(self):
        manual = {**self.body, 'route_mode': 'manual', 'model_id': 'local'}

        def availability(engine):
            if engine == 'pi':
                return {'ready': False, 'message': 'Pi fixture unavailable'}
            return dict(READY)

        with patch.object(engines, 'availability', side_effect=availability):
            with self.assertRaisesRegex(ValueError, 'Pi Agent.*不会切换'):
                router.choose(manual, [self.local], self.settings)

    def test_paid_gate_uses_the_resolved_agent_not_the_global_one(self):
        # The local model is free; its saved Pi preference must remain usable even
        # though the global default is DSH.  Conversely a paid model with an
        # explicit non-agent mode is refused by the resolved agent.
        paid_manual = {**self.body, 'route_mode': 'manual', 'model_id': 'paid', 'engine': 'direct'}
        with patch.object(engines, 'availability', side_effect=ready_agents):
            with self.assertRaisesRegex(ValueError, '付费任务请使用 DSH 或 Pi'):
                router.choose(paid_manual, [self.paid], self.settings)


class PreferenceStoreTests(Base):
    def test_atomic_patch_changes_only_the_preference(self):
        settings.save_settings({'enabled': True, 'execution_agent': 'dsh'})
        raw_path = registry.REGISTRY
        data = json.loads(raw_path.read_text(encoding='utf-8'))
        data['models'][0].update({'agent_preset': 'standard', 'thinking_level': 'default',
                                  'pricing_cny': None, 'secret': 'opaque-fixture',
                                  'default_agent': 'dsh', 'capability_notes': 'keep me'})
        registry.write_json(raw_path, data)
        before = json.loads(raw_path.read_text(encoding='utf-8'))
        settings_before = (registry.HERE / 'workflow.json').read_bytes()
        result = preferences.update('home', 'pi')
        after = json.loads(raw_path.read_text(encoding='utf-8'))
        expected = deepcopy(before)
        expected['models'][0]['default_agent'] = 'pi'
        self.assertEqual(after, expected)
        self.assertEqual((registry.HERE / 'workflow.json').read_bytes(), settings_before)
        self.assertEqual(result['default_agent'], 'pi')
        self.assertNotIn('opaque-fixture', json.dumps(result))

        before_invalid = raw_path.read_bytes()
        with self.assertRaises(ValueError):
            preferences.update('home', 'invalid-agent')
        self.assertEqual(raw_path.read_bytes(), before_invalid)
        with self.assertRaisesRegex(ValueError, '模型不存在'):
            preferences.update('missing', 'pi')

    def test_upsert_keeps_legacy_inheritance_and_accepts_valid_values(self):
        data = json.loads(registry.REGISTRY.read_text(encoding='utf-8'))
        data['models'][0].pop('default_agent', None)
        registry.write_json(registry.REGISTRY, data)
        public = registry.list_models()['models'][0]
        self.assertEqual(public['default_agent'], preferences.FOLLOW_GLOBAL)
        self.assertTrue(public['default_agent_valid'])
        body = dict(public)
        body['name'] = 'renamed'
        updated = registry.upsert_model(body, 'home')
        self.assertEqual(updated['default_agent'], preferences.FOLLOW_GLOBAL)
        body['default_agent'] = 'pi'
        self.assertEqual(registry.upsert_model(body, 'home')['default_agent'], 'pi')
        connection_only = dict(body)
        connection_only.pop('default_agent')
        connection_only['name'] = 'connection-only edit'
        preserved = registry.upsert_model(connection_only, 'home')
        self.assertEqual(preserved['default_agent'], 'pi')
        body['default_agent'] = 'bad'
        with self.assertRaises(ValueError):
            registry.upsert_model(body, 'home')


class PanelPreferenceTests(Base):
    def setUp(self):
        super().setUp()
        settings.save_settings({'enabled': True, 'paid_allowed': False, 'model_scope': 'all',
                                'enforce_paid_budget': False, 'enforce_model_call_limit': False,
                                'execution_agent': 'dsh'})

    def submit(self, **kwargs):
        body = {'prompt': 'fixture task', 'model_id': 'home', 'route_mode': 'manual',
                'kind': 'edit', 'project': str(self.project), 'files': ['a.py'],
                'source': 'codex', **kwargs}
        with patch.object(panel.EXECUTOR, 'submit'), \
             patch.object(engines, 'availability', side_effect=ready_agents):
            return panel.submit_task(body)

    def test_default_task_omits_global_override_and_uses_model_preference(self):
        preferences.update('home', 'pi')
        task = self.submit()
        self.assertEqual((task['engine'], task['route']['agent_source']), ('pi', 'model'))
        explicit = self.submit(engine='dsh')
        self.assertEqual((explicit['engine'], explicit['route']['agent_source']), ('dsh', 'explicit'))
        # A form sentinel is still inheritance, never an engine named model_default.
        inherited = self.submit(engine='model_default')
        self.assertEqual((inherited['engine'], inherited['route']['agent_source']), ('pi', 'model'))

    def test_existing_tasks_continuation_rework_and_recovery_keep_original(self):
        parent_run = self.task('pi-parent', engine='pi', pi_session_id='pi-exact',
                               pi_session_file=str(self.project / '.model-desk/runs/pi-parent/session.jsonl'),
                               connection_fingerprint='pi-fingerprint')
        parent = registry.read_task(parent_run)
        # New preference is DSH, but every existing-task path stays Pi.
        preferences.update('home', 'dsh')
        settings.save_settings({'execution_agent': 'dsh'})
        child = self.submit(continue_task=parent['id'])
        self.assertEqual((child['engine'], child['pi_session_id']), ('pi', 'pi-exact'))
        policy.record_executor(parent_run, {'completed': True, 'changed_files': ['a.py']})
        policy.request_rework(parent_run, 'main', ['c1'], 'fixture defect', 'a.py only')
        rework = self.submit(rework_of=parent['id'])
        self.assertEqual(rework['engine'], 'pi')
        self.assertNotIn('pi_session_id', rework)
        registry.patch_task(parent_run, status='recovery_pending')
        with patch.object(panel.EXECUTOR, 'submit') as queue:
            restored = panel.recover_task(parent['id'], {'confirm': True})
        self.assertEqual(restored['task_id'], parent['id'])
        self.assertEqual(registry.read_task(parent_run)['engine'], 'pi')
        queue.assert_called_once()

    def test_new_stage_uses_fixed_models_saved_preference(self):
        preferences.update('home', 'pi')
        settings.save_settings({'execution_agent': 'dsh'})
        _, stage = self.goal_with_stage()
        with patch.object(panel.EXECUTOR, 'submit'), \
             patch.object(engines, 'availability', side_effect=ready_agents):
            task = panel.dispatch_stage(stage['id'], {'prompt': 'stage fixture'})['task']
        self.assertEqual((task['engine'], task['route']['agent_source']), ('pi', 'model'))

    def test_stage_reuse_keeps_session_agent_after_defaults_change(self):
        preferences.update('home', 'pi')
        _, stage = self.goal_with_stage()
        with patch.object(panel.EXECUTOR, 'submit'), \
             patch.object(engines, 'availability', side_effect=ready_agents):
            first = panel.dispatch_stage(stage['id'], {'prompt': 'first round'})['task']
        registry.patch_task(registry.task_dir(first['id']), status='completed',
                            pi_session_id='original-pi',
                            pi_session_file=str(self.project / 'session.jsonl'))
        stage_store.sync_from_tasks()
        preferences.update('home', 'dsh')
        settings.save_settings({'execution_agent': 'dsh'})
        with patch.object(panel.EXECUTOR, 'submit'), \
             patch.object(engines, 'availability', side_effect=ready_agents):
            reused = panel.dispatch_stage(stage['id'], {
                'prompt': 'same session', 'reuse_session': True})['task']
        self.assertEqual((reused['engine'], reused['parent_task_id'], reused['pi_session_id']),
                         ('pi', first['id'], 'original-pi'))

    def test_saved_agent_unavailable_is_refused_before_queueing(self):
        preferences.update('home', 'pi')

        def availability(engine):
            if engine == 'pi':
                return {'ready': False, 'message': 'Pi fixture unavailable'}
            return dict(READY)

        with patch.object(panel.EXECUTOR, 'submit') as queue, \
             patch.object(engines, 'availability', side_effect=availability):
            with self.assertRaisesRegex(ValueError, 'Pi Agent.*不会切换'):
                panel.submit_task({'prompt': 'fixture', 'model_id': 'home', 'route_mode': 'manual',
                                   'kind': 'edit', 'project': str(self.project),
                                   'files': ['a.py']})
        queue.assert_not_called()
        self.assertFalse((self.project / '.model-desk' / 'runs').exists()
                         and any((self.project / '.model-desk' / 'runs').iterdir()))

    def _server(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _post(self, opener, port, path, body):
        request = Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json', 'X-Panel-Token': panel.TOKEN})
        with opener.open(request, timeout=5) as response:
            return json.load(response)

    def test_http_preference_patch_is_atomic_and_preview_uses_model_default(self):
        server = self._server()
        opener = build_opener(ProxyHandler({}))
        port = server.server_port
        try:
            before = registry.REGISTRY.read_bytes()
            self.assertEqual(registry.public_model(registry.get_model('home'))['default_agent'], 'follow_global')
            with patch.object(panel, 'public_tasks', return_value=[{'model_id': 'home', 'status': 'running'}]):
                patched = self._post(opener, port, '/api/models/home/save', {'default_agent': 'pi'})
            self.assertEqual(patched['default_agent'], 'pi')
            self.assertNotEqual(registry.REGISTRY.read_bytes(), before)
            with self.assertRaises(HTTPError):
                self._post(opener, port, '/api/models/home/save',
                           {'default_agent': 'dsh', 'name': 'must not be mixed'})
            with patch.object(panel.budget_relay, 'daily_state',
                              return_value={'remaining_cny': 100, 'unresolved': False}), \
                 patch.object(engines, 'availability', side_effect=ready_agents):
                preview = self._post(opener, port, '/api/route-preview', {
                    'prompt': 'fixture preview', 'model_id': 'home', 'route_mode': 'manual',
                    'kind': 'edit', 'project': str(self.project), 'files': ['a.py'],
                    'routing': {'category': 'code_small', 'complexity': 'simple', 'expected_calls': 1},
                })
                explicit = self._post(opener, port, '/api/route-preview', {
                    'prompt': 'fixture preview', 'model_id': 'home', 'route_mode': 'manual',
                    'engine': 'dsh', 'kind': 'edit', 'project': str(self.project), 'files': ['a.py'],
                    'routing': {'category': 'code_small', 'complexity': 'simple', 'expected_calls': 1},
                })
            self.assertEqual((preview['engine'], preview['agent_source']), ('pi', 'model'))
            self.assertEqual((explicit['engine'], explicit['agent_source']), ('dsh', 'explicit'))
            atomic = self._post(opener, port, '/api/models/home/agent-default',
                                {'default_agent': 'dsh'})
            self.assertEqual(atomic['default_agent'], 'dsh')
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
