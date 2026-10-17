"""Isolated tests for workflow_assist: packet, profiles, threshold, context, calibration.

Everything runs against a temporary registry/project with mocked "would run a
command" hooks.  No model call, no network, no shell execution, no real task or
review record is touched.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delegation_policy as policy
import failure_classification as fc
import model_registry as registry
import model_router as router
import project_storage
import stage_store
import task_handover
import workflow_assist as wa


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

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def task(self, name, acceptance=('A', 'B'), project=None, **fields):
        run = registry.prepare_task_dir(name, project) if project else registry.task_dir(name)
        row = {'id': name, 'title': 'fixture ' + name, 'status': 'completed', 'kind': 'edit',
               'engine': 'dsh', 'model_id': 'home', 'model': 'qwen-test',
               'project': str(project or self.project), 'files': ['a.py'],
               'changed_files': ['a.py'], 'model_calls': 0,
               'route': {'mode': 'manual', 'requirements': {'acceptance': list(acceptance)}},
               'delegation': {'schema': 1, 'mode': 'outcome', 'rework_count': 0,
                              'inherited_rework_count': 0}}
        row.update(fields)
        registry.patch_task(run, **row)
        return run

    def accept(self, run, source='reviewer_test'):
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 0, 'output': 'ok'}})
        record = policy.load(run)
        rows = [{'id': item['id'], 'status': 'pass',
                 'evidence': [{'source': source, 'detail': 'independent fixture verification',
                               'exit_code': 0}]}
                for item in record['criteria']]
        return policy.accept(run, 'main', rows, 'independent verification')

    def goal_with_stage(self, title='stage', depends=(), files=('a.py',)):
        goal = stage_store.create_goal('Goal ' + title, 'persistent goal', str(self.project))
        stage = stage_store.add_stage(goal['id'], title, 'deliver ' + title,
                                      depends_on=list(depends), files=list(files),
                                      acceptance=['criterion one', 'criterion two'],
                                      candidates=['home'], fixed_model='home')
        return goal, stage


class DeliveryPacketTests(Base):
    def test_execution_and_acceptance_are_separate(self):
        run = self.task('p-separate')
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 0, 'output': 'worker ran it'}})
        packet = wa.delivery_packet(run.name)
        self.assertEqual(packet['status'], 'ok')
        self.assertEqual(packet['execution']['task_status'], 'completed')
        self.assertEqual(packet['execution']['executor_status'], 'completed')
        self.assertFalse(packet['acceptance']['independently_accepted'])
        self.assertEqual(packet['acceptance']['state'], policy.EXECUTOR_COMPLETED)
        self.assertEqual(packet['execution']['changed_files'], ['a.py'])
        self.assertEqual(packet['execution']['worker_test_exit_code'], 0)
        # A worker's own test result lives on the executor side, not as evidence.
        self.assertEqual(packet['evidence']['independent_count'], 0)
        self.assertEqual(packet['evidence']['executor_report']['worker_test_exit_code'], 0)
        self.assertFalse(packet['handover']['present'])

    def test_independent_evidence_and_claims_stay_apart(self):
        run = self.task('p-evidence')
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 1, 'output': 'failed first'}})
        record = policy.load(run)
        record['criteria'][0]['status'] = policy.FAIL
        record['criteria'][0]['evidence'] = [
            {'source': 'executor_report', 'detail': 'worker claims pass', 'exit_code': None}]
        record['criteria'][1]['status'] = policy.PASS
        record['criteria'][1]['evidence'] = [
            {'source': 'reviewer_test', 'detail': 'reviewer reran the test', 'exit_code': 0}]
        policy.save(run, record)

        packet = wa.delivery_packet(run.name)
        self.assertEqual(packet['evidence']['claim_count'], 1)
        self.assertEqual(packet['evidence']['independent_count'], 1)
        self.assertEqual(packet['evidence']['executor_claims'][0]['source'], 'executor_report')
        self.assertEqual(packet['evidence']['independent'][0]['source'], 'reviewer_test')
        self.assertEqual([item['id'] for item in packet['failed_criteria']], ['c1'])
        self.assertEqual(packet['unverified_criteria'], [])

    def test_missing_review_stays_unknown_never_passed(self):
        run = self.task('p-missing')
        packet = wa.delivery_packet(run.name)
        self.assertIsNone(packet['acceptance']['state'])
        self.assertFalse(packet['acceptance']['independently_accepted'])
        self.assertIn('acceptance', packet['unknowns'])
        self.assertEqual(packet['evidence']['independent_count'], 0)

    def test_corrupt_review_is_error_not_passed(self):
        run = self.task('p-corrupt')
        (run / 'review.json').write_text('{not json', encoding='utf-8')
        packet = wa.delivery_packet(run.name)
        self.assertEqual(packet['acceptance']['state'], 'error')
        self.assertFalse(packet['acceptance']['independently_accepted'])
        self.assertTrue(packet['errors'])

    def test_packet_excludes_prompt_and_secrets(self):
        run = self.task('p-secret', prompt='SECRET-PROMPT-TEXT',
                        api_key='sk-should-not-appear')
        packet = wa.delivery_packet(run.name)
        text = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn('SECRET-PROMPT-TEXT', text)
        self.assertNotIn('sk-should-not-appear', text)
        self.assertIn('context_pointers', packet)

    def test_packet_by_task_id_with_explicit_project(self):
        run = self.task('p-scoped', project=self.project)
        packet = wa.delivery_packet('p-scoped', str(self.project))
        self.assertEqual(packet['identity']['task_id'], 'p-scoped')
        self.assertEqual(packet['status'], 'ok')

    def test_accepted_without_evidence_is_not_a_pass(self):
        run = self.task('p-empty-accept')
        (run / 'review.json').write_text(json.dumps(
            {'schema': 1, 'task_id': run.name, 'state': 'accepted',
             'rework_count': 0, 'criteria': []}), encoding='utf-8')
        packet = wa.delivery_packet(run.name)
        self.assertFalse(packet['acceptance']['independently_accepted'])
        self.assertFalse(packet['acceptance']['trusted'])
        self.assertTrue(packet['errors'])
        self.assertIn('acceptance_evidence', packet['unknowns'])

    def test_accepted_with_executor_only_evidence_is_not_a_pass(self):
        run = self.task('p-claim-accept')
        (run / 'review.json').write_text(json.dumps(
            {'schema': 1, 'task_id': run.name, 'state': 'accepted', 'rework_count': 0,
             'criteria': [{'id': 'c1', 'text': 'only', 'status': 'pass',
                           'evidence': [{'source': 'executor_report', 'detail': 'self claim',
                                         'exit_code': 0}]}]}), encoding='utf-8')
        packet = wa.delivery_packet(run.name)
        self.assertFalse(packet['acceptance']['independently_accepted'])
        self.assertTrue(any('独立证据' in item for item in packet['acceptance']['problems']))

    def test_renamed_task_identity_is_error_not_ok(self):
        run = self.task('p-renamed')
        row = registry.read_task(run)
        registry.patch_task(run, **{**row, 'id': 'another-task'})
        packet = wa.delivery_packet(run.name)
        self.assertEqual(packet['status'], 'error')
        self.assertTrue(packet['identity']['identity_mismatch'])
        self.assertEqual(packet['identity']['record_task_id'], 'another-task')
        self.assertTrue(packet['errors'])

    def test_review_task_id_mismatch_is_not_a_pass(self):
        run = self.task('p-review-id')
        self.accept(run)
        record = policy.load(run)
        record['task_id'] = 'some-other-task'
        policy.save(run, record)
        packet = wa.delivery_packet(run.name)
        self.assertFalse(packet['acceptance']['independently_accepted'])
        self.assertTrue(any('task_id' in item for item in packet['acceptance']['problems']))
        self.assertEqual(packet['evidence']['independent_count'], 0)
        self.assertFalse(packet['evidence']['independent_trusted'])
        self.assertTrue(packet['evidence']['untrusted_independent'])

    def test_panel_hint_review_is_never_verified(self):
        hint = {'task_id': 'hint-task', 'title': 'hint',
                'review': {'state': 'accepted', 'task_id': 'hint-task'}}
        packet = wa.delivery_packet('hint-task', task=hint)
        self.assertFalse(packet['acceptance']['independently_accepted'])
        self.assertIn('local_task_record_missing', packet['unknowns'])
        self.assertTrue(any('面板回执' in item for item in packet['acceptance']['problems']))

    def test_malformed_review_fields_do_not_crash_or_pass(self):
        payloads = (
            {'schema': 1, 'state': 'accepted', 'criteria': 'oops'},
            {'schema': 1, 'state': 'accepted',
             'criteria': [{'id': 'c1', 'status': 'pass', 'evidence': 'oops'}]},
            {'schema': 1, 'state': 'accepted',
             'criteria': [{'id': 'c1', 'status': 'pass',
                           'evidence': [{'source': 'reviewer_test', 'detail': 'ok',
                                         'exit_code': '0'}]}]},
            {'schema': 1, 'state': 'accepted', 'criteria': [None, 7]},
        )
        for index, payload in enumerate(payloads):
            with self.subTest(index=index):
                run = self.task('p-malformed-' + str(index))
                (run / 'review.json').write_text(json.dumps(payload), encoding='utf-8')
                packet = wa.delivery_packet(run.name)
                self.assertFalse(packet['acceptance']['independently_accepted'])
                self.assertTrue(packet['errors'])


class AcceptancePlanTests(Base):
    def test_known_scope_selects_the_smallest_profile(self):
        plan = wa.acceptance_plan(files=['model_router.py'])
        self.assertEqual([row['id'] for row in plan['selected_profiles']], ['routing-budgets'])
        self.assertFalse(plan['unknown_scope'])
        self.assertFalse(plan['conservative'])
        commands = plan['selected_profiles'][0]['independent_commands']
        self.assertTrue(commands)
        self.assertTrue(all(isinstance(row['argv'], list) for row in commands))

    def test_mixed_known_and_unknown_files_cover_gaps_conservatively(self):
        plan = wa.acceptance_plan(files=['dispatch_task.py', 'unfamiliar/new.py'])
        ids = [row['id'] for row in plan['selected_profiles']]
        self.assertIn('waiter', ids)
        self.assertIn('general', ids)
        self.assertTrue(plan['unknown_scope'])
        self.assertTrue(plan['conservative'])
        self.assertEqual(plan['unmatched_files'], ['unfamiliar/new.py'])

    def test_front_end_binding_files_are_actually_covered(self):
        plan = wa.acceptance_plan(files=['panel/codex-bridge.js'])
        ids = [row['id'] for row in plan['selected_profiles']]
        self.assertIn('panel-binding', ids)
        self.assertFalse(plan['unknown_scope'])
        self.assertEqual(plan['unmatched_files'], [])
        self.assertEqual(plan['scope_coverage'][0]['match'], 'path')

    def test_multiple_subsystems_expand_conservatively(self):
        plan = wa.acceptance_plan(files=['model_router.py', 'dispatch_task.py'])
        ids = [row['id'] for row in plan['selected_profiles']]
        self.assertIn('routing-budgets', ids)
        self.assertIn('waiter', ids)
        self.assertIn('integration', ids)
        self.assertIn('integration', plan['expanded_profiles'])
        self.assertTrue(plan['conservative'])

    def test_unknown_scope_falls_back_to_general(self):
        plan = wa.acceptance_plan()
        self.assertTrue(plan['unknown_scope'])
        self.assertTrue(plan['conservative'])
        self.assertEqual([row['id'] for row in plan['selected_profiles']], ['general'])

    def test_selection_is_deterministic(self):
        first = wa.acceptance_plan(files=['stage_store.py', 'task_handover.py'])
        second = wa.acceptance_plan(files=['stage_store.py', 'task_handover.py'])
        self.assertEqual(first, second)
        self.assertIn('stage-handover', [row['id'] for row in first['selected_profiles']])

    def test_task_scope_comes_from_the_real_record(self):
        self.task('plan-task', changed_files=['dispatch_task.py'], files=['dispatch_task.py'])
        plan = wa.acceptance_plan(task_id='plan-task')
        self.assertEqual(plan['files'], ['dispatch_task.py'])
        self.assertIn('waiter', [row['id'] for row in plan['selected_profiles']])

    def test_never_executes_supplied_commands(self):
        profiles = {
            'schema': 1,
            'fallback_profile': 'general',
            'profiles': [
                {'id': 'evil', 'title': 'evil', 'match': {'files': ['x.py']},
                 'worker_tests': [],
                 'independent_commands': [{'purpose': 'injected', 'command': 'echo; rm -rf /'}],
                 'risk_triggers': []},
                {'id': 'general', 'title': 'general', 'match': {},
                 'independent_commands': [{'purpose': 'fallback', 'command': 'echo safe'}],
                 'worker_tests': [], 'risk_triggers': []},
            ],
        }
        with mock.patch.object(subprocess, 'run', side_effect=AssertionError('must not run')), \
             mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('must not run')), \
             mock.patch.object(os, 'system', side_effect=AssertionError('must not run')):
            plan = wa.acceptance_plan(files=['x.py'], profiles=profiles)
        self.assertFalse(plan['executes_commands'])
        self.assertFalse(plan['marks_acceptance'])
        self.assertEqual(plan['selected_profiles'][0]['id'], 'evil')
        self.assertEqual(plan['selected_profiles'][0]['independent_commands'][0]['command'],
                         'echo; rm -rf /')

    def test_bad_profile_file_is_rejected(self):
        broken = Path(self.temp.name) / 'broken.json'
        broken.write_text('{"schema": 2, "profiles": []}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '结构不正确'):
            wa.load_profiles(broken)
        missing = Path(self.temp.name) / 'missing.json'
        with self.assertRaisesRegex(ValueError, '不存在'):
            wa.load_profiles(missing)


class DelegationPlanTests(Base):
    def test_consequential_work_requires_gpt_decision_first(self):
        for key in wa.CONSEQUENTIAL_KEYS:
            with self.subTest(key=key):
                result = wa.delegation_plan({'trivial': True, 'capability_judged': True, key: True})
                self.assertEqual(result['recommendation'], 'gpt_decision_first')
                self.assertTrue(result['rationale'])

    def test_unknown_ambiguity_requires_gpt_first(self):
        result = wa.delegation_plan({'scoped': True, 'understandable': True,
                                     'capability_judged': True, 'policy_allowed': True,
                                     'expected_benefit': True, 'ambiguity': 'unknown'})
        self.assertEqual(result['recommendation'], 'gpt_decision_first')

    def test_trivial_work_stays_with_gpt(self):
        result = wa.delegation_plan({'trivial': True, 'ambiguity': 'low'})
        self.assertEqual(result['recommendation'], 'gpt_owned')

    def test_scoped_capable_work_is_a_candidate(self):
        result = wa.delegation_plan({'scoped': True, 'understandable': True,
                                     'capability_judged': True, 'policy_allowed': True,
                                     'expected_benefit': True, 'ambiguity': 'low'})
        self.assertEqual(result['recommendation'], 'delegate_candidate')

    def test_missing_capability_judgement_stays_with_gpt(self):
        result = wa.delegation_plan({'scoped': True, 'understandable': True,
                                     'policy_allowed': True, 'expected_benefit': True,
                                     'ambiguity': 'low', 'capability_judged': False})
        self.assertEqual(result['recommendation'], 'gpt_owned')

    def test_aid_never_claims_to_replace_gates(self):
        result = wa.delegation_plan({'trivial': True})
        for gate in ('authorization', 'budget', 'capability'):
            self.assertIn(gate, result['does_not_replace_gates'])
        self.assertTrue(result['declarations_are_not_verification'])
        self.assertTrue(result['context_size_is_not_ability'])
        self.assertTrue(result['read_only'])


class StageContextTests(Base):
    def pending_pair(self, title='pair'):
        goal, first = self.goal_with_stage(title=title + '-first')
        second = stage_store.add_stage(goal['id'], title + '-second', 'depends on first',
                                       depends_on=[first['id']], files=['a.py'],
                                       acceptance=['second criterion'], candidates=['home'])
        run = self.task('t-' + title + '-first', project=self.project,
                        stage_id=first['id'], goal_id=goal['id'])
        stage_store.claim_dispatch(first['id'], 't-' + title + '-first', project=str(self.project))
        return goal, first, second, run

    def test_missing_prerequisite_handover_blocks(self):
        goal, first, second, run = self.pending_pair('missing')
        self.accept(run)
        stage_store.sync_from_tasks(project=str(self.project))
        context = wa.stage_context(stage_id=second['id'], project=str(self.project))
        self.assertEqual(context['status'], 'blocked')
        self.assertFalse(context['resumable'])
        self.assertIn('成果交接', context['prerequisites']['error'])

    def test_valid_prerequisite_context_is_compact_and_verified(self):
        goal, first, second, run = self.pending_pair('valid')
        self.accept(run)
        stage_store.sync_from_tasks(project=str(self.project))
        task_handover.create(run, summary='first deliverable', interfaces=['one() -> int'],
                             decisions=['kept signature'], remaining_issues=['none'],
                             context=['a.py'],
                             test_evidence=[{'name': 'unittest', 'source': 'reviewer_test',
                                             'exit_code': 0}],
                             stage_id=first['id'], goal_id=goal['id'])
        context = wa.stage_context(stage_id=second['id'], project=str(self.project))
        self.assertEqual(context['status'], 'ok')
        self.assertTrue(context['resumable'])
        entry = context['prerequisites']['items'][0]
        self.assertEqual(entry['summary'], 'first deliverable')
        self.assertIn('one() -> int', entry['interfaces'])
        self.assertIn('kept signature', entry['decisions'])
        self.assertTrue(entry['summary_is_claim'])
        self.assertEqual(entry['files'][0]['path'], 'a.py')
        self.assertTrue(context['independent_evidence'])
        self.assertEqual(context['stage']['acceptance'], ['second criterion'])
        self.assertEqual(context['goal']['title'], 'Goal valid-first')

    def test_stale_handover_is_refused(self):
        goal, first, second, run = self.pending_pair('stale')
        self.accept(run)
        stage_store.sync_from_tasks(project=str(self.project))
        task_handover.create(run, summary='deliverable', stage_id=first['id'], goal_id=goal['id'])
        (self.project / 'a.py').write_text('def one():\n    return 99\n', encoding='utf-8')
        context = wa.stage_context(stage_id=second['id'], project=str(self.project))
        self.assertEqual(context['status'], 'blocked')
        self.assertIn('失效', context['prerequisites']['error'])

    def test_corrupt_handover_is_refused(self):
        goal, first, second, run = self.pending_pair('corrupt')
        self.accept(run)
        stage_store.sync_from_tasks(project=str(self.project))
        (run / 'handover.json').write_text('{broken', encoding='utf-8')
        context = wa.stage_context(stage_id=second['id'], project=str(self.project))
        self.assertEqual(context['status'], 'blocked')
        self.assertTrue(context['prerequisites']['error'])

    def test_task_without_stage_is_an_explicit_error(self):
        run = self.task('t-no-stage')
        context = wa.stage_context(task_id='t-no-stage')
        self.assertEqual(context['status'], 'error')
        self.assertFalse(context['resumable'])
        self.assertIn('packet', ' '.join(context['errors']))

    def test_resume_by_task_id_never_leaks_the_prompt(self):
        goal, first = self.goal_with_stage(title='resume')
        run = self.task('t-resume', project=self.project, stage_id=first['id'],
                        goal_id=goal['id'], prompt='SECRET-CHAT-HISTORY')
        stage_store.claim_dispatch(first['id'], 't-resume', project=str(self.project))
        self.accept(run)
        stage_store.sync_from_tasks(project=str(self.project))
        task_handover.create(run, summary='resume deliverable', stage_id=first['id'],
                             goal_id=goal['id'])
        context = wa.stage_context(task_id='t-resume', project=str(self.project))
        self.assertEqual(context['status'], 'ok')
        self.assertEqual(context['stage']['id'], first['id'])
        self.assertNotIn('SECRET-CHAT-HISTORY', json.dumps(context, ensure_ascii=False))
        self.assertNotIn('prompt', context)

    def test_own_stale_handover_blocks_resume_and_hides_summary(self):
        goal, first, second, run = self.pending_pair('own-stale')
        self.accept(run)
        stage_store.sync_from_tasks(project=str(self.project))
        task_handover.create(run, summary='OUTDATED-CLAIM', stage_id=first['id'],
                             goal_id=goal['id'])
        (self.project / 'a.py').write_text('changed after acceptance', encoding='utf-8')
        context = wa.stage_context(stage_id=first['id'], project=str(self.project))
        self.assertEqual(context['status'], 'blocked')
        self.assertFalse(context['resumable'])
        self.assertEqual(context['stale_current_handovers'], [run.name])
        row = context['stage_handovers'][0]
        self.assertFalse(row['handover']['trusted'])
        self.assertEqual(row['handover']['summary'], '')
        self.assertEqual(row['handover']['stale_summary'], 'OUTDATED-CLAIM')
        self.assertEqual(context['independent_evidence'], [])
        self.assertTrue(context['blocked_reasons'])

    def test_own_corrupt_handover_blocks_resume(self):
        goal, first, second, run = self.pending_pair('own-corrupt')
        self.accept(run)
        stage_store.sync_from_tasks(project=str(self.project))
        (run / 'handover.json').write_text('{broken', encoding='utf-8')
        context = wa.stage_context(stage_id=first['id'], project=str(self.project))
        self.assertEqual(context['status'], 'blocked')
        self.assertFalse(context['resumable'])
        self.assertEqual(context['stale_current_handovers'], [run.name])

    def test_own_accepted_without_handover_blocks_resume(self):
        goal, first, second, run = self.pending_pair('own-empty')
        self.accept(run)
        stage_store.sync_from_tasks(project=str(self.project))
        context = wa.stage_context(stage_id=first['id'], project=str(self.project))
        self.assertEqual(context['status'], 'blocked')
        self.assertFalse(context['resumable'])

    def test_contradictory_task_and_stage_identity_is_rejected(self):
        goal, first, second, run = self.pending_pair('contradiction')
        self.accept(run)
        stage_store.sync_from_tasks(project=str(self.project))
        context = wa.stage_context(stage_id=second['id'], task_id=run.name,
                                   project=str(self.project))
        self.assertEqual(context['status'], 'error')
        self.assertFalse(context['resumable'])
        self.assertTrue(context['errors'])

    def test_task_in_another_project_is_rejected(self):
        goal, first, second, run = self.pending_pair('cross-project')
        self.accept(run)
        other = registry.HERE / 'other'
        other.mkdir()
        context = wa.stage_context(task_id=run.name, project=str(other))
        self.assertEqual(context['status'], 'error')
        self.assertFalse(context['resumable'])
        self.assertNotIn('stage_handovers', context)


class CalibrationTests(Base):
    def final_accepted_fixture(self, name, *, model_id='m1', category='code_small',
                               parent=None, project=True):
        run = self.task(name, acceptance=['only'], project=self.project if project else None,
                        model_id=model_id, status='failed',
                        route={'mode': 'manual',
                               'requirements': {'acceptance': ['only'], 'category': category}},
                        **({'parent_task_id': parent} if parent else {}))
        policy.record_executor(run, {'completed': False, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 1, 'output': 'executor failed'}})
        record = policy.load(run)
        rows = [{'id': item['id'], 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'main reran', 'exit_code': 0}]}
                for item in record['criteria']]
        policy.final_accept(run, 'main', rows, 'main took over and fixed a.py')
        return run

    def classification_row(self, run):
        record = registry.read_task(run)
        review = policy.load(run)
        return {**record, 'review': policy.panel_summary(review)}

    def test_final_accept_is_not_executor_success(self):
        run = self.final_accepted_fixture('F1')
        row = self.classification_row(run)
        finding = fc.classify(row)
        self.assertEqual(finding['outcome'], fc.OUTCOME_UNKNOWN)
        self.assertEqual(finding['reason'], 'main_takeover_final_accept')
        self.assertFalse(finding['counts_as_model_quality'])
        self.assertFalse(finding['verified'])

        report = fc.classify_history([row])
        self.assertEqual(report['families'][run.name]['outcome'], fc.OUTCOME_UNKNOWN)
        self.assertFalse(report['families'][run.name]['counts_as_model_quality'])

        risk = router.rework_risk([row], 'm1', 'code_small')
        self.assertEqual(risk['verified_samples'], 0)
        self.assertEqual(risk['quality_rework_families'], 0)
        self.assertEqual(risk['excluded']['unreviewed_families'], 1)
        self.assertEqual(risk['retry_factor'], 1.0)

    def test_final_accept_parent_with_accepted_rework_counts_once_as_success(self):
        parent = self.final_accepted_fixture('F2')
        child = self.task('F2-rework', acceptance=['only'], project=self.project,
                          model_id='m1', parent_task_id='F2',
                          route={'mode': 'manual',
                                 'requirements': {'acceptance': ['only'], 'category': 'code_small'}})
        self.accept(child)
        rows = [self.classification_row(parent), self.classification_row(child)]
        report = fc.classify_history(rows)
        self.assertEqual(report['families']['F2']['outcome'], fc.OUTCOME_SUCCESS)
        risk = router.rework_risk(rows, 'm1', 'code_small', report=report)
        self.assertEqual(risk['verified_samples'], 1)
        self.assertEqual(risk['quality_rework_families'], 0)

    def test_calibration_summary_is_read_only_and_excludes_unknown(self):
        returned = self.task('Q1', acceptance=['only'], project=self.project,
                             model_id='m1', status='completed',
                             route={'mode': 'manual',
                                    'requirements': {'acceptance': ['only'], 'category': 'code_small'}})
        policy.record_executor(returned, {'completed': True, 'changed_files': ['a.py'],
                                          'test': {'exit_code': 0}})
        policy.request_rework(returned, 'main', ['c1'], 'reviewer saw a gap', 'no scope change')

        self.task('S1', acceptance=['only'], project=self.project, model_id='m1', status='failed',
                  error='Connection error: timed out',
                  route={'mode': 'manual',
                         'requirements': {'acceptance': ['only'], 'category': 'code_small'}})
        self.task('U1', acceptance=['only'], project=self.project, model_id='m1', status='failed',
                  route={'mode': 'manual',
                         'requirements': {'acceptance': ['only'], 'category': 'code_small'}})
        self.final_accepted_fixture('F1')

        summary = wa.calibration_summary(str(self.project))
        self.assertTrue(summary['read_only'])
        self.assertEqual(summary['excluded_from_quality_denominator'], ['system', 'cancelled', 'unknown'])
        self.assertEqual(summary['main_takeover_count'], 1)
        self.assertIn('F1', summary['main_takeover_families'])
        self.assertNotIn('F1', summary['executor_success_representative_task_ids'])
        self.assertTrue(summary['notes'])
        self.assertNotIn('节省', json.dumps(summary['scopes'], ensure_ascii=False))
        scope = [row for row in summary['scopes'] if row['model_id'] == 'm1'
                 and row['category'] == 'code_small']
        self.assertTrue(scope)
        self.assertTrue(scope[0]['insufficient_samples'])

    def test_calibration_never_scans_other_projects(self):
        other = registry.HERE / 'other'
        other.mkdir()
        run = self.task('outside', acceptance=['only'], model_id='m1', status='failed',
                        route={'mode': 'manual',
                               'requirements': {'acceptance': ['only'], 'category': 'code_small'}})
        registry.patch_task(run, project=str(other))
        summary = wa.calibration_summary(str(self.project))
        self.assertNotIn('outside', summary['quality_representative_task_ids'])
        self.assertEqual(summary['records'], 0)

    def test_calibration_requires_an_explicit_project(self):
        with self.assertRaisesRegex(ValueError, 'project'):
            wa.calibration_summary('')

    def test_corrupt_review_never_inherits_embedded_accept(self):
        run = self.task('cal-corrupt', acceptance=['only'], project=self.project,
                        model_id='m1', status='completed',
                        route={'mode': 'manual',
                               'requirements': {'acceptance': ['only'], 'category': 'code_small'}},
                        review={'state': 'accepted', 'executor_status': 'completed'})
        (run / 'review.json').write_text('{corrupt', encoding='utf-8')
        summary = wa.calibration_summary(str(self.project), model_id='m1')
        self.assertEqual(summary['verified_families'], 0)
        self.assertGreaterEqual(summary['unreadable_records'], 1)
        scopes = [row for row in summary['scopes'] if row['model_id'] == 'm1']
        self.assertTrue(scopes)
        self.assertEqual(scopes[0]['verified_samples'], 0)
        self.assertEqual(scopes[0]['quality_rework_families'], 0)

    def test_missing_review_with_stale_embed_is_unknown(self):
        self.task('cal-missing', acceptance=['only'], project=self.project,
                  model_id='m1', status='completed',
                  route={'mode': 'manual',
                         'requirements': {'acceptance': ['only'], 'category': 'code_small'}},
                  review={'state': 'accepted', 'executor_status': 'completed'})
        summary = wa.calibration_summary(str(self.project), model_id='m1')
        self.assertEqual(summary['verified_families'], 0)
        self.assertGreaterEqual(summary['stale_embedded_review_records'], 1)

    def test_incoherent_accepted_review_is_not_verified(self):
        run = self.task('cal-incoherent', acceptance=['only'], project=self.project,
                        model_id='m1', status='completed',
                        route={'mode': 'manual',
                               'requirements': {'acceptance': ['only'], 'category': 'code_small'}})
        (run / 'review.json').write_text(json.dumps(
            {'schema': 1, 'task_id': run.name, 'state': 'accepted',
             'rework_count': 0, 'criteria': []}), encoding='utf-8')
        summary = wa.calibration_summary(str(self.project), model_id='m1')
        self.assertEqual(summary['verified_families'], 0)
        self.assertGreaterEqual(summary['incoherent_accepted_reviews'], 1)

    def test_project_wide_totals_are_labelled_when_filtering(self):
        self.task('cal-scope', acceptance=['only'], project=self.project,
                  model_id='m1', status='failed',
                  route={'mode': 'manual',
                         'requirements': {'acceptance': ['only'], 'category': 'code_small'}})
        summary = wa.calibration_summary(str(self.project), model_id='m1')
        self.assertEqual(summary['totals_scope'], 'project_wide_all_models_and_categories')
        self.assertIn('filter', summary)


class DispatchPacketTests(Base):
    def test_quiet_wait_carries_the_packet_and_verbose_result_does_not(self):
        import contextlib
        import io
        import dispatch_task as dispatch

        run = self.task('wait-packet')
        self.accept(run)
        state = {'id': 'wait-packet', 'status': 'completed', 'phase': '',
                 'model': 'qwen-test', 'engine': 'dsh', 'project': str(self.project)}

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = dispatch.wait_for_task(lambda path: {'task': state}, 'wait-packet')
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(code, 0)
        self.assertIn('packet', rows[0])
        self.assertTrue(rows[0]['packet']['acceptance']['independently_accepted'])
        self.assertEqual(rows[0]['packet']['identity']['task_id'], 'wait-packet')

        verbose = io.StringIO()
        with contextlib.redirect_stdout(verbose):
            dispatch.wait_for_task(lambda path: {'task': state}, 'wait-packet', verbose=True)
        self.assertNotIn('packet', json.loads(verbose.getvalue()))


if __name__ == '__main__':
    unittest.main()
