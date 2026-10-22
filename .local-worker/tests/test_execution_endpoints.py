"""Offline endpoint admission, native placeholder and controller tests."""
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_endpoints as ep
import execution_engines as engines
import model_registry as registry
import model_router as router
import panel_server as panel
import workflow_settings


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enterContext(patch.object(registry, 'HERE', self.root))
        self.enterContext(patch.object(registry, 'REGISTRY', self.root / 'models.json'))
        self.settings = {**workflow_settings.DEFAULTS, 'enabled': True, 'paid_allowed': True,
                         'enforce_paid_budget': False, 'enforce_model_call_limit': False}
        self.local = {'id': 'local', 'name': 'Local', 'model': 'qwen', 'provider': 'local',
                      'api_base': 'http://127.0.0.1:1', 'enabled': True, 'default_agent': 'pi',
                      'max_input_tokens': 32768, 'max_output_tokens': 4096}
        self.other = {**self.local, 'id': 'other', 'name': 'Other'}
        registry.write_json(registry.REGISTRY, {'default_model': 'local', 'models': [self.local, self.other]})
        self.enterContext(patch.object(engines, 'availability', return_value={'ready': True, 'name': 'fixture'}))

    def test_dispatch_freezes_access_and_does_not_trust_request_override(self):
        import endpoint_access
        endpoint_access.update('pi', 'project')
        body = {'kind': 'edit', 'prompt': 'fixture', 'model_id': 'local', 'engine': 'pi',
                'project': str(self.root), 'files': ['probe.txt'], 'execution_access_mode': 'full'}
        with patch.object(panel.EXECUTOR, 'submit'), \
             patch.object(workflow_settings, 'snapshot', return_value=self.settings), \
             patch.object(panel, 'connectivity_gate', return_value={'blocked': False, 'checked': False, 'message': '', 'verdict': '', 'check': None}):
            row = panel.submit_task(body)
            self.assertEqual(row['execution_access_mode'], 'project')
            endpoint_access.update('pi', 'full')
            self.assertEqual(registry.read_task(registry.task_dir(row['id']))['execution_access_mode'], 'project')

    def test_missing_store_is_read_only_and_new_native_disabled(self):
        self.assertTrue(ep.snapshot()['endpoints']['pi']['enabled'])
        self.assertFalse(ep.snapshot()['endpoints']['workbuddy']['enabled'])
        self.assertFalse((self.root / ep.CONFIG_NAME).exists())

    def preset(self, **changes):
        return {'preferred_tasks':['代码修改','文档'],'capability_notes':'endpoint fixture',
                'cost_preference':'quality','max_task_complexity':'complex',**changes}

    def test_preset_applies_to_all_models_and_future_models_without_rewriting_registry(self):
        before=registry.REGISTRY.read_bytes();flags=ep.snapshot()
        ep.update('pi',{'preset':self.preset()},self.settings)
        for model in [self.local,self.other,{**self.local,'id':'future'}]:
            effective=ep.apply_preset(model,'pi')
            self.assertEqual(effective['capability_notes'],'endpoint fixture')
            self.assertEqual(effective['routing_preset']['source'],'endpoint')
            self.assertEqual(effective['api_base'],model['api_base'])
        self.assertNotIn('capability_notes',ep.apply_preset(self.local,'dsh'))
        self.assertEqual(registry.REGISTRY.read_bytes(),before)
        self.assertEqual(ep.snapshot()['endpoints']['pi']['enabled'],flags['endpoints']['pi']['enabled'])
        self.assertIsNone(ep.snapshot()['endpoints']['pi']['current_model'])

    def test_preset_validation_and_legacy_clear_are_strict(self):
        original={**self.local,'capability_notes':'legacy','cost_preference':'economy'}
        self.assertEqual(ep.apply_preset(original,'pi')['capability_notes'],'legacy')
        for value in [{},self.preset(cost_preference='free'),self.preset(preferred_tasks='text'),
                      self.preset(max_task_complexity='unlimited'),self.preset(pricing_cny={})]:
            with self.subTest(value=value),self.assertRaises(ValueError):ep.update('pi',{'preset':value},self.settings)
        with self.assertRaises(ValueError):ep.update('pi',{'preset':self.preset(),'enabled':False},self.settings)
        ep.update('pi',{'preset':self.preset()},self.settings)
        ep.update('pi',{'preset':None},self.settings)
        self.assertEqual(ep.apply_preset(original,'pi')['capability_notes'],'legacy')

    def test_actual_agent_preset_controls_auto_route_and_is_snapshotted(self):
        ep.update('pi',{'preset':self.preset()},self.settings)
        ep.update('dsh',{'preset':self.preset(max_task_complexity='simple')},self.settings)
        body={'kind':'chat','prompt':'fixture','model_id':'local','route_mode':'auto',
              'routing':{'category':'code_small','complexity':'medium','input_tokens':100,
                         'output_tokens':100,'expected_calls':1,'eligible_model_ids':['local']}}
        route=router.choose(body,registry.list_models()['models'],self.settings)
        self.assertEqual(route['routing_preset']['endpoint'],'pi')
        self.assertEqual(route['routing_preset']['values']['max_task_complexity'],'complex')
        with self.assertRaisesRegex(ValueError,'复杂度'):
            router.choose({**body,'engine':'dsh'},registry.list_models()['models'],self.settings)
        ep.update('pi',{'preset':self.preset(max_task_complexity='simple')},self.settings)
        self.assertEqual(route['routing_preset']['values']['max_task_complexity'],'complex')

    def test_shared_candidates_publish_effective_preset_and_keep_model_prices(self):
        import brain_gateway
        ep.update('pi', {'preset': self.preset()}, self.settings)
        priced = {**self.local, 'pricing_cny': {'output': 12}}
        self.assertEqual(ep.apply_preset(priced, 'pi')['pricing_cny'], priced['pricing_cny'])
        with patch.object(brain_gateway, '_settings', return_value=self.settings):
            result = brain_gateway._candidates(None, {})
        for row in result['models']:
            self.assertEqual(row['capability_notes'], 'endpoint fixture')
            self.assertEqual(row['routing_preset']['endpoint'], 'pi')
            self.assertEqual(row['preferred_tasks'], self.preset()['preferred_tasks'])
        ep.update('workbuddy', {'preset': self.preset(capability_notes='native endpoint')}, self.settings)
        for name in ['native-a', 'native-b']:
            self.assertEqual(ep.apply_preset({'id': name}, 'workbuddy')['capability_notes'], 'native endpoint')
        raw = json.loads((self.root / ep.CONFIG_NAME).read_text(encoding='utf-8'))
        raw['endpoints']['pi']['preset']['max_task_complexity'] = 'invalid'
        registry.write_json(self.root / ep.CONFIG_NAME, raw)
        self.assertFalse(ep.snapshot()['readable'])
        with self.assertRaises(ValueError):
            ep.apply_preset(self.local, 'pi')

    def test_update_changes_only_endpoint_not_settings_models_or_records(self):
        before = registry.REGISTRY.read_bytes()
        ep.update('pi', {'enabled': False}, self.settings)
        self.assertFalse(ep.snapshot()['endpoints']['pi']['enabled'])
        self.assertTrue(ep.snapshot()['endpoints']['dsh']['enabled'])
        self.assertEqual(before, registry.REGISTRY.read_bytes())
        self.assertFalse((self.root / 'workflow.json').exists())
        self.assertFalse((self.root / '.model-desk').exists())

    def test_strict_updates_and_foreign_model_are_refused(self):
        for key, body in [('unknown', {'enabled': True}), ('pi', {'enabled': 1}),
                          ('pi', {'enabled': False, 'paid_allowed': True}),
                          ('dsh', {'current_model': 'local'}), ('pi', {'current_model': 'missing'}),
                          ('workbuddy', {'current_model': 'local'}), ('pi', {})]:
            with self.subTest(key=key, body=body), self.assertRaises(ValueError):
                ep.update(key, body, self.settings)

    def test_corruption_fails_closed_without_rewriting(self):
        path = self.root / ep.CONFIG_NAME
        for raw in ['{', '[]', '{"schema":1,"endpoints":{"pi":{"enabled":"yes","current_model":null}}}']:
            path.write_text(raw, encoding='utf-8')
            self.assertFalse(ep.snapshot()['readable'])
            with self.assertRaises(ValueError):
                ep.gate('pi', self.local, self.settings)
            self.assertEqual(path.read_text(encoding='utf-8'), raw)

    def test_manual_auto_and_direct_routes_cannot_bypass_disabled_endpoint(self):
        ep.update('pi', {'enabled': False}, self.settings)
        for mode, override in [('manual', {}), ('auto', {}), ('manual', {'engine': 'pi'}),
                               ('manual', {'engine': 'direct'}), ('manual', {'engine': 'aider'})]:
            with self.subTest(mode=mode, override=override), self.assertRaisesRegex(ValueError, '已禁用'):
                router.choose({'kind': 'chat', 'prompt': 'fixture', 'model_id': 'local',
                               'route_mode': mode, **override}, [self.local], self.settings)

    def test_explicit_enabled_engine_is_deliberate_not_silent_fallback(self):
        ep.update('pi', {'enabled': False}, self.settings)
        result = router.choose({'kind': 'chat', 'prompt': 'fixture', 'model_id': 'local',
                                'route_mode': 'manual', 'engine': 'dsh'}, [self.local], self.settings)
        self.assertEqual(result['engine'], 'dsh')
        self.assertEqual(result['agent_source'], 'explicit')

    def test_selected_model_filters_auto_but_keeps_manual_and_existing_task_identity(self):
        ep.update('pi', {'current_model': 'other'}, self.settings)
        result = router.choose({'kind': 'chat', 'prompt': 'fixture', 'route_mode': 'auto'},
                               [self.local, self.other], self.settings)
        self.assertEqual(result['model_id'], 'other')
        result = router.choose({'kind': 'chat', 'prompt': 'fixture', 'route_mode': 'manual',
                                'model_id': 'local'}, [self.local], self.settings)
        self.assertEqual(result['model_id'], 'local')
        self.assertEqual(ep.gate('pi', self.local, self.settings), 'pi')

    def test_enable_does_not_widen_legacy_scope_or_paid_policy(self):
        self.settings['model_scope'] = 'paid_only'
        self.settings['paid_allowed'] = False
        ep.update('pi', {'enabled': True}, self.settings)
        with self.assertRaisesRegex(ValueError, '仅付费'):
            router.choose({'kind': 'chat', 'prompt': 'fixture', 'route_mode': 'manual', 'model_id': 'local'},
                          [self.local], self.settings)
        self.assertFalse(self.settings['paid_allowed'])
        self.assertEqual(self.settings['model_scope'], 'paid_only')

    def test_stale_selection_is_visible_and_no_auto_substitution(self):
        ep.update('pi', {'current_model': 'other'}, self.settings)
        with self.assertRaisesRegex(ValueError, '当前模型'):
            router.choose({'kind': 'chat', 'prompt': 'fixture', 'route_mode': 'auto'}, [self.local], self.settings)
        result = ep.public_rows([self.local], self.settings, {'pi': {'ready': True}})
        self.assertTrue(result['rows'][0]['selection_error'])
        self.assertIsNone(result['rows'][0]['model'])

    def test_workbuddy_card_never_implies_native_queue_or_balance(self):
        ep.update('workbuddy', {'enabled': True}, self.settings)
        result = ep.public_rows([self.local], self.settings, {'pi': {'ready': True}})
        native = next(row for row in result['rows'] if row['id'] == 'workbuddy')
        self.assertFalse(native['ready'])
        self.assertIsNone(native['balance'])
        self.assertEqual(native['billing_source'], 'workbuddy-native-account')
        with self.assertRaisesRegex(ValueError, '互换'):
            ep.gate('workbuddy', self.local, self.settings)

    def test_invalid_model_agent_is_visible_not_silently_reassigned(self):
        invalid = {**self.local, 'default_agent': 'invalid'}
        result = ep.public_rows([invalid], self.settings, {})
        self.assertEqual(result['unassigned_models'], [invalid])

    def test_queued_start_checks_switch_before_spawning_and_keeps_identity(self):
        run = self.root / 'run'; run.mkdir()
        original = {'id': 'fixture', 'model_id': 'local', 'engine': 'pi', 'kind': 'chat', 'status': 'queued'}
        registry.write_json(run / 'task.json', original)
        ep.update('pi', {'enabled': False}, self.settings)
        with patch.object(registry, 'task_dir', return_value=run), patch.object(registry, 'get_model', return_value=self.local), \
             patch.object(workflow_settings, 'snapshot', return_value=self.settings), patch.object(panel.subprocess, 'Popen') as spawn:
            panel.execute_task('fixture')
        spawn.assert_not_called()
        result = registry.read_task(run)
        self.assertEqual((result['status'], result['engine'], result['model_id']), ('failed', 'pi', 'local'))
        self.assertIn('已禁用', result['error'])

    def test_worker_gate_runs_before_thinking_metadata_or_model_requests(self):
        import dsh_worker
        ep.update('pi', {'enabled': False}, self.settings)
        with patch.object(workflow_settings, 'snapshot', return_value=self.settings), \
             patch.object(dsh_worker.thinking_config, 'prepare_for_task') as probe:
            with self.assertRaisesRegex(ValueError, '已禁用'):
                dsh_worker.run_task(self.root, {'engine': 'pi'}, deepcopy(self.local))
        probe.assert_not_called()

    def test_global_http_endpoint_auth_and_atomic_update(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        opener = build_opener(ProxyHandler({}))
        url = f'http://127.0.0.1:{server.server_port}/api/execution-endpoints/pi'
        raw = json.dumps({'enabled': False}).encode()
        with self.assertRaises(HTTPError) as error:
            opener.open(Request(url, data=raw, headers={'Content-Type': 'application/json'}))
        self.assertEqual(error.exception.code, 403)
        with opener.open(Request(url, data=raw, headers={'Content-Type': 'application/json',
                                                       'X-Panel-Token': panel.TOKEN})) as response:
            self.assertFalse(json.load(response)['enabled'])
        # Existing Codex MCP processes know /api/workflow but not the new URL.
        compat = f'http://127.0.0.1:{server.server_port}/api/workflow'
        headers = {'Content-Type': 'application/json', 'X-Panel-Token': panel.TOKEN}
        envelope = {'endpoint_settings': {'id': 'pi', 'values': {'enabled': True}}}
        with opener.open(Request(compat, data=json.dumps(envelope).encode(), headers=headers)) as response:
            self.assertTrue(json.load(response)['enabled'])
        self.assertFalse((self.root / 'workflow.json').exists())
        with self.assertRaises(HTTPError) as mixed:
            opener.open(Request(compat, data=json.dumps({**envelope, 'paid_allowed': True}).encode(), headers=headers))
        self.assertEqual(mixed.exception.code, 400)

    def test_codex_proxy_allows_only_declared_endpoint_controls(self):
        import codex_mcp_server as mcp
        for key in ep.IDS:
            self.assertEqual(mcp.validate_request('/api/execution-endpoints/'+key, 'POST'), '/api/execution-endpoints/'+key)
        with self.assertRaises(mcp.PanelError):
            mcp.validate_request('/api/execution-endpoints/unknown', 'POST')

    def test_dsh_candidates_and_common_startup_signal_exclude_disabled_endpoint(self):
        import brain_gateway
        ep.update('pi', {'enabled': False}, self.settings)
        with patch.object(brain_gateway, '_settings', return_value=self.settings):
            result = brain_gateway._candidates(None, {})
        self.assertEqual(result['selectable_model_ids'], [])
        for row in result['models']:
            self.assertFalse(row['dispatch_ready'])
            self.assertFalse(row['endpoint_allowed'])
            self.assertIn('已禁用', row['selectable_reason'])


if __name__ == '__main__':
    unittest.main()
