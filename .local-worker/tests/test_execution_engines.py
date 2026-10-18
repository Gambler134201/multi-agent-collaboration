"""Offline agent-selection, persistence, routing and lifecycle regression tests."""
import json
import contextlib
import io
from pathlib import Path
import sys
from unittest.mock import patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_engines as engines
import dispatch_task
import executor_adapter
import model_agent_defaults as model_defaults
import model_registry as registry
import panel_server as panel
import pi_worker
import workflow_settings as settings
import delegation_policy as policy
import stage_store
from test_stage_workflow import Base


class EngineTests(Base):
    def setUp(self):
        super().setUp()
        settings.save_settings({'enabled': True, 'paid_allowed': False, 'model_scope': 'local_only',
                                'enforce_paid_budget': False, 'enforce_model_call_limit': False})

    def submit(self, **kwargs):
        body = {'prompt': 'fixture task', 'model_id': 'home', 'route_mode': 'manual', 'kind': 'edit',
                'project': str(self.project), 'files': ['a.py'], 'source': 'codex', **kwargs}
        with patch.object(panel.EXECUTOR, 'submit'), patch.object(engines, 'availability', return_value={'ready': True}):
            return panel.submit_task(body)

    def test_old_settings_keep_dsh_and_switch_changes_only_agent(self):
        file = registry.HERE / 'workflow.json'
        old = json.loads(file.read_text(encoding='utf-8'))
        old.pop('execution_agent')
        registry.write_json(file, old)
        self.assertEqual(settings.snapshot()['execution_agent'], 'dsh')
        before = settings.snapshot()
        after = settings.save_settings({'execution_agent': 'pi'})
        self.assertEqual(after['execution_agent'], 'pi')
        self.assertEqual({k: after[k] for k in settings.FIELDS if k != 'execution_agent'},
                         {k: before[k] for k in settings.FIELDS if k != 'execution_agent'})
        for bad in ('unknown', 'aider', '', None, True, ['pi']):
            with self.assertRaises(ValueError): settings.save_settings({'execution_agent': bad})
        self.assertEqual(settings.snapshot()['execution_agent'], 'pi')

    def test_default_and_explicit_agent_and_independent_review(self):
        self.assertEqual(self.submit()['engine'], 'dsh')
        settings.save_settings({'execution_agent': 'pi'})
        task = self.submit()
        self.assertEqual(task['engine'], 'pi')
        self.assertEqual(self.submit(engine='dsh')['engine'], 'dsh')
        self.assertEqual(task['project'], str(self.project.resolve()))
        self.assertTrue(registry.task_dir(task['id']).is_relative_to(self.project / '.model-desk'))
        self.assertTrue(policy.is_delegated(task))
        self.assertIsNone(policy.load(registry.task_dir(task['id'])))

    def test_invalid_setting_is_visible_and_fails_closed(self):
        registry.write_json(registry.HERE / 'workflow.json', {**settings.DEFAULTS, 'execution_agent': 'typo'})
        snapshot = settings.snapshot()
        self.assertFalse(snapshot['readable'])
        with self.assertRaises(ValueError): self.submit()
        with self.assertRaises(ValueError): engines.availability('typo')
        selected, public = engines.public_selection(snapshot)
        self.assertEqual(selected, 'invalid')
        self.assertFalse(public['ready'])

    def test_unavailable_pi_does_not_fall_back_or_queue(self):
        settings.save_settings({'execution_agent': 'pi'})
        with patch.object(panel.EXECUTOR, 'submit') as queued, \
             patch.object(engines, 'availability', return_value={'ready': False}):
            with self.assertRaisesRegex(ValueError, 'Pi Agent.*不会切换'):
                panel.submit_task({'prompt': 'fixture', 'project': str(self.project), 'model_id': 'home'})
        queued.assert_not_called()

    def test_existing_queue_is_pinned_after_default_switch(self):
        task = self.submit(engine='pi')
        settings.save_settings({'execution_agent': 'dsh'})
        with patch.object(panel.subprocess, 'Popen') as process:
            process.return_value.pid = 4242
            process.return_value.wait.return_value = 0
            panel.execute_task(task['id'])
        argv = process.call_args.args[0]
        self.assertTrue(argv[1].endswith('pi_worker.py'), argv)
        self.assertEqual(registry.read_task(registry.task_dir(task['id']))['engine'], 'pi')

    def test_queued_task_pins_the_models_saved_preference(self):
        model_defaults.update('home', 'pi')
        task = self.submit()
        self.assertEqual(task['engine'], 'pi')
        model_defaults.update('home', 'dsh')
        settings.save_settings({'execution_agent': 'dsh'})
        with patch.object(panel.subprocess, 'Popen') as process:
            process.return_value.pid = 4242
            process.return_value.wait.return_value = 0
            panel.execute_task(task['id'])
        argv = process.call_args.args[0]
        self.assertTrue(argv[1].endswith('pi_worker.py'), argv)
        self.assertEqual(registry.read_task(registry.task_dir(task['id']))['engine'], 'pi')

    def pi_parent(self):
        run = self.task('pi-parent', engine='pi', pi_session_id='exact-pi-session',
                        pi_session_file=str(self.project / '.model-desk/runs/pi-parent/pi-session.jsonl'),
                        connection_fingerprint='pi-fingerprint')
        return registry.read_task(run)

    def test_continuation_uses_original_agent_and_exact_session(self):
        parent = self.pi_parent()
        settings.save_settings({'execution_agent': 'dsh'})
        child = self.submit(continue_task=parent['id'])
        self.assertEqual(child['engine'], 'pi')
        self.assertEqual(child['pi_session_id'], parent['pi_session_id'])
        self.assertEqual(child['pi_session_file'], parent['pi_session_file'])
        self.assertNotIn('dsh_session_id', child)
        with self.assertRaisesRegex(ValueError, '不能切换执行 Agent'):
            self.submit(continue_task=parent['id'], engine='dsh')

    def test_rework_keeps_pi_with_a_fresh_session(self):
        parent = self.pi_parent()
        run = registry.task_dir(parent['id'])
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py']})
        policy.request_rework(run, 'main', ['c1'], 'defect fixture', 'a.py only')
        with self.assertRaisesRegex(ValueError, '整改保持原执行 Agent'):
            self.submit(rework_of=parent['id'], engine='dsh')
        self.assertFalse((policy.load(run).get('rework') or {}).get('dispatched_task_id'))
        child = self.submit(rework_of=parent['id'])
        self.assertEqual(child['engine'], 'pi')
        self.assertNotIn('pi_session_id', child)
        self.assertNotIn('connection_fingerprint', child)
        self.assertEqual(child['delegation']['rework_count'], 1)

    def test_stage_uses_selected_agent_and_keeps_kind_scope_acceptance(self):
        settings.save_settings({'execution_agent': 'pi'})
        _, stage = self.goal_with_stage()
        with patch.object(panel.EXECUTOR, 'submit'), patch.object(engines, 'availability', return_value={'ready': True}):
            task = panel.dispatch_stage(stage['id'], {'prompt': 'stage fixture'})['task']
        self.assertEqual(task['engine'], 'pi')
        self.assertEqual(task['files'], ['a.py'])
        self.assertEqual(task['kind'], 'edit')
        self.assertEqual(task['route']['requirements']['acceptance'], stage['acceptance'])

    def test_recovery_keeps_the_original_agent_without_new_record(self):
        task = self.submit(engine='pi')
        registry.patch_task(registry.task_dir(task['id']), status='recovery_pending')
        settings.save_settings({'execution_agent': 'dsh'})
        with patch.object(panel.EXECUTOR, 'submit') as queue:
            restored = panel.recover_task(task['id'], {'confirm': True})
        self.assertEqual(restored['task_id'], task['id'])
        self.assertFalse(restored['creates_new_record'])
        self.assertEqual(registry.read_task(registry.task_dir(task['id']))['engine'], 'pi')
        queue.assert_called_once()

    def test_restart_marks_pi_queue_pending_recovery_without_redispatch(self):
        task = self.submit(engine='pi')
        with patch.object(panel.EXECUTOR, 'submit') as queue:
            panel.recover_pending_tasks()
        queue.assert_not_called()
        record = registry.read_task(registry.task_dir(task['id']))
        self.assertEqual(record['status'], 'recovery_pending')
        self.assertEqual(record['engine'], 'pi')

    def test_pi_capability_delegates_are_real_and_subagents_stay_denied(self):
        adapter = executor_adapter.adapter_for('pi', transport=pi_worker.worker_transport())
        self.assertTrue(adapter.callable_now('dispatch'))
        self.assertTrue(adapter.callable_now('session'))
        self.assertIsInstance(adapter, executor_adapter.PiCapabilityAdapter)
        for capability, delegate in executor_adapter.ENGINE_DELEGATES['pi'].items():
            self.assertTrue(callable(getattr(pi_worker, delegate.split('.')[-1])), capability)
        report = adapter.report()
        self.assertFalse(report['subagent_ready'])
        self.assertIsNone(report['subagent_lifecycle_schema'])
        with self.assertRaises(executor_adapter.SubagentDenied): adapter.subagent()
        with self.assertRaises(ValueError): pi_worker.run_task(None, {'engine': 'dsh'}, {})

    def test_pi_cannot_review_its_own_result(self):
        parent = self.pi_parent()
        with self.assertRaises((ValueError, PermissionError)):
            policy.request_rework(registry.task_dir(parent['id']), 'pi', ['c1'], 'self review', 'fixture')

    def test_pi_requires_explicit_project_and_keeps_paid_scope_guards(self):
        with self.assertRaisesRegex(ValueError, '选择项目目录'):
            self.submit(engine='pi', kind='chat', project='', files=[])
        run = registry.HERE / 'worker-fixture'
        run.mkdir()
        model = {**registry.get_model('home'), 'api_key': 'mock-only'}
        state = {'id': 'worker-fixture', 'engine': 'pi', 'project': str(self.project),
                 'kind': 'chat', 'files': [], 'timeout': 0, 'route': {'mode': 'manual'}}
        for scope, paid, reason in [('paid_only', False, '执行模型范围'), ('all', True, '付费执行未开启')]:
            settings.save_settings({'model_scope': scope})
            candidate = {**model, 'provider': 'custom' if paid else 'local'}
            with patch('thinking_config.prepare_for_task', return_value=({}, {'status': 'fixture'})), \
                 patch('thinking_config.validate'), patch.object(pi_worker, 'prepare') as start:
                with self.assertRaisesRegex(ValueError, reason): pi_worker.run_task(run, state, candidate)
            start.assert_not_called()

    def test_cli_default_is_omitted_and_continuation_cannot_override_agent(self):
        prompt = registry.HERE / 'cli-prompt.txt'
        prompt.write_text('CLI fixture', encoding='utf-8')
        calls = []
        parent = {'id': 'pi-parent', 'engine': 'pi', 'model_id': 'home', 'kind': 'chat',
                  'project': str(self.project), 'files': []}
        def request(path, body=None):
            calls.append((path, body))
            return {'task': parent} if path == '/api/tasks/pi-parent' else {'model_id': 'home'}
        argv = ['dispatch_task.py', '--task-file', str(prompt), '--preview']
        with patch.object(dispatch_task, 'connect_panel', return_value=request), \
             patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch_task.main(), 0)
        self.assertNotIn('engine', calls[-1][1])
        with patch.object(dispatch_task, 'connect_panel', return_value=request), \
             patch.object(sys, 'argv', [*argv, '--continue-task', 'pi-parent']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dispatch_task.main(), 0)
        self.assertEqual(calls[-1][1]['engine'], 'pi')
        calls.clear()
        with patch.object(dispatch_task, 'connect_panel', return_value=request), \
             patch.object(sys, 'argv', [*argv, '--continue-task', 'pi-parent', '--engine', 'dsh']):
            with self.assertRaisesRegex(ValueError, '不能切换执行 Agent'): dispatch_task.main()
        self.assertEqual(calls, [('/api/tasks/pi-parent', None)])


if __name__ == '__main__': unittest.main()
