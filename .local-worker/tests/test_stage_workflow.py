"""Offline tests for persistent goals/stages, cross-session handovers and rework.

No model call, no paid generation, no service restart.  SQLite, the CLI modules
and the panel HTTP handler all run against a temporary registry directory.
"""
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delegation_policy as policy
import model_registry as registry
import model_router
import panel_server as panel
import project_storage
import stage_store
import task_handover
import token_meter
import workflow_settings as settings


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.project = registry.HERE / 'project'
        self.project.mkdir()
        (self.project / 'a.py').write_text('def one():\n    return 1\n', encoding='utf-8')
        (self.project / 'b.py').write_text('def two():\n    return 2\n', encoding='utf-8')
        registry.write_json(registry.HERE / 'settings.json', {
            'model': 'qwen-test', 'api_base': 'http://127.0.0.1:1234/v1', 'api_key_env': '',
            'max_input_tokens': 32768, 'max_output_tokens': 4096})
        registry.list_models()

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def task(self, name, acceptance=('A', 'B'), delegated=True, files=('a.py',), **fields):
        run = registry.task_dir(name)
        row = {'id': name, 'status': 'completed', 'kind': 'edit', 'engine': 'dsh',
               'model_id': 'home', 'model': 'qwen-test', 'project': str(self.project),
               'files': list(files), 'changed_files': list(files), 'model_calls': 0,
               'route': {'mode': 'manual', 'requirements': {'acceptance': list(acceptance)}}}
        row.update(fields)
        if delegated:
            row.setdefault('delegation', {'schema': 1, 'mode': 'outcome', 'rework_count': 0,
                                          'inherited_rework_count': 0})
        registry.patch_task(run, **row)
        return run

    def accept_task(self, run, notes='independent verification'):
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 0, 'output': 'ok'}})
        record = policy.load(run)
        rows = [{'id': item['id'], 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'independent fixture run', 'exit_code': 0}]}
                for item in record['criteria']]
        return policy.accept(run, 'main', rows, notes)

    def goal_with_stage(self, depends=(), files=('a.py',), title='stage', fixed_model='home'):
        goal = stage_store.create_goal('Goal ' + title, 'persistent goal', str(self.project))
        stage = stage_store.add_stage(goal['id'], title, 'deliver ' + title,
                                      depends_on=list(depends), files=list(files),
                                      acceptance=['criterion one', 'criterion two'],
                                      candidates=['home'], fixed_model=fixed_model)
        return goal, stage

    def claim_and_accept(self, stage, name):
        self.task(name)
        stage_store.claim_dispatch(stage['id'], name)
        self.accept_task(registry.task_dir(name))
        stage_store.sync_from_tasks()


class StageStoreTests(Base):
    def test_goal_and_stage_persist_across_connections(self):
        goal, stage = self.goal_with_stage()
        with stage_store.connection() as conn:
            goal_row = conn.execute('SELECT * FROM goals WHERE id = ?', (goal['id'],)).fetchone()
            stage_row = conn.execute('SELECT * FROM stages WHERE id = ?', (stage['id'],)).fetchone()
        self.assertEqual(goal_row['title'], 'Goal stage')
        self.assertEqual(json.loads(stage_row['acceptance']), ['criterion one', 'criterion two'])
        self.assertEqual(stage_row['goal_id'], goal['id'])
        view = stage_store.stage_view(stage['id'])
        self.assertEqual(view['status'], stage_store.READY)
        self.assertTrue(view['readiness']['ready'])

    def test_dependency_requires_independent_acceptance(self):
        goal, first = self.goal_with_stage(title='first')
        second = stage_store.add_stage(goal['id'], 'second', 'depends on first',
                                       depends_on=[first['id']], files=['a.py'],
                                       acceptance=['second criterion'], candidates=['home'])
        self.assertEqual(second['status'], stage_store.PENDING)
        readiness = stage_store.readiness(second['id'])
        self.assertFalse(readiness['ready'])
        self.assertEqual(readiness['blocked_by'], [first['id']])
        with self.assertRaisesRegex(ValueError, '前置阶段'):
            stage_store.claim_dispatch(second['id'], 't-second')
        self.claim_and_accept(first, 't-first')
        self.assertEqual(stage_store.get_stage(first['id'])['status'], stage_store.ACCEPTED)
        self.assertTrue(stage_store.readiness(second['id'])['ready'])
        claimed = stage_store.claim_dispatch(second['id'], 't-second')
        self.assertEqual(claimed['status'], stage_store.DISPATCHED)
        self.assertEqual(claimed['task_ids'], ['t-second'])

    def test_final_acceptance_also_unblocks_dependents(self):
        goal, first = self.goal_with_stage(title='first')
        second = stage_store.add_stage(goal['id'], 'second', 'depends on first',
                                       depends_on=[first['id']], files=['a.py'],
                                       acceptance=['criterion'], candidates=['home'])
        run = self.task('t-final')
        stage_store.claim_dispatch(first['id'], 't-final')
        policy.record_executor(run, {'completed': False, 'test': {'exit_code': 1, 'output': 'boom'}})
        record = policy.load(run)
        rows = [{'id': item['id'], 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'main reran the check', 'exit_code': 0}]}
                for item in record['criteria']]
        policy.final_accept(run, 'main', rows, 'main took over and reran the acceptance test')
        stage_store.sync_from_tasks()
        stage = stage_store.get_stage(first['id'])
        self.assertEqual(stage['status'], stage_store.FINAL_ACCEPTED)
        self.assertTrue(stage_store.readiness(second['id'])['ready'])

    def test_atomic_dispatch_has_only_one_winner(self):
        _, stage = self.goal_with_stage()

        def claim(name):
            try:
                stage_store.claim_dispatch(stage['id'], name)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ['race-a', 'race-b']))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(stage_store.get_stage(stage['id'])['task_ids']), 1)

    def test_recovery_pending_never_redispatch_and_can_be_requeued(self):
        _, stage = self.goal_with_stage()
        self.task('t-interrupted')
        stage_store.claim_dispatch(stage['id'], 't-interrupted')
        registry.patch_task(registry.task_dir('t-interrupted'), status='failed',
                            phase='面板重启，任务未执行', error='面板在任务开始前重启，请重新分派。')
        report = stage_store.recover()
        self.assertEqual([row['stage_id'] for row in report['actions']], [stage['id']])
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.RECOVERY_PENDING)
        self.assertEqual([row['stage_id'] for row in stage_store.pending_recovery()], [stage['id']])
        with self.assertRaisesRegex(ValueError, '恢复'):
            stage_store.claim_dispatch(stage['id'], 't-retry')
        requeued = stage_store.requeue_stage(stage['id'], 'inspected; safe to retry')
        self.assertEqual(requeued['status'], stage_store.READY)
        stage_store.claim_dispatch(stage['id'], 't-retry')
        actions = [row['action'] for row in stage_store.log_entries(stage_id=stage['id'])]
        self.assertIn('recovery_pending', actions)
        self.assertIn('requeue', actions)

    def test_cancellation_is_a_queryable_record(self):
        _, stage = self.goal_with_stage()
        stage_store.cancel_stage(stage['id'], 'user stopped the plan')
        current = stage_store.get_stage(stage['id'])
        self.assertEqual(current['status'], stage_store.CANCELLED)
        entries = stage_store.log_entries(stage_id=stage['id'])
        cancel = [row for row in entries if row['action'] == 'cancel']
        self.assertEqual(len(cancel), 1)
        self.assertIn('user stopped', cancel[0]['detail'])
        self.assertFalse(stage_store.readiness(stage['id'])['ready'])

    def test_readiness_and_claim_gate_cancelled_goal(self):
        goal, stage = self.goal_with_stage(title='cancelled-goal')
        stage_store.set_goal_status(goal['id'], stage_store.GOAL_CANCELLED)
        readiness = stage_store.readiness(stage['id'])
        self.assertFalse(readiness['ready'])
        self.assertTrue(any('目标已取消' in reason for reason in readiness['reasons']))
        with self.assertRaisesRegex(ValueError, '不满足派发条件'):
            stage_store.claim_dispatch(stage['id'], 't-cancelled-goal')
        # The snapshot view used by the panel reports the same block.
        view = stage_store.stage_view(stage['id'], conn_stages=stage_store.get_goal(goal['id'])['stages'],
                                      goal_status=stage_store.GOAL_CANCELLED)
        self.assertFalse(view['readiness']['ready'])
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.READY)

    def test_goal_completion_requires_accepted_nonempty_stages(self):
        empty = stage_store.create_goal('Empty goal', 'no stages', str(self.project))
        with self.assertRaisesRegex(ValueError, '没有任何阶段'):
            stage_store.set_goal_status(empty['id'], stage_store.GOAL_COMPLETED)
        goal, stage = self.goal_with_stage(title='completion')
        with self.assertRaisesRegex(ValueError, '全部阶段独立验收'):
            stage_store.set_goal_status(goal['id'], stage_store.GOAL_COMPLETED)
        self.assertEqual(stage_store.get_goal(goal['id'])['status'], stage_store.GOAL_ACTIVE)
        self.claim_and_accept(stage, 't-complete')
        completed = stage_store.set_goal_status(goal['id'], stage_store.GOAL_COMPLETED)
        self.assertEqual(completed['status'], stage_store.GOAL_COMPLETED)
        self.assertFalse(stage_store.readiness(stage['id'])['ready'])
        self.assertTrue(any('目标已完成' in reason for reason in stage_store.readiness(stage['id'])['reasons']))

    def test_cancelled_stage_and_goal_survive_sync_and_recover(self):
        goal, stage = self.goal_with_stage(title='durable-cancel')
        self.task('t-durable')
        stage_store.claim_dispatch(stage['id'], 't-durable')
        stage_store.cancel_stage(stage['id'], 'user stopped')
        registry.patch_task(registry.task_dir('t-durable'), status='running')
        stage_store.sync_from_tasks()
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.CANCELLED)
        stage_store.recover()
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.CANCELLED)

        goal2, stage2 = self.goal_with_stage(title='goal-cancel')
        self.task('t-goal')
        stage_store.claim_dispatch(stage2['id'], 't-goal')
        stage_store.set_goal_status(goal2['id'], stage_store.GOAL_CANCELLED)
        registry.patch_task(registry.task_dir('t-goal'), status='failed', phase='执行失败')
        stage_store.sync_from_tasks()
        self.assertEqual(stage_store.get_stage(stage2['id'])['status'], stage_store.DISPATCHED)
        stage_store.recover()
        self.assertEqual(stage_store.get_stage(stage2['id'])['status'], stage_store.DISPATCHED)
        self.assertEqual(stage_store.get_goal(goal2['id'])['status'], stage_store.GOAL_CANCELLED)
        with self.assertRaisesRegex(ValueError, '不能改回'):
            stage_store.set_goal_status(goal2['id'], stage_store.GOAL_ACTIVE)
        with self.assertRaisesRegex(ValueError, '不能重新派发'):
            stage_store.requeue_stage(stage2['id'], 'try to revive a cancelled goal')


class StageDispatchTests(Base):
    def setUp(self):
        super().setUp()
        self.settings_file = registry.HERE / 'workflow.json'
        settings.save_settings({'enabled': True, 'paid_allowed': False})

    def dispatch(self, stage_id, **body):
        with mock.patch.object(panel.EXECUTOR, 'submit') as queued, \
             mock.patch.object(panel.dsh_worker, 'availability', return_value={'ready': True}):
            row = panel.dispatch_stage(stage_id, {'prompt': 'do the stage work', **body})
        return row, queued

    def test_stage_dispatch_links_task_and_starts_a_new_session(self):
        _, stage = self.goal_with_stage()
        row, queued = self.dispatch(stage['id'], source='codex')
        queued.assert_called_once()
        task = row['task']
        self.assertEqual(task['stage_id'], stage['id'])
        self.assertEqual(task['goal_id'], stage['goal_id'])
        self.assertEqual(task['model_id'], 'home')
        self.assertEqual(task['files'], ['a.py'])
        self.assertNotIn('dsh_session_id', task)
        self.assertNotIn('dsh_session_key', task)
        self.assertEqual(task['route']['requirements']['acceptance'], ['criterion one', 'criterion two'])
        self.assertEqual(row['stage']['status'], stage_store.DISPATCHED)
        self.assertEqual(row['stage']['active_task_id'], task['id'])

    def test_fixed_stage_model_cannot_bypass_paid_only_scope(self):
        _, stage = self.goal_with_stage()
        settings.save_settings({'paid_allowed': True, 'model_scope': 'paid_only'})
        with mock.patch.object(panel.EXECUTOR, 'submit') as queued, \
             mock.patch.object(panel.dsh_worker, 'availability', return_value={'ready': True}):
            with self.assertRaisesRegex(ValueError, '执行模型范围'):
                panel.dispatch_stage(stage['id'], {'prompt': 'do the stage work', 'source': 'codex'})
        queued.assert_not_called()
        saved = stage_store.get_stage(stage['id'])
        self.assertFalse(saved.get('active_task_id'))

    def test_stage_dispatch_resolves_an_explicit_project_without_the_locator(self):
        goal = stage_store.create_goal('Project goal', 'project goal', str(self.project),
                                       store='project')
        stage = stage_store.add_stage(goal['id'], 'project stage', 'deliver',
                                      files=['a.py'], acceptance=['criterion one'],
                                      candidates=['home'], fixed_model='home')
        project = project_storage.normalize(stage['project'])
        project_storage.index_path().unlink()
        with self.assertRaises(ValueError):
            # Without the explicit project the locator is gone, so the stage is
            # not guessed from any global "last project".
            panel.dispatch_stage(stage['id'], {'prompt': 'x', 'source': 'codex'})
        with mock.patch.object(panel.EXECUTOR, 'submit'), \
             mock.patch.object(panel.dsh_worker, 'availability', return_value={'ready': True}):
            row = panel.dispatch_stage(stage['id'], {'prompt': 'do the stage work', 'source': 'codex'},
                                       project=project)
        self.assertEqual(row['task']['stage_id'], stage['id'])
        self.assertEqual(row['stage']['active_task_id'], row['task']['id'])
        self.assertTrue(project_storage.task_dir(project, row['task']['id']).is_dir())

    def test_stage_round_can_explicitly_reuse_the_stage_session(self):
        _, stage = self.goal_with_stage()
        row, _ = self.dispatch(stage['id'], source='codex')
        first = row['task']['id']
        registry.patch_task(registry.task_dir(first), status='completed',
                            dsh_session_id='native-stage', dsh_session_key='desk-' + first)
        stage_store.sync_from_tasks()
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.EXECUTOR_COMPLETED)
        with mock.patch.object(panel.EXECUTOR, 'submit'), \
             mock.patch.object(panel.dsh_worker, 'availability', return_value={'ready': True}):
            again = panel.dispatch_stage(stage['id'], {'prompt': 'round two', 'source': 'codex',
                                                       'reuse_session': True})
        self.assertEqual(again['task']['dsh_session_key'], 'desk-' + first)
        self.assertEqual(again['task']['dsh_session_id'], 'native-stage')
        self.assertEqual(again['task']['parent_task_id'], first)

    def test_switch_off_blocks_auto_but_not_fixed_model_and_keeps_running_task(self):
        settings.save_settings({'enabled': False})
        _, open_stage = self.goal_with_stage(title='open')
        with self.assertRaisesRegex(ValueError, '总开关'):
            panel.dispatch_stage(open_stage['id'], {'prompt': 'x', 'route_mode': 'auto'})
        self.assertEqual(stage_store.get_stage(open_stage['id'])['status'], stage_store.READY)
        manual, _ = self.dispatch(open_stage['id'], source='codex')
        self.assertEqual(manual['task']['status'], 'queued')
        # A running task is never cancelled by flipping the switch off.
        registry.patch_task(registry.task_dir(manual['task']['id']), status='running')
        self.assertEqual(registry.read_task(registry.task_dir(manual['task']['id']))['status'], 'running')

    def test_request_output_tokens_recorded_without_changing_model_preset(self):
        _, stage = self.goal_with_stage()
        before = registry.get_model('home')['max_output_tokens']
        row, _ = self.dispatch(stage['id'], source='codex', request_output_tokens=1234)
        self.assertEqual(row['task']['request_output_tokens'], 1234)
        self.assertEqual(registry.get_model('home')['max_output_tokens'], before)
        _, other = self.goal_with_stage(title='other')
        with self.assertRaisesRegex(ValueError, '输出额度'):
            panel.dispatch_stage(other['id'], {'prompt': 'x', 'request_output_tokens': 0})

    def test_dependent_dispatch_injects_validated_prerequisite_handover(self):
        goal, first = self.goal_with_stage(title='inject-first')
        second = stage_store.add_stage(goal['id'], 'inject-second', 'depends on first',
                                       depends_on=[first['id']], files=['a.py'],
                                       acceptance=['second criterion'], candidates=['home'])
        run = self.task('t-inject-first', stage_id=first['id'], goal_id=goal['id'])
        stage_store.claim_dispatch(first['id'], 't-inject-first')
        self.accept_task(run)
        stage_store.sync_from_tasks()
        task_handover.create(run, summary='first deliverable summary',
                             interfaces=['one() -> int'], decisions=['kept signature'],
                             context=['a.py'], stage_id=first['id'], goal_id=goal['id'])
        row, _ = self.dispatch(second['id'], source='codex')
        prompt = (registry.task_dir(row['task']['id']) / 'prompt.txt').read_text(encoding='utf-8')
        self.assertIn('前置阶段成果交接', prompt)
        self.assertIn('first deliverable summary', prompt)
        self.assertIn('sha256:', prompt)
        self.assertIn('one() -> int', prompt)
        self.assertIn('do the stage work', prompt)

    def test_dependent_dispatch_blocks_missing_and_stale_handover(self):
        goal, first = self.goal_with_stage(title='block-first')
        second = stage_store.add_stage(goal['id'], 'block-second', 'depends on first',
                                       depends_on=[first['id']], files=['a.py'],
                                       acceptance=['c'], candidates=['home'])
        run = self.task('t-block-first', stage_id=first['id'], goal_id=goal['id'])
        stage_store.claim_dispatch(first['id'], 't-block-first')
        self.accept_task(run)
        stage_store.sync_from_tasks()
        # An accepted prerequisite without a persisted handover blocks the consumer.
        with self.assertRaisesRegex(ValueError, '成果交接'):
            self.dispatch(second['id'], source='codex')
        self.assertIsNone(stage_store.get_stage(second['id'])['active_task_id'] or None)
        # A handover whose recorded files changed is stale and also blocks it.
        task_handover.create(run, summary='deliverable', stage_id=first['id'], goal_id=goal['id'])
        (self.project / 'a.py').write_text('def one():\n    return 42\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '失效'):
            self.dispatch(second['id'], source='codex')
        self.assertEqual(stage_store.get_stage(second['id'])['status'], stage_store.PENDING)

    def test_task_request_output_tokens_bounds_explicit_sdk_maximum(self):
        before = registry.get_model('home')['max_output_tokens']
        _, stage = self.goal_with_stage(title='token-bound')
        row, _ = self.dispatch(stage['id'], source='codex', request_output_tokens=8192)
        state = registry.read_task(registry.task_dir(row['task']['id']))
        self.assertEqual(state['request_output_tokens'], 8192)
        # The model preset is untouched ...
        self.assertEqual(registry.get_model('home')['max_output_tokens'], before)
        # ... but an explicit SDK max_tokens is still bounded by the opt-in task target.
        model = dict(registry.get_model(state['model_id']))
        model['max_output_tokens'] = 64000
        measured = token_meter.measure_for_relay(
            model, {'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 64000},
            task_state=state)
        self.assertEqual(measured['output']['target'], 8192)
        self.assertEqual(measured['output']['origin'], 'explicit_max_tokens_task_limit')

    def test_acceptance_array_is_not_stringified(self):
        body = {'kind': 'edit', 'engine': 'dsh', 'route_mode': 'manual', 'model_id': 'home',
                'prompt': 'x', 'files': ['a.py'],
                'routing': {'category': 'code_small', 'complexity': 'simple', 'input_tokens': 8192,
                            'output_tokens': 4096, 'expected_calls': 1,
                            'acceptance': ['first condition', 'second condition']}}
        req = model_router.requirements(body)
        self.assertEqual(req['acceptance'], ['first condition', 'second condition'])
        _, stage = self.goal_with_stage()
        run = self.task('t-array', acceptance=['first condition', 'second condition'])
        stage_store.claim_dispatch(stage['id'], 't-array')
        policy.record_executor(run, {'completed': True})
        record = policy.load(run)
        self.assertEqual([item['text'] for item in record['criteria']],
                         ['first condition', 'second condition'])
        self.assertNotIn("['first condition'", json.dumps(record['criteria']))


class ReworkSessionTests(Base):
    def setUp(self):
        super().setUp()
        settings.save_settings({'enabled': True, 'paid_allowed': False})

    def prepare_returned(self, name='parent'):
        run = self.task(name)
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 0, 'output': 'ok'}})
        policy.request_rework(run, 'main', ['c1'], 'observed defect', 'a.py only')
        return run

    def test_rework_refuses_original_model_outside_scope_and_keeps_allowance(self):
        run = self.prepare_returned()
        settings.save_settings({'paid_allowed': True, 'model_scope': 'paid_only'})
        with self.assertRaisesRegex(ValueError, '执行模型范围'):
            self.submit({'prompt': 'fix it', 'rework_of': 'parent', 'source': 'codex'})
        review = policy.load(run)
        self.assertEqual(review['state'], policy.RETURNED)
        self.assertFalse((review.get('rework') or {}).get('dispatched_task_id'))

    def test_continuation_cannot_bypass_scope_by_omitting_the_model(self):
        run = self.task('continue-parent', dsh_session_id='native-parent', dsh_session_key='desk-parent')
        settings.save_settings({'paid_allowed': True, 'model_scope': 'paid_only'})
        with self.assertRaisesRegex(ValueError, '执行模型范围'):
            self.submit({'prompt': 'continue', 'continue_task': run.name, 'source': 'codex'})

    def submit(self, body):
        with mock.patch.object(panel.EXECUTOR, 'submit'), \
             mock.patch.object(panel.dsh_worker, 'availability', return_value={'ready': True}):
            return panel.submit_task(body)

    def test_rework_defaults_to_a_new_session_with_same_model_and_scope(self):
        self.prepare_returned()
        child = self.submit({'prompt': 'fix it', 'rework_of': 'parent', 'source': 'codex'})
        self.assertEqual(child['parent_task_id'], 'parent')
        self.assertEqual(child['rework_of'], 'parent')
        self.assertEqual(child['model_id'], 'home')
        self.assertEqual(child['project'], str(self.project))
        self.assertEqual(child['files'], ['a.py'])
        self.assertNotIn('dsh_session_id', child)
        self.assertNotIn('dsh_session_key', child)
        self.assertEqual(child['delegation']['rework_count'], 1)
        parent = policy.load(registry.task_dir('parent'))
        self.assertEqual(parent['rework']['dispatched_task_id'], child['id'])

    def test_rework_reuse_session_is_explicit_only(self):
        run = self.prepare_returned()
        registry.patch_task(run, dsh_session_key='desk-parent', dsh_session_id='native-parent')
        child = self.submit({'prompt': 'fix it', 'rework_of': 'parent', 'reuse_session': True, 'source': 'codex'})
        self.assertEqual(child['dsh_session_key'], 'desk-parent')
        self.assertEqual(child['dsh_session_id'], 'native-parent')

    def test_rework_inherits_canonical_stage_and_criteria(self):
        goal, stage = self.goal_with_stage()
        run = self.task('parent', goal_id=goal['id'], stage_id=stage['id'])
        stage_store.claim_dispatch(stage['id'], 'parent')
        policy.record_executor(run, {'completed': True})
        policy.request_rework(run, 'main', ['c1'], 'defect', 'a.py only')
        stage_store.sync_from_tasks()
        with self.assertRaisesRegex(ValueError, 'rework-of'):
            stage_store.claim_dispatch(stage['id'], 'bypass')
        child = self.submit({'prompt': 'fix it', 'rework_of': 'parent', 'source': 'codex'})
        self.assertEqual(child['stage_id'], stage['id'])
        self.assertEqual(child['goal_id'], goal['id'])
        self.assertEqual(child['route']['requirements']['acceptance'], ['A', 'B'])
        policy.record_executor(registry.task_dir(child['id']), {'completed': True})
        self.assertEqual(len(policy.load(registry.task_dir(child['id']))['criteria']), 2)

    def test_goal_cancel_requests_active_task_cancel(self):
        goal, stage = self.goal_with_stage()
        self.task('active', status='running')
        stage_store.claim_dispatch(stage['id'], 'active')
        with mock.patch.object(panel, 'cancel_task') as cancel:
            result = panel.set_goal_status(goal['id'], {'status': 'cancelled', 'reason': 'stop'})
        cancel.assert_called_once()
        self.assertEqual(cancel.call_args.args[0], 'active')
        self.assertEqual(result['cancel_requested_tasks'], ['active'])
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.CANCELLED)

    def test_concurrent_rework_dispatch_has_one_winner(self):
        self.prepare_returned()

        def dispatch(_):
            try:
                return self.submit({'prompt': 'fix it', 'rework_of': 'parent', 'source': 'codex'})['id']
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(dispatch, range(2)))
        winners = [row for row in results if row]
        self.assertEqual(len(winners), 1)
        self.assertEqual(policy.load(registry.task_dir('parent'))['rework']['dispatched_task_id'], winners[0])

    def test_continue_task_still_explicitly_reuses_the_session(self):
        run = self.task('parent2')
        registry.patch_task(run, dsh_session_key='desk-parent2', dsh_session_id='native-parent2', status='completed')
        child = self.submit({'prompt': 'continue', 'continue_task': 'parent2', 'kind': 'edit',
                             'project': str(self.project), 'files': ['a.py']})
        self.assertEqual(child['dsh_session_key'], 'desk-parent2')
        self.assertEqual(child['dsh_session_id'], 'native-parent2')
        self.assertEqual(child['parent_task_id'], 'parent2')

    def _returned_stage_parent(self):
        """A returned review whose stage still points at the finished parent."""
        goal, stage = self.goal_with_stage()
        run = self.task('parent', goal_id=goal['id'], stage_id=stage['id'])
        stage_store.claim_dispatch(stage['id'], 'parent')
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py']})
        policy.request_rework(run, 'main', ['c1'], 'defect', 'a.py only')
        return goal, stage, run

    def test_rework_dispatch_reconciles_a_stage_left_on_the_finished_parent(self):
        goal, stage, _run = self._returned_stage_parent()
        # Deliberately no sync_from_tasks(): the persisted stage is still
        # ``dispatched`` on the completed parent, which is exactly the queue
        # state that used to refuse the claim after burning the rework slot.
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.DISPATCHED)
        child = self.submit({'prompt': 'fix it', 'rework_of': 'parent', 'source': 'codex'})
        self.assertEqual(child['stage_id'], stage['id'])
        self.assertEqual(child['goal_id'], goal['id'])
        current = stage_store.get_stage(stage['id'])
        self.assertEqual(current['status'], stage_store.DISPATCHED)
        self.assertEqual(current['active_task_id'], child['id'])
        parent = policy.load(registry.task_dir('parent'))
        self.assertEqual(parent['rework']['dispatched_task_id'], child['id'])
        self.assertEqual(parent['rework_count'], 1)
        self.assertTrue(project_storage.task_dir(stage['project'] or str(self.project),
                                                 child['id']).is_dir())

    def test_a_refused_stage_claim_releases_only_the_unstarted_rework(self):
        goal, stage, _run = self._returned_stage_parent()
        stage_store.sync_from_tasks(stage['goal_id'], project=stage['project'] or None)
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.RETURNED)
        with mock.patch.object(stage_store, 'claim_dispatch',
                               side_effect=stage_store.StageError('注入的认领失败')):
            with self.assertRaises(stage_store.StageError):
                self.submit({'prompt': 'fix it', 'rework_of': 'parent', 'source': 'codex'})
        parent = policy.load(registry.task_dir('parent'))
        # The single rework slot stays usable: count and state are untouched,
        # only the never-started child binding is cleared with a durable record.
        self.assertEqual(parent['rework_count'], 1)
        self.assertEqual(parent['state'], policy.RETURNED)
        self.assertIsNone(parent['rework']['dispatched_task_id'])
        self.assertIn('release_unstarted_rework', [row['action'] for row in parent['decisions']])
        task_ids = [row['task_id'] for row in project_storage.iter_task_dirs()]
        self.assertEqual(task_ids, ['parent'], '不得留下没有任务目录的幻影子任务')
        # And the freed slot can really be used afterwards.
        child = self.submit({'prompt': 'fix it', 'rework_of': 'parent', 'source': 'codex'})
        self.assertEqual(policy.load(registry.task_dir('parent'))['rework']['dispatched_task_id'],
                         child['id'])

    def test_a_cancelled_goal_refusal_keeps_the_rework_slot(self):
        goal, stage, _run = self._returned_stage_parent()
        stage_store.sync_from_tasks(stage['goal_id'], project=stage['project'] or None)
        stage_store.set_goal_status(goal['id'], stage_store.GOAL_CANCELLED)
        with self.assertRaisesRegex(ValueError, '不能派发'):
            self.submit({'prompt': 'fix it', 'rework_of': 'parent', 'source': 'codex'})
        parent = policy.load(registry.task_dir('parent'))
        self.assertEqual(parent['rework_count'], 1)
        self.assertIsNone(parent['rework']['dispatched_task_id'])

    def test_release_never_touches_a_different_claim(self):
        run = self.prepare_returned()
        first = policy.mark_rework_dispatched(run, 'child-one')
        self.assertEqual(first['rework']['dispatched_task_id'], 'child-one')
        unchanged = policy.release_rework_dispatch(run, 'child-two', reason='not ours')
        self.assertEqual(unchanged['rework']['dispatched_task_id'], 'child-one')
        released = policy.release_rework_dispatch(run, 'child-one', reason='precheck failed')
        self.assertIsNone(released['rework']['dispatched_task_id'])
        self.assertEqual(released['rework_count'], 1)
        self.assertEqual(released['state'], policy.RETURNED)

    def test_final_acceptance_preserves_failure_and_rework_count(self):
        run = self.task('failed-work')
        policy.record_executor(run, {'completed': False, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 1, 'output': 'boom'}})
        record = policy.load(run)
        rows = [{'id': item['id'], 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'main reran the test', 'exit_code': 0}]}
                for item in record['criteria']]
        with self.assertRaisesRegex(ValueError, '最终验收'):
            policy.accept(run, 'main', rows)
        final = policy.final_accept(run, 'main', rows, 'main repaired the file and reran the test')
        self.assertEqual(final['state'], policy.FINAL_ACCEPTED)
        self.assertEqual(final['executor']['status'], 'failed')
        self.assertEqual(final['final']['executor_status_preserved'], 'failed')
        self.assertEqual(final['rework_count'], 0)
        summary = policy.panel_summary(final)
        self.assertTrue(summary['final_accepted'])
        self.assertEqual(summary['executor_status'], 'failed')
        with self.assertRaises(ValueError):
            policy.block(run, 'main', 'should not downgrade a final acceptance')
        self.assertEqual(policy.aggregate()[policy.FINAL_ACCEPTED], 1)


class HandoverTests(Base):
    def accepted_task(self, name='source', stage_id=None):
        run = self.task(name)
        self.accept_task(run)
        if stage_id:
            registry.patch_task(run, stage_id=stage_id)
        return run

    def test_handover_computes_real_hashes_and_never_treats_summary_as_evidence(self):
        run = self.accepted_task()
        record = task_handover.create(run, summary='changed one() to return 1',
                                      interfaces=['one() -> int'], decisions=['kept signature'],
                                      remaining_issues=['none'], context=['a.py'],
                                      test_evidence=[{'name': 'summary note', 'source': 'summary'},
                                                     {'name': 'unittest', 'source': 'reviewer_test', 'exit_code': 0}])
        digest = hashlib.sha256((self.project / 'a.py').read_bytes()).hexdigest()
        self.assertEqual(record['files'][0]['sha256'], digest)
        self.assertTrue(record['verification']['files_verified'])
        self.assertTrue(record['verification']['evidence_independent'])
        self.assertFalse(record['verification']['summary_counts_as_evidence'])
        self.assertTrue(record['summary_is_claim'])
        self.assertEqual(record['claims'][0]['source'], 'summary')
        self.assertNotIn('answer.txt', json.dumps(record))

    def test_handover_requires_accepted_review_and_stays_in_scope(self):
        run = self.task('unfinished')
        policy.record_executor(run, {'completed': False})
        with self.assertRaisesRegex(ValueError, '独立验收'):
            task_handover.create(run, summary='not accepted yet')
        accepted = self.accepted_task('accepted')
        with self.assertRaisesRegex(ValueError, '范围'):
            task_handover.create(accepted, summary='scope creep', files=['b.py'])

    def test_verify_detects_a_changed_file(self):
        run = self.accepted_task()
        task_handover.create(run, summary='initial deliverable')
        self.assertFalse(task_handover.verify(run)['stale'])
        (self.project / 'a.py').write_text('def one():\n    return 99\n', encoding='utf-8')
        freshness = task_handover.verify(run)
        self.assertTrue(freshness['stale'])
        self.assertEqual(freshness['changed'], ['a.py'])
        self.assertIn('不能悄悄沿用旧摘要', freshness['message'])

    def test_reference_rejects_unaccepted_out_of_scope_and_cross_project(self):
        goal, source_stage = self.goal_with_stage(title='source-stage')
        consumer_stage = stage_store.add_stage(goal['id'], 'consumer', 'consumer objective',
                                               depends_on=[source_stage['id']], files=['a.py'],
                                               acceptance=['consumer criterion'], candidates=['home'])
        source = self.accepted_task('source-task', stage_id=source_stage['id'])
        task_handover.create(source, summary='persisted deliverable', stage_id=source_stage['id'],
                             goal_id=goal['id'])
        consumer = self.task('consumer-task', stage_id=consumer_stage['id'])
        # Identity is derived from the real records; no stage parameters needed.
        resolved = task_handover.resolve_reference(consumer, 'source-task')
        self.assertEqual(resolved['status'], 'ok')

        # A real same-goal source that is not a prerequisite is still refused.
        unrelated = stage_store.add_stage(goal['id'], 'unrelated', 'not a prerequisite',
                                          files=['a.py'], acceptance=['x'], candidates=['home'])
        unrelated_run = self.accepted_task('unrelated-task', stage_id=unrelated['id'])
        task_handover.create(unrelated_run, summary='unrelated deliverable',
                             stage_id=unrelated['id'], goal_id=goal['id'])
        with self.assertRaisesRegex(ValueError, '必要前置'):
            task_handover.resolve_reference(consumer, 'unrelated-task')

        # A source in a different goal is refused even with matching project.
        other_goal = stage_store.create_goal('Other', 'other goal', str(self.project))
        other_stage = stage_store.add_stage(other_goal['id'], 'other', 'other objective',
                                            files=['a.py'], acceptance=['x'], candidates=['home'])
        other_run = self.accepted_task('other-goal-task', stage_id=other_stage['id'])
        task_handover.create(other_run, summary='other goal deliverable',
                             stage_id=other_stage['id'], goal_id=other_goal['id'])
        with self.assertRaisesRegex(ValueError, '同一目标'):
            task_handover.resolve_reference(consumer, 'other-goal-task')
        # Naming a real prerequisite stage cannot unlock another goal's task.
        with self.assertRaisesRegex(ValueError, '阶段参数'):
            task_handover.resolve_reference(consumer, 'other-goal-task',
                                            consumer_stage_id=consumer_stage['id'],
                                            source_stage_id=source_stage['id'])

        self.task('unaccepted-source')
        with self.assertRaisesRegex(ValueError, '没有成果交接'):
            task_handover.resolve_reference(consumer, 'unaccepted-source')

    def test_reference_cannot_be_widened_by_caller_supplied_stage_ids(self):
        goal, source_stage = self.goal_with_stage(title='src-bind')
        consumer_stage = stage_store.add_stage(goal['id'], 'cons-bind', 'consumer',
                                               depends_on=[source_stage['id']], files=['a.py'],
                                               acceptance=['x'], candidates=['home'])
        other = stage_store.add_stage(goal['id'], 'other-bind', 'unrelated stage',
                                      files=['a.py'], acceptance=['x'], candidates=['home'])
        source = self.accepted_task('bind-source', stage_id=source_stage['id'])
        task_handover.create(source, summary='deliverable', stage_id=source_stage['id'],
                             goal_id=goal['id'])
        consumer = self.task('bind-consumer', stage_id=consumer_stage['id'])
        # Restating the true ids is accepted ...
        self.assertEqual(task_handover.resolve_reference(
            consumer, 'bind-source', consumer_stage_id=consumer_stage['id'],
            source_stage_id=source_stage['id'])['status'], 'ok')
        # ... but a caller cannot claim a different consumer stage ...
        with self.assertRaisesRegex(ValueError, '阶段参数'):
            task_handover.resolve_reference(consumer, 'bind-source',
                                            consumer_stage_id=other['id'])
        # ... nor point the reference at a different source stage.
        with self.assertRaisesRegex(ValueError, '阶段参数'):
            task_handover.resolve_reference(consumer, 'bind-source',
                                            source_stage_id=other['id'])

    def test_reference_from_staged_consumer_requires_staged_source(self):
        goal, source_stage = self.goal_with_stage(title='stage-src')
        consumer_stage = stage_store.add_stage(goal['id'], 'stage-cons', 'consumer',
                                               depends_on=[source_stage['id']], files=['a.py'],
                                               acceptance=['x'], candidates=['home'])
        unstaged = self.accepted_task('unstaged-source')
        task_handover.create(unstaged, summary='unstaged deliverable')
        consumer = self.task('staged-consumer', stage_id=consumer_stage['id'])
        with self.assertRaisesRegex(ValueError, '不属于任何阶段'):
            task_handover.resolve_reference(consumer, 'unstaged-source')

    def test_reference_reports_a_stale_handover_instead_of_trusting_it(self):
        goal, source_stage = self.goal_with_stage(title='src')
        consumer_stage = stage_store.add_stage(goal['id'], 'cons', 'consumer',
                                               depends_on=[source_stage['id']], files=['a.py'],
                                               acceptance=['x'], candidates=['home'])
        source = self.accepted_task('src-task', stage_id=source_stage['id'])
        task_handover.create(source, summary='deliverable', stage_id=source_stage['id'], goal_id=goal['id'])
        consumer = self.task('cons-task', stage_id=consumer_stage['id'])
        (self.project / 'a.py').write_text('def one():\n    return 7\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '失效'):
            task_handover.resolve_reference(consumer, 'src-task')
        # The reporting path still describes the staleness without handing the
        # payload over for use.
        self.assertTrue(task_handover.verify(registry.task_dir('src-task'))['stale'])

    def test_source_review_must_still_be_accepted_at_reference_time(self):
        run = self.accepted_task('late-source')
        task_handover.create(run, summary='deliverable')
        consumer = self.task('late-consumer')
        # A rollback of the source review state must invalidate the reference.
        path = policy.review_path(run)
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['state'] = policy.BLOCKED
        path.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '尚未完成独立验收'):
            task_handover.resolve_reference(consumer, 'late-source')


class PanelHttpTests(Base):
    def setUp(self):
        super().setUp()
        settings.save_settings({'enabled': True, 'paid_allowed': False})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, path, body=None, token=True, origin=None):
        import project_scope
        headers = {'Content-Type': 'application/json',
                   'X-Model-Desk-View':project_scope.encode_view_header({'mode':'all'})}
        if token:
            headers['X-Panel-Token'] = panel.TOKEN
        if origin:
            headers['Origin'] = origin
        req = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                      headers=headers)
        with self.opener.open(req, timeout=10) as response:
            return json.load(response)

    def test_guard_and_goal_stage_api_shapes(self):
        with self.assertRaises(HTTPError) as error:
            self.request('/api/goals', {'title': 'x', 'objective': 'y'}, token=False)
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.request('/api/goals', {'title': 'x', 'objective': 'y'}, origin='https://evil.example')
        self.assertEqual(error.exception.code, 403)
        goal = self.request('/api/goals', {'title': 'API goal', 'objective': 'shape',
                                           'project': str(self.project)})
        stage = self.request('/api/stages', {'goal_id': goal['id'], 'title': 'API stage',
                                             'objective': 'deliver', 'files': ['a.py'],
                                             'acceptance': ['first', 'second'],
                                             'candidates': ['home'], 'fixed_model': 'home'})
        self.assertEqual(stage['status'], stage_store.READY)
        detail = self.request('/api/goals/' + goal['id'])
        self.assertEqual(detail['id'], goal['id'])
        self.assertEqual(len(detail['stages']), 1)
        listing = self.request('/api/stages')
        self.assertIn('stats', listing)
        self.assertEqual(self.request('/api/recovery')['pending'], [])
        state = self.request('/api/state')
        self.assertIn('stages', state)
        self.assertEqual(state['stages']['stats']['goals'], 1)

    def test_stage_dispatch_api_enforces_dependencies(self):
        goal = self.request('/api/goals', {'title': 'g', 'objective': 'o', 'project': str(self.project)})
        first = self.request('/api/stages', {'goal_id': goal['id'], 'title': 'first', 'objective': 'one',
                                             'files': ['a.py'], 'acceptance': ['c1'],
                                             'candidates': ['home'], 'fixed_model': 'home'})
        second = self.request('/api/stages', {'goal_id': goal['id'], 'title': 'second', 'objective': 'two',
                                              'depends_on': [first['id']], 'files': ['a.py'],
                                              'acceptance': ['c2'], 'candidates': ['home'],
                                              'fixed_model': 'home'})
        with mock.patch.object(panel.EXECUTOR, 'submit'), \
             mock.patch.object(panel.dsh_worker, 'availability', return_value={'ready': True}):
            with self.assertRaises(HTTPError) as error:
                self.request(f"/api/stages/{second['id']}/dispatch",
                             {'prompt': 'work', 'source': 'codex'})
        self.assertEqual(error.exception.code, 400)
        self.assertIn('前置阶段', json.loads(error.exception.read().decode('utf-8'))['error'])
        with mock.patch.object(panel.EXECUTOR, 'submit'), \
             mock.patch.object(panel.dsh_worker, 'availability', return_value={'ready': True}):
            dispatched = self.request(f"/api/stages/{first['id']}/dispatch",
                                      {'prompt': 'work', 'source': 'codex'})
        self.assertEqual(dispatched['task']['stage_id'], first['id'])
        self.assertEqual(dispatched['stage']['status'], stage_store.DISPATCHED)

    def test_review_final_accept_and_handover_api(self):
        run = self.task('api-task')
        policy.record_executor(run, {'completed': False, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 1, 'output': 'boom'}})
        record = policy.load(run)
        rows = [{'id': item['id'], 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'main reran', 'exit_code': 0}]}
                for item in record['criteria']]
        result = self.request(f'/api/tasks/{run.name}/review',
                              {'action': 'final_accept', 'criteria': rows,
                               'notes': 'main repaired it'})
        self.assertTrue(result['summary']['final_accepted'])
        self.assertEqual(result['summary']['executor_status'], 'failed')
        handover = self.request(f'/api/tasks/{run.name}/handover',
                                {'summary': 'main repaired a.py', 'files': ['a.py'],
                                 'test_evidence': [{'name': 'unittest', 'source': 'reviewer_test',
                                                    'exit_code': 0}]})
        self.assertTrue(handover['freshness']['valid'])
        read_back = self.request(f'/api/tasks/{run.name}/handover')
        self.assertEqual(read_back['handover']['summary'], 'main repaired a.py')
        with self.assertRaises(HTTPError) as error:
            self.request(f'/api/tasks/{run.name}/handover',
                         {'summary': 'scope creep', 'files': ['b.py']})
        self.assertEqual(error.exception.code, 400)


class CliTests(Base):
    def run_cli(self, argv):
        import contextlib
        import io
        buffer = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
            code = stage_store.main(argv)
        return code, buffer.getvalue()

    def test_stage_store_cli_creates_goal_and_stage(self):
        code, out = self.run_cli(['--create-goal', '--title', 'CLI goal', '--objective', 'cli',
                                  '--project', str(self.project)])
        self.assertEqual(code, 0)
        goal = json.loads(out)
        code, out = self.run_cli(['--add-stage', '--goal', goal['id'], '--title', 'CLI stage',
                                  '--objective', 'deliver', '--files', 'a.py',
                                  '--acceptance', 'first;second', '--candidates', 'home',
                                  '--fixed-model', 'home'])
        self.assertEqual(code, 0)
        stage = json.loads(out)
        self.assertEqual(stage['acceptance'], ['first', 'second'])
        self.assertEqual(self.run_cli(['--stage', stage['id']])[0], 0)
        code, out = self.run_cli(['--list-goals'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['goals'][0]['id'], goal['id'])
        self.assertEqual(self.run_cli(['--sync'])[0], 0)
        self.assertEqual(self.run_cli(['--recover'])[0], 0)
        self.assertEqual(self.run_cli(['--requeue', '--stage', stage['id'], '--reason', 'cli check'])[0], 1)
        self.assertEqual(self.run_cli(['--cancel', '--stage', stage['id'], '--reason', 'cli cancel'])[0], 0)
        self.assertIn('cancel', [row['action'] for row in stage_store.log_entries(stage_id=stage['id'])])

    def test_handover_cli_creates_and_verifies(self):
        import contextlib
        import io
        run = self.task('cli-handover')
        self.accept_task(run)
        with contextlib.redirect_stdout(io.StringIO()):
            code = task_handover.main(['--create', '--task', 'cli-handover', '--summary', 'cli handover'])
        self.assertEqual(code, 0)
        record = task_handover.load(run)
        self.assertEqual(record['summary'], 'cli handover')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(task_handover.main(['--verify', '--task', 'cli-handover']), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
