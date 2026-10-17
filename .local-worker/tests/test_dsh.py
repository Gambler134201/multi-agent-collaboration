import json
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dsh_worker
import dispatch_task
import model_registry as registry
import model_capabilities as caps
import panel_server as panel
import executor_adapter


class DshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        self.saved_worker_home = dsh_worker.HERE
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        # Worker patches must land in the sandbox too, never in the real install home.
        dsh_worker.HERE = registry.HERE
        registry.write_json(registry.HERE / 'settings.json', {'model': 'qwen-test', 'api_base': 'http://127.0.0.1:1234/v1',
            'api_key_env': '', 'max_input_tokens': 32768, 'max_output_tokens': 4096})
        registry.list_models()

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        dsh_worker.HERE = self.saved_worker_home
        self.temp.cleanup()

    def test_profile_has_no_plaintext_key_or_paid_fallback(self):
        model = {**registry.get_model('home'), 'api_key': 'private-test-key'}
        rows = dsh_worker.build_patch(model, 'chat')
        text = json.dumps(rows)
        self.assertNotIn('private-test-key', text)
        adapter = next(r for r in rows if r['id'] == 'llm-pi-ai')
        self.assertEqual(list(adapter['config']['providers']), ['desk-worker'])
        self.assertEqual(adapter['config']['providers']['desk-worker']['retryPolicy']['maxRetries'], 0)
        for name in ['session-log-deepseek', 'session-telemetry-otel', 'llm-deepseek', 'llm-deepseek-account']:
            self.assertTrue(next(r for r in rows if r['id'] == name)['disabled'])
        self.assertEqual(next(r for r in rows if r['id'] == 'sandbox-policy')['config']['mode'], 'read-only')

    def test_completion_keeps_tool_identity_and_file(self):
        project = Path(self.temp.name)
        started = dsh_worker.tool_update({}, {'title': 'edit', 'status': 'in_progress',
            'rawInput': {'file_path': str(project / 'sample.py')}}, project)
        finished = dsh_worker.tool_update(started, {'title': 'tool call', 'status': 'completed'}, project)
        self.assertEqual(finished['title'], 'edit')
        self.assertEqual(finished['file'], 'sample.py')
        self.assertEqual(finished['status'], 'completed')

    def test_full_access_edit_and_read_only_chat_preserve_scope(self):
        model = registry.get_model('home')
        for kind, mode in [('edit', 'danger-full-access'), ('chat', 'read-only')]:
            rows = dsh_worker.build_patch(model, kind)
            self.assertEqual(next(r for r in rows if r['id'] == 'sandbox-policy')['config']['mode'], mode)
        self.assertEqual(dsh_worker.availability()['edit_sandbox_mode'], 'danger-full-access')
        self.assertEqual(dsh_worker.availability()['chat_sandbox_mode'], 'read-only')

    def test_old_edit_session_is_not_silently_upgraded(self):
        model = registry.get_model('home')
        preset = {'id': 'native', 'revision': 'permission-test'}
        def old_fingerprint(kind):
            old_data = [model['id'], model['api_base'], model['model'], model.get('max_input_tokens'), model.get('max_output_tokens'), kind, str(registry.HERE), preset['id'], preset['revision']]
            return hashlib.sha256(json.dumps(old_data).encode()).hexdigest()
        self.assertEqual(old_fingerprint('chat'), dsh_worker.fingerprint(model, 'chat', registry.HERE, preset))
        self.assertNotEqual(old_fingerprint('edit'), dsh_worker.fingerprint(model, 'edit', registry.HERE, preset))
        state = {'id':'old-edit', 'kind':'edit', 'project':str(registry.HERE), 'files':[], 'connection_fingerprint':old_fingerprint('edit')}
        with patch.object(dsh_worker, 'availability', return_value={'ready': True}), patch.object(dsh_worker.preset_runtime, 'load', return_value=preset):
            with self.assertRaisesRegex(ValueError, '权限.*创建新任务'):
                dsh_worker._run_task(registry.HERE, state, model, object())

    def test_resume_retains_scope_and_rejects_model_or_file_changes(self):
        parent = registry.task_dir('original')
        parent.mkdir(parents=True)
        registry.patch_task(parent, id='original', model_id='home', kind='edit', engine='dsh', status='completed',
            project=str(registry.HERE), files=['sample.py'], dsh_session_key='desk-original', dsh_session_id='native-id')
        body = {'model_id': 'home', 'kind': 'edit', 'engine': 'dsh', 'project': str(registry.HERE),
                'files': ['sample.py'], 'prompt': 'continue', 'continue_task': 'original'}
        with patch.object(panel.EXECUTOR, 'submit') as queued:
            child = panel.submit_task(body)
            self.assertEqual(child['dsh_session_key'], 'desk-original')
            self.assertEqual(child['parent_task_id'], 'original')
            queued.assert_called_once()
            with self.assertRaises(ValueError):
                panel.submit_task({**body, 'files': ['different.py']})
            with self.assertRaises(ValueError):
                panel.submit_task({**body, 'engine': 'aider'})

    def test_agent_prompt_preserves_task_text_and_adds_index(self):
        run = registry.task_dir('prompt-check')
        run.mkdir(parents=True)
        base = '用户约束：不要改动其它文件。\n验收：运行 unittest 且全部通过。'
        (run / 'prompt.txt').write_text(base, encoding='utf-8')
        project = registry.HERE
        (project / 'a.py').write_text('def one():\n    pass\n', encoding='utf-8')
        index_path = dsh_worker.context_manager.default_index_path(run)
        index = dsh_worker.context_manager.build_index(project, ['a.py'])
        dsh_worker.context_manager.write_index(index, index_path)
        state = {'kind': 'edit', 'files': ['a.py'], 'test_command': 'python -m unittest'}
        prompt, base_text = dsh_worker.build_agent_prompt(run, state, index=index, index_path=index_path)
        self.assertEqual(base_text, base)
        self.assertTrue(prompt.startswith(base))
        self.assertIn('用户约束：不要改动其它文件。', prompt)
        self.assertIn('验收：运行 unittest 且全部通过。', prompt)
        self.assertIn(dsh_worker.context_manager.EXECUTION_CONSTRAINTS, prompt)
        self.assertIn('验收测试由控制端运行', prompt)
        self.assertNotIn(dsh_worker.context_manager.SELF_TEST_NOTE, prompt)
        self.assertIn('a.py', prompt)
        self.assertIn('project-index.json', prompt)
        fields = dsh_worker.context_manager.prompt_fields(base, prompt)
        self.assertTrue(fields['prompt_preserved'])
        self_test_prompt, self_test_base = dsh_worker.build_agent_prompt(
            run, {'kind': 'edit', 'files': ['a.py']})
        self.assertEqual(self_test_base, base)
        self.assertTrue(self_test_prompt.startswith(base))
        self.assertEqual(self_test_prompt.count(dsh_worker.context_manager.SELF_TEST_NOTE), 1)
        self.assertNotIn('验收测试由控制端运行', self_test_prompt)
        self.assertIn('自测通过仍需主脑独立验收', self_test_prompt)
        chat_prompt, _ = dsh_worker.build_agent_prompt(run, {'kind': 'chat', 'files': []})
        self.assertTrue(chat_prompt.startswith(base))
        self.assertIn(dsh_worker.context_manager.READONLY_NOTE, chat_prompt)
        self.assertNotIn('允许修改的文件', chat_prompt)
        self.assertNotIn(dsh_worker.context_manager.SELF_TEST_NOTE, chat_prompt)
        with self.assertRaises(ValueError):
            dsh_worker.context_manager.verify_prompt('truncated', base)

    def test_invalid_engine_and_project_are_rejected(self):
        with self.assertRaises(ValueError):
            panel.submit_task({'model_id': 'home', 'kind': 'edit', 'engine': 'direct', 'prompt': 'test'})
        with self.assertRaises(ValueError):
            panel.submit_task({'model_id': 'home', 'kind': 'chat', 'engine': 'dsh', 'prompt': 'test', 'project': '../relative'})

    def test_task_level_request_output_tokens_is_recorded_but_not_a_model_preset(self):
        before = registry.get_model('home')['max_output_tokens']
        with patch.object(panel.EXECUTOR, 'submit'), \
             patch.object(dsh_worker, 'availability', return_value={'ready': True}):
            task = panel.submit_task({'model_id': 'home', 'kind': 'chat', 'engine': 'dsh',
                                      'prompt': 'hello', 'request_output_tokens': 321})
            self.assertEqual(task['request_output_tokens'], 321)
            with self.assertRaises(ValueError):
                panel.submit_task({'model_id': 'home', 'kind': 'chat', 'engine': 'dsh',
                                   'prompt': 'hello', 'request_output_tokens': True})
            with self.assertRaises(ValueError):
                panel.submit_task({'model_id': 'home', 'kind': 'chat', 'engine': 'dsh',
                                   'prompt': 'hello', 'request_output_tokens': 0})
        self.assertEqual(registry.get_model('home')['max_output_tokens'], before)

    def test_capability_presets_survive_other_edits_and_stay_public(self):
        body = {'name': 'worker', 'model': 'test', 'provider': 'custom', 'api_base': 'https://example.com/v1',
                'api_key': 'private-test-key', 'preferred_tasks': ['文档', '测试'],
                'capability_notes': '只处理简单任务', 'cost_preference': 'economy'}
        row = registry.upsert_model(body)
        for key in ['preferred_tasks', 'capability_notes', 'cost_preference']:
            body.pop(key)
        updated = registry.upsert_model(body, row['id'])
        self.assertEqual(updated['preferred_tasks'], ['文档', '测试'])
        self.assertEqual(updated['capability_notes'], '只处理简单任务')
        self.assertEqual(updated['cost_preference'], 'economy')
        self.assertNotIn('api_key', updated)
        self.assertNotIn('secret', updated)

    # ------------------------------------------------- subagent capability (D1)

    def test_subagent_plugins_stay_disabled_by_default(self):
        model = registry.get_model('home')
        rows = dsh_worker.build_patch(model, 'chat')
        for name in ['tool-subagent', 'tool-subagent-fork', 'tool-subagent-control', 'tool-subagent-list-agents']:
            self.assertTrue(next(r for r in rows if r['id'] == name)['disabled'], name)
        self.assertFalse(next(r for r in rows if r['id'] == 'subagent-model-selection-settings')['config']['enabled'])
        forced = dsh_worker.build_patch(model, 'chat', subagent=True)
        tool = next(r for r in forced if r['id'] == 'tool-subagent')
        self.assertFalse(tool['disabled'])
        # The child is one-level, foreground-only and pinned to the parent route;
        # the fork backend (parent history seed) is never enabled.
        self.assertEqual(tool['config']['provider'], 'spawn')
        self.assertEqual(tool['config']['backgroundMode'], 'one-shot')
        self.assertFalse(tool['config']['enableRunInBackground'])
        self.assertEqual(tool['config']['maxDepth'], 1)
        self.assertEqual(tool['config']['agentOptions'], {'provider': 'desk-worker', 'model': model['model']})
        for name in ['tool-subagent-fork', 'tool-subagent-control', 'tool-subagent-list-agents']:
            self.assertTrue(next(r for r in forced if r['id'] == name)['disabled'], name)

    def test_home_model_gets_a_recorded_denial_and_dispatch_stops(self):
        model = registry.get_model('home')
        run = registry.HERE / 'subagent-denied'
        with patch.object(dsh_worker.executor_adapter, 'subagent_gate',
                          side_effect=dsh_worker.executor_adapter.SubagentDenied('子代理未获允许：尚无实测证据')):
            decision = dsh_worker.subagent_decision(model, run)
        self.assertEqual(decision['decision'], 'deny')
        self.assertEqual(decision['policy'], 'deny')
        self.assertTrue(decision['local'])
        recorded = json.loads((run / 'task.json').read_text(encoding='utf-8'))['subagent_capability']
        self.assertEqual(recorded['decision'], 'deny')
        with patch.object(dsh_worker.executor_adapter, 'subagent_gate',
                          side_effect=dsh_worker.executor_adapter.SubagentDenied('子代理未获允许：尚无实测证据')):
            with self.assertRaisesRegex(dsh_worker.executor_adapter.SubagentDenied, '未获允许'):
                dsh_worker.executor_adapter.subagent_gate(model)

    def test_dispatch_never_enables_subagent_plugins_for_unverified_model(self):
        run = registry.HERE / 'subagent-patch-check'
        run.mkdir(parents=True, exist_ok=True)
        (run / 'prompt.txt').write_text('只读任务', encoding='utf-8')
        model = dict(registry.get_model('home'))
        model.update({'id': 'cloudish', 'provider': 'custom', 'subagent': {'policy': 'allow', 'evidence': []}})
        state = {'id': 'subagent-patch-check', 'kind': 'chat', 'project': str(registry.HERE), 'files': [],
                 'timeout': 30, 'model_id': 'cloudish'}
        relay = SimpleNamespace(url='http://127.0.0.1:9/v1', token='relay-token')
        real_patch = registry.patch_task
        with patch.object(dsh_worker, 'availability', return_value={'ready': True}), \
             patch.object(dsh_worker.preset_runtime, 'load', return_value={'id': 'standard', 'revision': 'r', 'source': 's', 'overrides': [], 'policy_overrides': [], 'declaration': {'id': 'agent-preset', 'config': {'plugins': []}}}), \
             patch.object(dsh_worker.registry, 'patch_task', side_effect=real_patch), \
             patch('sys.stdout', new=io.StringIO()):
            # The mocked ACP runner can never complete, but the patch written
            # before the process start is the enforcement point under test.
            dsh_worker._run_task(run, state, model, relay)
        patches = list((registry.HERE / 'dsh-home' / 'worker-patches').glob('*.yml'))
        self.assertEqual(len(patches), 1)
        text = patches[0].read_text(encoding='utf-8')
        for name in ['tool-subagent', 'tool-subagent-fork', 'tool-subagent-control', 'tool-subagent-list-agents']:
            self.assertRegex(text, rf'id: {name}\n\s+disabled: true', name)
        self.assertIn('enabled: false', text)
        self.assertEqual(json.loads((run / 'task.json').read_text(encoding='utf-8'))['subagent_capability']['decision'], 'deny')

    def test_public_model_payload_carries_no_secret_and_denies_by_default(self):
        body = {'name': 'worker', 'model': 'test', 'provider': 'custom', 'api_base': 'https://example.com/v1',
                'api_key': 'private-test-key', 'agent_preset': 'standard', 'thinking_level': 'default'}
        row = registry.upsert_model(body)
        self.assertNotIn('secret', row)
        self.assertEqual(row['subagent']['decision'], 'deny')
        self.assertEqual(row['subagent']['state'], 'unverified')
        self.assertEqual(row['subagent']['policy'], 'unset')
        body.pop('subagent_evidence', None)
        body['subagent_policy'] = 'allow'
        updated = registry.upsert_model(body, row['id'])
        self.assertEqual(updated['subagent']['policy'], 'allow')
        self.assertEqual(updated['subagent']['decision'], 'deny')
        self.assertNotIn('private-test-key', json.dumps(updated, ensure_ascii=False))

    # ------------------------------------- D1 rework: identity / evidence strictness

    def test_public_model_identity_matches_the_worker_identity(self):
        row = registry.get_model('home')
        public = registry.public_model(row)
        worker_identity = executor_adapter.model_identity(row)
        self.assertEqual(public['subagent']['identity']['version'], worker_identity['version'])
        self.assertEqual(public['subagent']['identity']['model'], row['model'])
        self.assertEqual(public['subagent']['identity']['agent_preset'], worker_identity['agent_preset'])

    def test_capability_models_keep_raw_evidence_without_secrets(self):
        identity = caps.evidence_identity(executor='dsh', model='test', preset='standard', version='0.2.0-rc.2')
        registry.upsert_model({'name': 'w', 'model': 'test', 'provider': 'custom',
                               'api_base': 'https://example.com/v1', 'api_key': 'private-test-key',
                               'subagent_policy': 'allow',
                               'subagent_evidence': {'state': 'supported', 'measured': True,
                                                     'tested_at': '2026-01-01T00:00:00+00:00',
                                                     'identity': identity, 'test_id': 'offline'}})
        rows = registry.capability_models()
        cloud = next(row for row in rows if row['model'] == 'test')
        self.assertEqual(cloud['subagent']['policy'], 'allow')
        self.assertEqual(len(cloud['subagent']['evidence']), 1)
        text = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn('private-test-key', text)
        self.assertNotIn('secret', text)

    def test_upsert_rejects_incomplete_evidence_instead_of_storing_allow(self):
        with self.assertRaisesRegex(ValueError, '实测记录不完整'):
            registry.upsert_model({'name': 'w', 'model': 'test', 'provider': 'custom',
                                   'api_base': 'https://example.com/v1',
                                   'subagent_policy': 'allow',
                                   'subagent_evidence': {'state': 'supported', 'measured': True}})

    def test_capability_view_uses_raw_evidence_not_the_public_stub(self):
        identity = executor_adapter.model_identity({'model': 'test', 'agent_preset': 'standard'})
        registry.upsert_model({'name': 'w', 'model': 'test', 'provider': 'custom',
                               'api_base': 'https://example.com/v1',
                               'subagent_policy': 'allow',
                               'subagent_evidence': {'state': 'supported', 'measured': True,
                                                     'tested_at': '2026-01-01T00:00:00+00:00',
                                                     'identity': identity, 'test_id': 'offline'}})
        view = executor_adapter.capabilities_view(registry.capability_models())
        row = next(item for item in view['models'] if item['model'] == 'test')
        public = registry.public_model(next(m for m in registry.capability_models() if m['model'] == 'test'))
        self.assertEqual(row['decision'], public['subagent']['decision'])
        self.assertIn(row['decision'], (caps.VERDICT_ALLOW, caps.VERDICT_DENY))

    def test_fingerprint_notices_replaced_evidence(self):
        base = registry.get_model('home')
        identity = caps.evidence_identity(executor='dsh', model=base['model'], preset='native',
                                          version='0.2.0-rc.2')

        def granted(test_id):
            return {**base, 'subagent': {'policy': 'allow', 'evidence': [
                {'state': 'supported', 'measured': True, 'tested_at': '2026-01-01T00:00:00+00:00',
                 'identity': identity, 'test_id': test_id}]}}

        first = dsh_worker.fingerprint(granted('run-a'), 'chat', registry.HERE)
        second = dsh_worker.fingerprint(granted('run-b'), 'chat', registry.HERE)
        self.assertNotEqual(first, second)
        # Same content, different bookkeeping timestamp: identity stays stable.
        same = granted('run-a')
        same['subagent']['updated_at'] = 'later'
        self.assertEqual(first, dsh_worker.fingerprint(same, 'chat', registry.HERE))

    def test_fingerprint_includes_executor_version_for_delegated_rows(self):
        base = registry.get_model('home')
        identity = caps.evidence_identity(executor='dsh', model=base['model'], preset='native',
                                          version='0.2.0-rc.2')
        granted = {**base, 'subagent': {'policy': 'allow', 'evidence': [
            {'state': 'supported', 'measured': True, 'tested_at': '2026-01-01T00:00:00+00:00',
             'identity': identity, 'test_id': 'x'}]}}
        with patch.object(executor_adapter, 'installed_executor_version', return_value='1.0.0'):
            first = dsh_worker.fingerprint(granted, 'chat', registry.HERE)
        with patch.object(executor_adapter, 'installed_executor_version', return_value='2.0.0'):
            second = dsh_worker.fingerprint(granted, 'chat', registry.HERE)
        self.assertNotEqual(first, second)
        # A default capability block keeps the legacy fingerprint (no session churn).
        default = {**base, 'subagent': {'policy': 'unset', 'evidence': []}}
        with patch.object(executor_adapter, 'installed_executor_version', return_value='1.0.0'):
            a = dsh_worker.fingerprint(default, 'chat', registry.HERE)
        with patch.object(executor_adapter, 'installed_executor_version', return_value='2.0.0'):
            b = dsh_worker.fingerprint(default, 'chat', registry.HERE)
        self.assertEqual(a, b)


class ControllerWaitTests(unittest.TestCase):
    """A transiently missing task record must not crash or re-dispatch."""

    def test_transient_none_is_retried_then_succeeds(self):
        calls = []

        def request(path):
            calls.append(path)
            if len(calls) < 3:
                return {'task': None, 'transient': True}
            return {'task': {'id': 't1', 'status': 'completed'}}

        with patch.object(dispatch_task, 'TASK_READ_DELAY', 0):
            task = dispatch_task.read_task_state(request, 't1')
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(path == '/api/tasks/t1' for path in calls))

    def test_persistent_none_raises_a_bounded_timeout_not_an_attributeerror(self):
        def request(path):
            return {'task': None, 'error': '任务记录暂时不可读'}

        with patch.object(dispatch_task, 'TASK_READ_ATTEMPTS', 3), \
             patch.object(dispatch_task, 'TASK_READ_DELAY', 0):
            with self.assertRaises(ValueError) as caught:
                dispatch_task.read_task_state(request, 't1')
        message = str(caught.exception)
        self.assertIn('未重新派发', message)
        self.assertIn('任务记录暂时不可读', message)
        self.assertNotIsInstance(caught.exception, AttributeError)

    def test_a_wrong_task_id_is_treated_as_a_transient_miss(self):
        def request(path):
            return {'task': {'id': 'other', 'status': 'running'}}

        with patch.object(dispatch_task, 'TASK_READ_ATTEMPTS', 2), \
             patch.object(dispatch_task, 'TASK_READ_DELAY', 0):
            with self.assertRaises(ValueError):
                dispatch_task.read_task_state(request, 't1')


if __name__ == '__main__':
    unittest.main(verbosity=2)
