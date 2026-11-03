"""Status is a projection of facts, never a new workflow or guessed phase."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_result
import integration_status
import task_status


class TaskStatusTests(unittest.TestCase):
    def task(self, status='completed', **extra):
        return {'id': 'fixture', 'status': status, 'engine': 'zcode', 'kind': 'edit',
                'controller_finalization': {'schema': 1, 'state': 'settled'}, **extra}

    def summary(self, task, review=None, **kwargs):
        return execution_result.project(task, review, **kwargs)['summary']

    def test_execution_end_does_not_mean_acceptance_and_cache_does_not_authorize_it(self):
        task = self.task(review_state='accepted', review={'state': 'accepted'})
        before = deepcopy(task)
        self.assertEqual(self.summary(task)['code'], 'awaiting_review')
        self.assertEqual(self.summary(task, {'state': 'accepted', 'acceptance_valid': False})['code'], 'review_unknown')
        self.assertEqual(self.summary(task, {'state': 'accepted', 'acceptance_valid': True})['code'], 'accepted')
        self.assertEqual(task, before)

    def test_waiting_resources_requires_a_real_typed_wait_record_not_phase_words(self):
        task = self.task('queued', phase='等待资源，已验收，结果未知')
        self.assertEqual(self.summary(task)['code'], 'queued')
        for count in (True, '1', 0, None):
            self.assertEqual(self.summary({**task, 'lease_wait': {'holder': 'other', 'count': count}})['code'], 'queued')
        self.assertEqual(self.summary({**task, 'lease_wait': {'holder': 'other', 'count': 1}})['code'], 'waiting_resource')
        self.assertEqual(self.summary(self.task('running', lease_wait={'holder': 'old', 'count': 1}))['code'], 'running')

    def test_actual_unknown_overrides_failed_but_independent_takeover_keeps_raw_failure(self):
        task = self.task('failed', zcode={'outcome_unknown': True}, changed_files=['a.js'])
        self.assertEqual(self.summary(task)['code'], 'outcome_unknown')
        summary = self.summary(task, {'state': 'final_accepted', 'acceptance_valid': True})
        self.assertEqual(summary['code'], 'accepted_takeover')
        self.assertEqual(summary['raw_status'], 'failed')
        self.assertIn('执行失败', [row['value'] for row in summary['facts']])
        self.assertEqual(task['status'], 'failed')

    def test_normal_finalizing_and_unknown_cleanup_are_distinct(self):
        for state, expected in [('pending', 'finalizing'), ('attention', 'cleanup_attention')]:
            task = self.task(controller_finalization={'schema': 1, 'state': state})
            self.assertEqual(self.summary(task, {'state': 'accepted', 'acceptance_valid': True})['code'], expected)
        self.assertEqual(self.summary(self.task('running', controller_finalization={'schema': 1, 'state': 'pending'}))['code'], 'running')

    def test_legacy_record_does_not_become_a_new_pending_work_item(self):
        task = {'status': 'completed', 'engine': 'pi'}
        self.assertEqual(self.summary(task)['code'], 'review_unrecorded')
        self.assertEqual(self.summary({**task, 'delegation': {'criteria': ['one']}})['code'], 'awaiting_review')

    def test_cancellation_review_error_and_return_are_distinct(self):
        self.assertEqual(self.summary(self.task('cancelled'))['code'], 'cancelled')
        self.assertEqual(self.summary(self.task(), review_error='corrupt review')['code'], 'review_unknown')
        self.assertEqual(self.summary(self.task(), {'state': 'returned'})['code'], 'returned')
        self.assertEqual(self.summary(self.task(), {'state': 'blocked'})['code'], 'blocked')
        blocked = self.summary(self.task(), {'state': 'blocked', 'blocked_reason': '需要用户确定采用哪个数据版本'})
        self.assertEqual(blocked['detail'], '需要用户确定采用哪个数据版本')
        returned = self.summary(self.task(), {'state': 'returned', 'problems': '边界检查尚未通过'})
        self.assertEqual(returned['detail'], '边界检查尚未通过')

    def test_readonly_observed_changes_are_not_claimed_as_executor_writes(self):
        task = self.task(kind='chat', changed_files=['test.py'])
        view = execution_result.project(task)
        self.assertEqual(view['artifacts']['attribution'], 'observed_only')
        self.assertIn('不能据此归因', view['artifacts']['scope_label'])
        self.assertEqual(task['changed_files'], ['test.py'])
        self.assertEqual(view['integration']['state'], 'not_applicable')

    def test_integration_states_never_turn_an_accepted_execution_into_a_failed_task(self):
        task = self.task()
        review = {'state': 'accepted', 'acceptance_valid': True}
        for state in ('awaiting_review', 'accepted', 'conflict', 'check_failed', 'multiple', 'stale'):
            view = execution_result.project(task, review, integration={'state': state, 'detail': 'fixture'})
            self.assertEqual(view['summary']['code'], 'integration_' + state)
            self.assertEqual(view['execution']['status'], 'completed')
            self.assertTrue(view['review']['accepted'])
        view = execution_result.project(task, review, integration={'state': 'none'})
        self.assertEqual(view['summary']['code'], 'accepted')
        self.assertNotIn('等待集成', view['summary']['label'])

    def test_malformed_status_values_do_not_break_projection(self):
        for value in (None, [], {}, True, 123):
            task = self.task(status=value)
            summary = self.summary(task)
            self.assertEqual(summary['code'], 'unknown')
        self.assertEqual(task_status.task_summary({}, {'integration': {'state': []}, 'review': {'state': []}})['code'], 'unknown')

    def test_stage_cancel_and_dependency_wait_and_active_task_are_distinct(self):
        ready = {'ready': False, 'blocked_by': ['dep'], 'dependency_states': {'dep': 'running'}, 'reasons': ['等待前置验收']}
        self.assertEqual(task_status.stage_summary({'status': 'pending'}, ready, [])['code'], 'waiting_dependency')
        self.assertEqual(task_status.stage_summary({'status': 'cancelled'}, ready, [])['code'], 'cancelled')
        active = {'task_id': 'current', 'execution_result': {'summary': self.summary(self.task('queued'))}}
        self.assertEqual(task_status.stage_summary({'status': 'running', 'active_task_id': 'current'}, ready, [active])['code'], 'queued')
        self.assertEqual(task_status.stage_summary({'status': 'ready'}, {'ready': True}, [])['code'], 'ready')


class IntegrationPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.folder = self.root / '.model-desk/integrations'
        self.task = {'id': 'task-a', 'kind': 'edit', 'status': 'completed', 'engine': 'pi',
                     'goal_id': 'goal-a', 'project': str(self.root), 'workspace': {'commit': 'a' * 40}}
        self.record = {'schema': 1, 'kind': 'integration-acceptance', 'name': 'batch-a',
            'project': str(self.root), 'goal_id': 'goal-a', 'task_ids': ['task-a'],
            'commits': {'task-a': 'a' * 40}, 'integrated_commit': 'b' * 40,
            'verdict': 'passed', 'accepted': False}

    def write(self, record=None, name='batch-a'):
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / (name + '.json')).write_text(json.dumps(record or self.record), encoding='utf-8')

    def context(self, task=None):
        return integration_status.for_task(integration_status.read_project(str(self.root)), task or self.task)

    def test_empty_project_read_creates_no_store_or_git(self):
        self.assertEqual(self.context()['state'], 'none')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unresolvable_record_path_does_not_break_task_status(self):
        with patch.object(Path, 'resolve', side_effect=RuntimeError('link loop')):
            self.assertFalse(integration_status.read_project(str(self.root))['available'])

    def test_pending_accepted_conflict_and_stale_inputs(self):
        self.write()
        self.assertEqual(self.context()['state'], 'awaiting_review')
        self.write({**self.record, 'accepted': True, 'accepted_commit': 'b' * 40})
        self.assertEqual(self.context()['state'], 'accepted')
        self.assertEqual(self.context({**self.task, 'workspace': {'commit': 'c' * 40}})['state'], 'stale')
        self.write({**self.record, 'verdict': 'merge_conflict'})
        self.assertEqual(self.context()['state'], 'conflict')

    def test_multiple_mixed_integrations_are_not_arbitrarily_collapsed(self):
        self.write()
        self.write({**self.record, 'name': 'batch-b', 'accepted': True, 'accepted_commit': 'b' * 40}, 'batch-b')
        self.assertEqual(self.context()['state'], 'multiple')
        self.assertEqual(len(self.context()['records']), 2)

    def test_valid_dotted_integration_name_and_strict_schema(self):
        record = {**self.record, 'name': 'release.1'}
        self.write(record, 'release.1')
        self.assertEqual(self.context()['state'], 'awaiting_review')
        self.write({**record, 'schema': True}, 'release.1')
        self.assertEqual(self.context()['state'], 'unavailable')

    def test_corrupt_cross_project_or_linked_records_stay_unavailable(self):
        self.write({**self.record, 'project': str(self.root / 'other-project')})
        self.assertEqual(self.context()['state'], 'unavailable')
        (self.folder / 'batch-a.json').write_text('{bad', encoding='utf-8')
        self.assertEqual(self.context()['state'], 'unavailable')
        self.write()
        os.link(self.folder / 'batch-a.json', self.root / 'same-resource.json')
        self.assertEqual(self.context()['state'], 'unavailable')

    def test_callers_binding_beats_cached_task_project_and_no_implicit_scope_is_read(self):
        self.write()
        task = {**self.task, 'project': str(self.root / 'wrong-cached-project')}
        with patch.object(integration_status, 'read_project', wraps=integration_status.read_project) as read:
            result = execution_result.project_with_context(task, context_project=str(self.root))
            self.assertEqual(result['integration']['state'], 'awaiting_review')
            read.assert_called_once_with(str(self.root))
        with patch.object(integration_status, 'read_project', side_effect=AssertionError('no implicit read')):
            self.assertEqual(execution_result.project_with_context(task)['integration']['state'], 'unavailable')

    def test_request_local_cache_does_not_reread_same_project(self):
        self.write()
        cache = {}
        with patch.object(integration_status, 'read_project', wraps=integration_status.read_project) as read:
            for _ in range(3):
                execution_result.project_with_context(self.task, context_project=str(self.root), integration_cache=cache)
            self.assertEqual(read.call_count, 1)
