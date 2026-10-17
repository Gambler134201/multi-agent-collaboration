"""Offline negative tests for the controlled DSH native subagent runtime (D2).

Nothing here starts DSH, calls a model or creates a real child.  The probe
orchestration is exercised with a fake executor that only writes the ledger and
task record a real run would have produced, so the gate, identity binding,
lineage pairing, cancel flag and aggregate billing can be checked without spend.
"""
import json
from pathlib import Path
import hashlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import budget_relay
import dsh_worker
import executor_adapter
import model_capabilities as caps
import model_registry as registry
import preset_runtime
import subagent_runtime as runtime


def first_tranche_fixture(root, runs):
    """Write a smoke record plus the real review/session records it references."""
    record = {
        'goal_id': 'goal-x', 'first_stage': 'stage-1', 'second_stage': 'stage-2',
        'first_task': 'task-a', 'first_session': 'session-a', 'first_verified': True,
        'second_task': 'task-b', 'second_session': 'session-b', 'second_verified': True,
    }
    (root / 'tasks').mkdir(parents=True, exist_ok=True)
    (root / 'tasks' / 'stage-smoke-record.json').write_text(json.dumps(record), encoding='utf-8')
    for task, session, stage in (('task-a', 'session-a', 'stage-1'), ('task-b', 'session-b', 'stage-2')):
        run = runs / task
        run.mkdir(parents=True, exist_ok=True)
        (run / 'review.json').write_text(json.dumps({
            'schema': 1, 'task_id': task, 'state': 'accepted', 'stage_id': stage, 'goal_id': 'goal-x',
            'executor': {'status': 'completed', 'dsh_session_id': session},
            'criteria': [], 'decisions': [],
        }), encoding='utf-8')


def paid_model(**overrides):
    row = {'id': 'cloud', 'name': 'cloud', 'provider': 'deepseek', 'model': 'deepseek-flash',
           'api_base': 'https://api.deepseek.com', 'agent_preset': 'standard', 'enabled': True,
           'model_version': '', 'executor_version': '0.2.0-rc.2'}
    row.update(overrides)
    return row


class PreconditionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir(parents=True, exist_ok=True)
        first_tranche_fixture(self.root, self.runs)
        self.settings = {'paid_allowed': True, 'enforce_paid_budget': False, 'enabled': True}

    def tearDown(self):
        self.temp.cleanup()

    def pre(self, model, explicit=True, **kwargs):
        return runtime.probe_preconditions(model, settings=kwargs.pop('settings', self.settings),
                                           explicit=explicit, root=self.root, runs=self.runs, **kwargs)

    def test_probe_requires_explicit_flag(self):
        result = self.pre(paid_model(), explicit=False)
        self.assertFalse(result['allowed'])
        self.assertTrue(any('显式' in reason for reason in result['reasons']))

    def test_local_and_home_are_never_probe_subjects(self):
        for row in ({'id': 'home', 'provider': 'local', 'model': 'qwen', 'api_base': 'http://127.0.0.1/v1', 'enabled': True},
                    {'id': 'home', 'provider': 'custom', 'model': 'qwen', 'api_base': 'http://127.0.0.1/v1', 'enabled': True}):
            result = self.pre(row)
            self.assertFalse(result['allowed'])
            self.assertTrue(any('home/本地' in reason for reason in result['reasons']))

    def test_probe_refuses_when_paid_not_allowed(self):
        result = self.pre(paid_model(), settings={'paid_allowed': False, 'enabled': True})
        self.assertFalse(result['allowed'])
        self.assertTrue(any('付费授权' in reason for reason in result['reasons']))

    def test_probe_refuses_without_a_verified_first_tranche(self):
        (self.root / 'tasks' / 'stage-smoke-record.json').write_text(json.dumps({'first_verified': False}), encoding='utf-8')
        result = self.pre(paid_model())
        self.assertFalse(result['allowed'])
        self.assertTrue(any('两阶段交接' in reason for reason in result['reasons']))

    def test_first_tranche_requires_real_review_records(self):
        (self.runs / 'task-a' / 'review.json').write_text(json.dumps({'state': 'running'}), encoding='utf-8')
        tranche = runtime.first_tranche_verified(root=self.root, runs=self.runs)
        self.assertFalse(tranche['verified'])
        self.assertTrue(tranche['reasons'])

    def test_probe_refuses_a_missing_endpoint(self):
        result = self.pre(paid_model(api_base=''))
        self.assertFalse(result['allowed'])
        self.assertTrue(any('endpoint' in reason for reason in result['reasons']))

    def test_probe_refuses_paid_model_without_rates(self):
        with mock.patch.object(runtime.model_router, 'is_paid', return_value=True), \
             mock.patch.object(runtime.model_router, 'rates_for', return_value=None):
            result = self.pre(paid_model())
        self.assertFalse(result['allowed'])
        self.assertTrue(any('单价' in reason for reason in result['reasons']))

    def test_all_allowed_for_a_registered_paid_non_local_model(self):
        with mock.patch.object(runtime.model_router, 'is_paid', return_value=True), \
             mock.patch.object(runtime.model_router, 'rates_for', return_value={'output': '4'}):
            result = self.pre(paid_model())
        self.assertTrue(result['allowed'], result['reasons'])
        self.assertFalse(result['checks']['requires_stored_support'])
        self.assertEqual(result['checks']['stored_supported_rows'], 0)

    def test_authorize_probe_raises_with_the_reasons(self):
        with self.assertRaises(runtime.ProbeDenied):
            runtime.authorize_probe(paid_model(), explicit=False, root=self.root, runs=self.runs)


class UnknownAndPolicyTests(unittest.TestCase):
    def test_unknown_model_stays_denied_by_the_normal_gate(self):
        row = paid_model(subagent={'policy': 'allow', 'evidence': []})
        with self.assertRaises(executor_adapter.SubagentDenied):
            executor_adapter.subagent_gate(row, engine='dsh')

    def test_normal_path_cannot_pass_a_probe_marker_as_support(self):
        # `subagent_decision(probe=True)` may only allow when every explicit
        # precondition holds; an ordinary task has no such state and must deny.
        temp = tempfile.TemporaryDirectory()
        try:
            root = Path(temp.name)
            run = root / 'runs' / 't1'
            run.mkdir(parents=True, exist_ok=True)
            row = paid_model(subagent={'policy': 'allow', 'evidence': []})
            with mock.patch.object(dsh_worker.subagent_runtime, 'authorize_probe',
                                   side_effect=runtime.ProbeDenied('前置不满足')):
                decision = dsh_worker.subagent_decision(row, run, probe=True)
            self.assertEqual(decision['decision'], 'deny')
            self.assertEqual(decision['blocked'], '前置不满足')
        finally:
            temp.cleanup()

    def test_child_config_pins_the_parent_route_and_never_forks(self):
        config = runtime.child_config(paid_model())
        self.assertEqual(config['provider'], 'spawn')
        self.assertEqual(config['agentOptions'], {'provider': 'desk-worker', 'model': 'deepseek-flash'})
        self.assertEqual(config['maxDepth'], 1)
        self.assertFalse(config['enableRunInBackground'])
        self.assertFalse(config['modelSelectionSettings'])

    def test_build_patch_never_enables_the_fork_backend(self):
        rows = dsh_worker.build_patch(paid_model(), 'chat', subagent=True)
        by_id = {row['id']: row for row in rows}
        self.assertFalse(by_id['tool-subagent']['disabled'])
        for name in ('tool-subagent-fork', 'tool-subagent-control', 'tool-subagent-list-agents'):
            self.assertTrue(by_id[name]['disabled'])


class IdentityDriftTests(unittest.TestCase):
    def evidence_row(self, **identity):
        return {'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat(),
                'identity': caps.evidence_identity(executor='dsh', model='deepseek-flash',
                                                   preset='standard', version='0.2.0-rc.2', **identity),
                'test_id': 'offline-drift'}

    def test_version_drift_invalidates_a_grant(self):
        row = paid_model(subagent={'policy': 'allow', 'evidence': [self.evidence_row()]})
        current = executor_adapter.model_identity(row, version='0.2.0-rc.3')
        self.assertEqual(caps.evaluate(row, identity=current)['decision'], caps.VERDICT_DENY)

    def test_endpoint_drift_invalidates_a_grant(self):
        row = paid_model(subagent={'policy': 'allow',
                                   'evidence': [self.evidence_row(endpoint='https://api.deepseek.com')]})
        moved = paid_model(api_base='https://mirror.example')
        moved['subagent'] = row['subagent']
        decision = caps.evaluate(moved, identity=executor_adapter.model_identity(moved))
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertIn('endpoint', decision['reasons'][0])

    def test_preset_content_drift_invalidates_a_grant(self):
        row = paid_model(subagent={'policy': 'allow', 'evidence': [self.evidence_row(preset_revision='old')]})
        decision = caps.evaluate(row, identity=executor_adapter.model_identity(row))
        self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        self.assertTrue(any('preset_revision' in reason for reason in decision['reasons']))

    def test_unresolved_preset_is_fail_closed_not_a_wildcard(self):
        saved = caps._PRESET_REVISION_RESOLVER
        caps.register_preset_resolver(None)
        try:
            row = paid_model(subagent={'policy': 'allow', 'evidence': [self.evidence_row()]})
            decision = caps.evaluate(row, identity=executor_adapter.model_identity(row))
            self.assertEqual(decision['decision'], caps.VERDICT_DENY)
        finally:
            caps.register_preset_resolver(saved)


class LifecycleAndBillingTests(unittest.TestCase):
    def test_lifecycle_pairs_start_and_end_and_flags_orphans(self):
        rows = [
            {'type': 'start', 'run_id': 'r1', 'provider': 'spawn', 'child_id': 'child-1', 'local': True,
             'at': '2026-10-14T00:00:00+00:00', 'child_model': 'deepseek-flash'},
            {'type': 'end', 'run_id': 'r1', 'provider': 'spawn', 'child_id': 'child-1', 'local': True,
             'at': '2026-10-14T00:00:05+00:00', 'stop_reason': 'completed', 'output_chars': 2,
             'output_preview': 'ok'},
            {'type': 'start', 'run_id': 'r2', 'provider': 'spawn', 'child_id': 'child-2', 'local': True,
             'at': '2026-10-14T00:01:00+00:00'},
            {'type': 'end', 'run_id': 'unknown', 'provider': 'spawn', 'child_id': 'x', 'local': True},
        ]
        lineage = runtime.lifecycle(rows)
        self.assertEqual(len(lineage['children']), 1)
        child = lineage['children'][0]
        self.assertEqual(child['child_id'], 'child-1')
        self.assertEqual(child['output_preview'], 'ok')
        self.assertEqual(child['stop_reason'], 'completed')
        self.assertEqual(lineage['orphan_starts'], ['r2'])
        self.assertEqual(lineage['unmatched_ends'], ['unknown'])

    def test_child_output_is_read_from_the_executor_tool_result(self):
        event = {'type': 'tool_call', 'status': 'completed', 'title': 'tool call',
                 'content': [{'type': 'content', 'content': {'type': 'text', 'text': 'ok'}}]}
        self.assertEqual(dsh_worker.tool_result_text(event), 'ok')
        self.assertEqual(dsh_worker.tool_result_text({'text': 'tool call (completed): ok'}), 'ok')
        self.assertEqual(dsh_worker.tool_result_text({}), '')

    def test_subagent_events_never_leak_a_private_field(self):
        rows = [{'type': 'start', 'run_id': 'r1', 'provider': 'spawn', 'child_id': 'c1',
                 'api_key': 'sk-private', 'token': 'secret-token', 'at': 'x'}]
        text = json.dumps(runtime.subagent_events(rows), ensure_ascii=False)
        self.assertNotIn('sk-private', text)
        self.assertNotIn('secret-token', text)

    def test_aggregate_usage_keeps_per_child_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            registry.write_json(run / 'task.json', {'cost_requests': {
                'a': {'state': 'settled', 'cost_cny': '0.01', 'billable': True,
                      'at': '2026-10-14T00:00:02+00:00', 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}},
                'b': {'state': 'pending', 'billable': True, 'at': '2026-10-14T00:00:08+00:00',
                      'usage': {'prompt_tokens': 5, 'completion_tokens': 1}},
            }})
            lineage = {'children': [{'child_id': 'c1', 'started_at': '2026-10-14T00:00:01+00:00',
                                     'ended_at': '2026-10-14T00:00:05+00:00'}]}
            usage = runtime.aggregate_usage(run, lineage)
            self.assertEqual(usage['authoritative'], 'aggregate')
            self.assertEqual(usage['per_child'], 'unavailable')
            self.assertEqual(usage['aggregate']['requests'], 2)
            self.assertEqual(usage['aggregate']['total_tokens'], 18)
            self.assertEqual(usage['aggregate']['settled_cny'], 0.01)
            self.assertEqual(usage['child_lifecycle_window_requests'], 1)
            self.assertTrue(usage['acp_used_size_not_used'])
            self.assertNotIn('child_invoice', usage)

    def test_duplicate_settlement_never_double_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            saved = registry.HERE
            registry.HERE = run
            try:
                registry.patch_task(run, cost_settled_cny=0.0, cost_unsettled_cny=0.5,
                                    cost_settled_calls=0, cost_unknown_calls=0,
                                    cost_legacy_unsettled_cny=0.0, cost_schema=2,
                                    cost_requests={'r1': {'schema': 2, 'request_id': 'r1', 'day': '2026-10-14',
                                                          'at': '2026-10-14T00:00:00+00:00', 'period': 'third_party',
                                                          'billable': True, 'rates': {'cache_hit_input': '0.02',
                                                                                      'cache_miss_input': '1', 'output': '4'},
                                                          'ceiling_rates': {}, 'input_bound': 10, 'output_bound': 10,
                                                          'reserved_cny': '0.5', 'state': 'pending', 'usage': None,
                                                          'cost_cny': None}})
                first = budget_relay.settle(run, 'r1', {'prompt_tokens': 10, 'completion_tokens': 2,
                                                        'prompt_cache_hit_tokens': 10,
                                                        'prompt_cache_miss_tokens': 0}, outcome='ok')
                second = budget_relay.settle(run, 'r1', {'prompt_tokens': 10, 'completion_tokens': 2,
                                                         'prompt_cache_hit_tokens': 10,
                                                         'prompt_cache_miss_tokens': 0}, outcome='ok')
                self.assertFalse(first.get('duplicate'))
                self.assertTrue(second.get('duplicate'))
                state = registry.read_task(run)
                self.assertEqual(state['cost_settled_calls'], 1)
            finally:
                registry.HERE = saved

    def test_cancel_writes_the_flag_and_never_redispatches(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            (run / 'task.json').write_text('{}', encoding='utf-8')
            result = executor_adapter.request_cancel(run, reason='test')
            self.assertTrue(result['cancelled'])
            self.assertFalse(result['redispatched'])
            self.assertTrue((run / 'cancelled.flag').is_file())


class ProbeOrchestrationTests(unittest.TestCase):
    """Exercise run_probe with a fake executor; no DSH and no paid call."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir(parents=True, exist_ok=True)
        first_tranche_fixture(self.root, self.runs)
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = self.root
        registry.REGISTRY = self.root / 'models.json'
        registry.RUNS = self.runs
        self.settings = {'paid_allowed': True, 'enforce_paid_budget': False, 'enabled': True}

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def fake_executor(self, model):
        def run_task(run, state, selected):
            registry.patch_task(run, dsh_session_id='parent-session', status='completed',
                                acpx_record_id='rec-1', changed_files=[])
            registry.patch_task(run, cost_requests={
                'p1': {'state': 'settled', 'cost_cny': '0.01', 'billable': True,
                       'at': '2026-10-14T00:00:00+00:00', 'usage': {'prompt_tokens': 40, 'completion_tokens': 5}},
                'c1': {'state': 'settled', 'cost_cny': '0.02', 'billable': True,
                       'at': '2026-10-14T00:00:02+00:00', 'usage': {'prompt_tokens': 12, 'completion_tokens': 2}},
                'p2': {'state': 'settled', 'cost_cny': '0.01', 'billable': True,
                       'at': '2026-10-14T00:00:04+00:00', 'usage': {'prompt_tokens': 30, 'completion_tokens': 4}},
            })
            ledger = runtime.ledger_path(run)
            ledger.write_text('\n'.join(json.dumps(row) for row in [
                {'schema': 1, 'source': 'run-dsh.mjs', 'type': 'parent', 'parent_session': 'parent-session'},
                {'schema': 1, 'source': 'dsh-subagent-lifecycle', 'type': 'start', 'run_id': 'r1',
                 'provider': 'spawn', 'child_id': 'child-session', 'local': True,
                 'child_model': model['model'], 'at': '2026-10-14T00:00:01+00:00'},
                {'schema': 1, 'source': 'dsh-subagent-lifecycle', 'type': 'end', 'run_id': 'r1',
                 'provider': 'spawn', 'child_id': 'child-session', 'local': True,
                 'at': '2026-10-14T00:00:03+00:00', 'stop_reason': 'completed',
                 'output_chars': 2, 'output_preview': 'ok'},
            ]) + '\n', encoding='utf-8')
            registry.patch_task(run, status='completed',
                                subagent_tool_outputs=[{'tool_call_id': 'call-1', 'text': 'ok', 'chars': 2}])
            return 0
        return run_task

    def test_probe_records_identity_output_and_aggregate_without_models_write(self):
        model = paid_model()
        evidence_path = self.root / 'evidence.json'
        with mock.patch.object(runtime.registry, 'get_model', return_value=model), \
             mock.patch.object(runtime.workflow_settings, 'snapshot', return_value=self.settings), \
             mock.patch.object(runtime.model_router, 'is_paid', return_value=True), \
             mock.patch.object(runtime.model_router, 'rates_for', return_value={'output': '4'}), \
             mock.patch.object(dsh_worker, 'run_task', side_effect=self.fake_executor(model)):
            evidence = runtime.run_probe('cloud', evidence_path=evidence_path,
                                         root=self.root, runs=self.runs)
        self.assertTrue(evidence['measured'])
        self.assertTrue(evidence['read_only_probe'])
        self.assertEqual(evidence['writes_project_files'], [])
        self.assertFalse(evidence['writes_models_json'])
        self.assertEqual(evidence['task']['parent_session_id'], 'parent-session')
        child = evidence['child_identity']
        self.assertEqual(child['child_ids'], ['child-session'])
        self.assertTrue(child['distinct_from_parent_session'])
        self.assertTrue(child['child_model_matches_parent'])
        self.assertTrue(child['paired_lifecycle'])
        self.assertEqual(evidence['ledger']['lineage']['children'][0]['output_preview'], 'ok')
        self.assertEqual(evidence['child_output']['from_executor_tool_result'][0]['text'], 'ok')
        self.assertEqual(evidence['gateway']['per_child'], 'unavailable')
        self.assertEqual(evidence['gateway']['aggregate']['requests'], 3)
        self.assertEqual(evidence['gateway']['child_lifecycle_window_requests'], 1)
        self.assertTrue(evidence_path.is_file())
        # The probe must not have written the user's model registry.
        self.assertFalse(registry.REGISTRY.exists())
        self.assertIn('models.json', evidence['policy_note'])

    def test_probe_is_refused_before_any_dispatch_when_preconditions_fail(self):
        model = paid_model(api_base='')
        with mock.patch.object(runtime.registry, 'get_model', return_value=model), \
             mock.patch.object(runtime.workflow_settings, 'snapshot', return_value=self.settings), \
             mock.patch.object(dsh_worker, 'run_task') as dispatch:
            with self.assertRaises(runtime.ProbeDenied):
                runtime.run_probe('cloud', root=self.root, runs=self.runs)
        dispatch.assert_not_called()

    def test_probe_writes_no_supported_evidence_into_the_model_registry(self):
        model = paid_model()
        with mock.patch.object(runtime.registry, 'get_model', return_value=model), \
             mock.patch.object(runtime.workflow_settings, 'snapshot', return_value=self.settings), \
             mock.patch.object(runtime.model_router, 'is_paid', return_value=True), \
             mock.patch.object(runtime.model_router, 'rates_for', return_value={'output': '4'}), \
             mock.patch.object(dsh_worker, 'run_task', side_effect=self.fake_executor(model)):
            runtime.run_probe('cloud', root=self.root, runs=self.runs)
        self.assertFalse((self.root / 'models.json').exists())
        self.assertEqual(caps.evaluate(model)['decision'], caps.VERDICT_DENY)


class PromptConstraintTests(unittest.TestCase):
    """The "do not create a child" clause must follow the realtime decision."""

    def test_denied_decision_keeps_the_original_constraint(self):
        self.assertEqual(dsh_worker.context_manager.execution_constraints(False),
                         dsh_worker.context_manager.EXECUTION_CONSTRAINTS)
        self.assertIn('不要切换模型或创建子代理', dsh_worker.context_manager.EXECUTION_CONSTRAINTS)

    def test_allowed_decision_never_contradicts_the_mounted_child(self):
        text = dsh_worker.context_manager.execution_constraints(True)
        self.assertNotIn('不要切换模型或创建子代理', text)
        self.assertIn('确有收益', text)
        self.assertIn('不要安装软件', text)

    def test_build_agent_prompt_uses_the_realtime_decision(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            (run / 'prompt.txt').write_text('任务正文', encoding='utf-8')
            state = {'kind': 'chat', 'files': []}
            allowed, _ = dsh_worker.build_agent_prompt(run, state, delegate=True)
            denied, _ = dsh_worker.build_agent_prompt(run, state, delegate=False)
        self.assertIn(dsh_worker.context_manager.EXECUTION_CONSTRAINTS_DELEGATE, allowed)
        self.assertNotIn(dsh_worker.context_manager.EXECUTION_CONSTRAINTS, allowed)
        self.assertIn(dsh_worker.context_manager.EXECUTION_CONSTRAINTS, denied)
        self.assertNotIn(dsh_worker.context_manager.EXECUTION_CONSTRAINTS_DELEGATE, denied)


class TaskBenefitTests(unittest.TestCase):
    def test_a_simple_request_has_no_expected_delegation_value(self):
        benefit = runtime.task_benefit({'kind': 'chat', 'files': []}, '只回复 ok')
        self.assertFalse(benefit['eligible'])
        self.assertTrue(any('收益' in reason for reason in benefit['reasons']))

    def test_multi_file_or_explicit_request_is_eligible(self):
        self.assertTrue(runtime.task_benefit({'files': ['a', 'b', 'c', 'd']}, '普通请求')['eligible'])
        self.assertTrue(runtime.task_benefit({'files': []}, '请创建子代理并行处理')['eligible'])

    def test_an_explicit_user_forbid_always_wins(self):
        narrow = runtime.task_benefit({'files': ['a', 'b', 'c', 'd']},
                                      '不要创建子代理，只做本地修改')
        self.assertFalse(narrow['eligible'])
        self.assertTrue(narrow['forbidden'])
        legacy = runtime.task_benefit({'files': []}, '执行约束：不要切换模型或创建子代理。')
        self.assertFalse(legacy['eligible'])


class DecisionGateTests(unittest.TestCase):
    def make_run(self, kind='chat', files=None, prompt='只回复 ok'):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = Path(temp.name) / 'runs' / 't1'
        run.mkdir(parents=True)
        (run / 'prompt.txt').write_text(prompt, encoding='utf-8')
        registry.patch_task(run, id='t1', kind=kind, files=files or [], status='queued')
        return run

    def test_allowed_capability_without_task_value_is_denied(self):
        run = self.make_run()
        with mock.patch.object(executor_adapter, 'subagent_gate',
                               return_value={'decision': 'allow', 'reasons': [], 'mode': 'chat'}):
            decision = dsh_worker.subagent_decision(paid_model(), run)
        self.assertEqual(decision['decision'], 'deny')
        self.assertIn('收益', decision['blocked'])
        self.assertFalse(decision['benefit']['eligible'])

    def test_allowed_capability_with_real_value_mounts_and_passes_the_mode(self):
        run = self.make_run(files=['a.py', 'b.py', 'c.py', 'd.py'])
        with mock.patch.object(executor_adapter, 'subagent_gate',
                               return_value={'decision': 'allow', 'reasons': []}) as gate:
            decision = dsh_worker.subagent_decision(paid_model(), run)
        self.assertEqual(decision['decision'], 'allow')
        self.assertTrue(decision['benefit']['eligible'])
        self.assertEqual(gate.call_args.kwargs.get('mode'), 'chat')

    def test_edit_mode_reaches_the_gate_and_is_denied_without_edit_evidence(self):
        run = self.make_run(kind='edit', files=['a.py', 'b.py', 'c.py', 'd.py'])
        identity = caps.evidence_identity(executor='dsh', model='deepseek-flash', preset='standard',
                                          version='0.2.0-rc.2', endpoint='https://api.deepseek.com',
                                          preset_revision='rev-1')
        model = paid_model(subagent={'policy': 'allow', 'evidence': [
            {'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat(),
             'identity': identity, 'read_only_probe': True}]})
        decision = dsh_worker.subagent_decision(model, run)
        self.assertEqual(decision['decision'], 'deny')
        self.assertIn('edit', ' '.join(decision.get('reasons') or []) + str(decision.get('blocked') or ''))

    def test_a_chat_only_grant_never_opens_edit_mode(self):
        identity = caps.evidence_identity(executor='dsh', model='deepseek-flash', preset='standard',
                                          version='0.2.0-rc.2', endpoint='https://api.deepseek.com',
                                          preset_revision='rev-1')
        model = paid_model(subagent={'policy': 'allow', 'evidence': [
            {'state': 'supported', 'measured': True, 'tested_at': caps._now().isoformat(),
             'identity': identity, 'read_only_probe': True}]})
        self.assertEqual(caps.evaluate(model, identity=identity, mode='chat')['decision'], caps.VERDICT_ALLOW)
        self.assertEqual(caps.evaluate(model, identity=identity, mode='edit')['decision'], caps.VERDICT_DENY)

    def test_unknown_task_mode_is_denied_before_the_gate_runs(self):
        run = self.make_run(kind='deploy')
        with mock.patch.object(executor_adapter, 'subagent_gate') as gate:
            decision = dsh_worker.subagent_decision(paid_model(), run)
        self.assertEqual(decision['decision'], 'deny')
        self.assertIn('未知任务模式', decision['blocked'])
        gate.assert_not_called()


class PresetProofTests(unittest.TestCase):
    def test_a_child_session_cannot_substitute_for_the_parent_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            preset = {'id': 'standard', 'revision': 'r1'}
            registry.write_json(run / 'preset-proof.json',
                                {'preset': 'standard', 'revision': 'r1', 'session_id': 'child-session'})
            with self.assertRaises(ValueError):
                preset_runtime.verify(run, preset, 'parent-session', allowed_sessions=['child-session'])

    def test_parent_proof_is_required_and_matched(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            preset = {'id': 'standard', 'revision': 'r1'}
            registry.write_json(run / 'preset-proof.json',
                                {'preset': 'standard', 'revision': 'r1', 'session_id': 'parent-session'})
            self.assertEqual(preset_runtime.verify(run, preset, 'parent-session')['matched'], 'parent')
            registry.write_json(run / 'preset-proof.json', {'preset': 'standard', 'revision': 'r1'})
            with self.assertRaises(ValueError):
                preset_runtime.verify(run, preset, 'parent-session')

    def test_bridge_writes_the_parent_proof_only_once(self):
        source = (runtime.HERE / 'agent-runtime' / 'preset-bridge.cjs').read_text(encoding='utf-8')
        # The write is guarded by an existence check, so a later child created
        # edge cannot overwrite the first (parent) proof.
        self.assertIn('if(!fs.existsSync(file))fs.writeFileSync(file,JSON.stringify', source)
        self.assertEqual(source.count('fs.writeFileSync(file'), 1)


class EvidenceRoundTests(unittest.TestCase):
    def test_reconstructed_round_keeps_real_cost_unknown_and_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / '20261014-050807-1013ec'
            run.mkdir(parents=True)
            registry.write_json(run / 'task.json', {
                'id': run.name, 'status': 'completed', 'kind': 'chat', 'model_id': 'cloud',
                'dsh_session_id': 'parent-session', 'agent_preset': 'standard',
                'preset_revision': 'old-rev', 'changed_files': [],
                'cost_requests': {
                    'a': {'state': 'settled', 'cost_cny': '0.005', 'billable': True,
                          'at': '2026-10-14T05:08:11+00:00',
                          'usage': {'prompt_tokens': 10, 'completion_tokens': 1}},
                    'b': {'state': 'unknown', 'billable': True,
                          'at': '2026-10-14T05:08:12+00:00',
                          'usage': {'prompt_tokens': 5, 'completion_tokens': 1}}}})
            registry.write_json(run / 'preset-proof.json',
                                {'preset': 'standard', 'revision': 'old-rev', 'session_id': 'child-session'})
            (run / 'subagent-ledger.jsonl').write_text('\n'.join(json.dumps(row) for row in [
                {'type': 'start', 'run_id': 'r1', 'provider': 'spawn', 'child_id': 'child-session',
                 'local': True, 'child_model': 'deepseek-flash', 'at': '2026-10-14T05:08:12+00:00'},
                {'type': 'end', 'run_id': 'r1', 'provider': 'spawn', 'child_id': 'child-session',
                 'local': True, 'at': '2026-10-14T05:08:13+00:00', 'stop_reason': 'completed',
                 'output_chars': 0},
            ]) + '\n', encoding='utf-8')
            round0 = runtime.reconstruct_round(run, model=paid_model())
            self.assertEqual(round0['gateway']['aggregate']['settled_cny'], 0.005)
            self.assertEqual(round0['gateway']['aggregate']['unknown_calls'], 1)
            self.assertEqual(round0['preset_proof']['matched'], 'child-overwrite-observed')
            self.assertFalse(round0['reconstruction']['dispatched'])

            path = root / 'evidence.json'
            later = {'kind': 'subagent-probe-evidence',
                     'task': {'task_id': '20261014-051043-4607c0'},
                     'gateway': {'aggregate': {'settled_cny': 0.006, 'unknown_calls': 0, 'requests': 3}}}
            runtime.merge_evidence(path, [later])
            saved = runtime.merge_evidence(path, [round0])
            self.assertEqual([row['task']['task_id'] for row in saved['rounds']],
                             ['20261014-050807-1013ec', '20261014-051043-4607c0'])
            self.assertEqual(saved['round_totals']['settled_cny'], 0.011)
            self.assertEqual(saved['round_totals']['unknown_calls'], 1)
            self.assertFalse(saved['round_totals']['settled_complete'])
            self.assertIn('不能声称总账单已完全结清', saved['round_totals']['note'])
            # Re-merging the same real task refreshes instead of duplicating.
            self.assertEqual(runtime.merge_evidence(path, [round0])['round_count'], 2)


class EditProbeTests(unittest.TestCase):
    """The explicit edit probe stays closed to everything but its own path."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir(parents=True, exist_ok=True)
        first_tranche_fixture(self.root, self.runs)
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = self.root
        registry.REGISTRY = self.root / 'models.json'
        registry.RUNS = self.runs
        self.settings = {'paid_allowed': True, 'enforce_paid_budget': False, 'enabled': True}

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def edit_run(self, nonce='a' * 32):
        return runtime.new_edit_probe_run('cloud', runs=self.runs, nonce=nonce)

    def fake_edit_executor(self, model, write=True, fail=False):
        def run_task(run, state, selected):
            if fail:
                raise RuntimeError('模拟 DSH 无法启动')
            workspace = Path(state['project'])
            relpath = state['files'][0]
            if write:
                (workspace / relpath).write_text(state['probe_nonce'] + '\n', encoding='utf-8')
            registry.patch_task(run, dsh_session_id='parent-session', status='completed',
                                acpx_record_id='rec-edit', kind='edit',
                                sandbox_mode='danger-full-access', permission_mode='approve-all',
                                changed_files=[relpath],
                                tool_calls=[{'tool_call_id': 'call-1', 'title': 'subagent',
                                             'status': 'completed', 'kind': 'other'}],
                                subagent_tool_outputs=[{'tool_call_id': 'call-1', 'text': 'ok', 'chars': 2}],
                                cost_requests={
                                    'p1': {'state': 'settled', 'cost_cny': '0.01', 'billable': True,
                                           'at': '2026-10-14T00:00:00+00:00',
                                           'usage': {'prompt_tokens': 40, 'completion_tokens': 5}},
                                    'c1': {'state': 'settled', 'cost_cny': '0.02', 'billable': True,
                                           'at': '2026-10-14T00:00:02+00:00',
                                           'usage': {'prompt_tokens': 12, 'completion_tokens': 2}},
                                    'u1': {'state': 'unknown', 'billable': True,
                                           'at': '2026-10-14T00:00:03+00:00',
                                           'usage': {'prompt_tokens': 8, 'completion_tokens': 1}}})
            runtime.ledger_path(run).write_text('\n'.join(json.dumps(row) for row in [
                {'schema': 1, 'source': 'run-dsh.mjs', 'type': 'parent', 'parent_session': 'parent-session'},
                {'schema': 1, 'source': 'dsh-subagent-lifecycle', 'type': 'start', 'run_id': 'r1',
                 'provider': 'spawn', 'child_id': 'child-session', 'local': True,
                 'child_model': model['model'], 'at': '2026-10-14T00:00:01+00:00'},
                {'schema': 1, 'source': 'dsh-subagent-lifecycle', 'type': 'end', 'run_id': 'r1',
                 'provider': 'spawn', 'child_id': 'child-session', 'local': True,
                 'at': '2026-10-14T00:00:03+00:00', 'stop_reason': 'completed',
                 'output_chars': 2, 'output_preview': 'ok'},
            ]) + '\n', encoding='utf-8')
            return 0
        return run_task

    def patched(self, model, executor):
        return (
            mock.patch.object(runtime.registry, 'get_model', return_value=model),
            mock.patch.object(runtime.workflow_settings, 'snapshot', return_value=self.settings),
            mock.patch.object(runtime.model_router, 'is_paid', return_value=True),
            mock.patch.object(runtime.model_router, 'rates_for', return_value={'output': '4'}),
            mock.patch.object(dsh_worker, 'run_task', side_effect=executor),
        )

    def test_explicit_flag_is_required_and_a_bare_context_is_refused(self):
        model = paid_model()
        result = runtime.probe_preconditions(model, settings=self.settings, explicit=False,
                                             root=self.root, runs=self.runs,
                                             mode=runtime.EDIT_PROBE_KIND)
        self.assertFalse(result['allowed'])
        self.assertTrue(any('显式' in reason for reason in result['reasons']))
        with self.assertRaises(runtime.ProbeDenied):
            runtime.authorize_probe(model, explicit=False, mode=runtime.EDIT_PROBE_KIND,
                                    root=self.root, runs=self.runs)
        # An explicit call with neither a mode nor an accepted probe context must
        # not be able to mount anything.
        with self.assertRaises(runtime.ProbeDenied):
            runtime.authorize_probe(model, explicit=True, root=self.root, runs=self.runs)

    def test_local_and_home_models_can_never_run_the_edit_probe(self):
        for row in ({'id': 'home', 'provider': 'local', 'model': 'qwen',
                     'api_base': 'http://127.0.0.1/v1', 'enabled': True},
                    {'id': 'home', 'provider': 'custom', 'model': 'qwen',
                     'api_base': 'http://127.0.0.1/v1', 'enabled': True}):
            result = runtime.probe_preconditions(row, settings=self.settings, explicit=True,
                                                 root=self.root, runs=self.runs,
                                                 mode=runtime.EDIT_PROBE_KIND)
            self.assertFalse(result['allowed'])
            self.assertTrue(any('home/本地' in reason for reason in result['reasons']))
            with self.assertRaises(runtime.ProbeDenied):
                runtime.authorize_edit_probe(row, settings=self.settings, root=self.root, runs=self.runs)

    def test_edit_probe_refuses_without_paid_authorization(self):
        result = runtime.probe_preconditions(paid_model(), settings={'paid_allowed': False, 'enabled': True},
                                             explicit=True, root=self.root, runs=self.runs,
                                             mode=runtime.EDIT_PROBE_KIND)
        self.assertFalse(result['allowed'])
        self.assertTrue(any('付费授权' in reason for reason in result['reasons']))

    def test_edit_probe_refuses_a_target_outside_the_run_workspace(self):
        model = paid_model()
        created = self.edit_run()
        for escaped in (created['workspace'] / '..' / 'escape.txt',
                        self.root / 'outside' / 'escape.txt'):
            with mock.patch.object(runtime.model_router, 'is_paid', return_value=True), \
                 mock.patch.object(runtime.model_router, 'rates_for', return_value={'output': '4'}):
                with self.assertRaises(runtime.ProbeDenied) as caught:
                    runtime.authorize_edit_probe(model, settings=self.settings, root=self.root,
                                                 runs=self.runs, run=created['run'], target=escaped)
            self.assertIn('越界', str(caught.exception))

    def test_edit_probe_refuses_a_run_outside_the_verified_runs_tree(self):
        model = paid_model()
        outside = self.root / 'outside' / 'task-x'
        (outside / 'workspace').mkdir(parents=True)
        registry.patch_task(outside, id='task-x', kind='edit', subagent_edit_probe=True)
        target = outside / 'workspace' / 'x.txt'
        with mock.patch.object(runtime.model_router, 'is_paid', return_value=True), \
             mock.patch.object(runtime.model_router, 'rates_for', return_value={'output': '4'}):
            with self.assertRaises(runtime.ProbeDenied) as caught:
                runtime.authorize_edit_probe(model, settings=self.settings, root=self.root,
                                             runs=self.runs, run=outside, target=target)
        self.assertIn('runs', str(caught.exception))

    def test_edit_probe_refuses_a_run_that_does_not_declare_edit(self):
        model = paid_model()
        created = self.edit_run()
        registry.patch_task(created['run'], kind='chat')
        with mock.patch.object(runtime.model_router, 'is_paid', return_value=True), \
             mock.patch.object(runtime.model_router, 'rates_for', return_value={'output': '4'}):
            with self.assertRaises(runtime.ProbeDenied) as caught:
                runtime.authorize_edit_probe(model, settings=self.settings, root=self.root,
                                             runs=self.runs, run=created['run'], target=created['target'])
        self.assertIn('edit', str(caught.exception))

    def test_a_chat_probe_context_cannot_be_pointed_at_an_edit_run(self):
        model = paid_model()
        created = self.edit_run()
        runtime._set_active_probe({'mode': caps.MODE_CHAT, 'run': str(created['run']),
                                   'target': None})
        try:
            with mock.patch.object(runtime.model_router, 'is_paid', return_value=True), \
                 mock.patch.object(runtime.model_router, 'rates_for', return_value={'output': '4'}):
                with self.assertRaises(runtime.ProbeDenied) as caught:
                    runtime.authorize_probe(model, explicit=True, root=self.root, runs=self.runs)
            self.assertIn('不一致', str(caught.exception))
        finally:
            runtime._clear_active_probe()

    def test_the_lock_blocks_a_concurrent_or_repeated_dispatch(self):
        model = paid_model()
        lock = runtime.acquire_edit_probe_lock(self.runs)
        dispatch = mock.MagicMock()
        try:
            with mock.patch.object(runtime.registry, 'get_model', return_value=model), \
                 mock.patch.object(dsh_worker, 'run_task', dispatch):
                with self.assertRaises(runtime.ProbeDenied) as caught:
                    runtime.run_edit_probe('cloud', evidence_path=self.root / 'e.json', runs=self.runs)
            self.assertIn('并发', str(caught.exception))
            dispatch.assert_not_called()
        finally:
            runtime.release_edit_probe_lock(lock)

    def test_an_expired_leftover_lock_is_reclaimed_once(self):
        from datetime import datetime, timedelta, timezone
        lock_path = runtime.edit_probe_lock_path(self.runs)
        lock_path.write_text(json.dumps({'pid': 1, 'at': (datetime.now(timezone.utc)
                                                          - timedelta(seconds=7200)).isoformat()}),
                             encoding='utf-8')
        reclaimed = runtime.acquire_edit_probe_lock(self.runs)
        self.assertTrue(reclaimed['reclaimed_stale'])
        runtime.release_edit_probe_lock(reclaimed)
        self.assertFalse(lock_path.exists())

    def test_the_inflight_context_is_readable_without_a_module_global(self):
        # The CLI runs this file as __main__ while dsh_worker imports it as
        # subagent_runtime; the persisted per-process context is what the second
        # module instance reads at the enforcement point.
        context = {'mode': 'edit', 'run': 'task-x', 'target': 'y.txt', 'kind': 'edit-probe'}
        runtime._clear_active_probe()
        runtime._persist_probe_context(context, self.runs)
        try:
            self.assertEqual(runtime.load_probe_context(self.runs), context)
        finally:
            runtime._clear_persisted_probe_context(self.runs)
        self.assertFalse(runtime.probe_context_path(self.runs).exists())

    def test_authorize_probe_reads_the_persisted_inflight_context(self):
        model = paid_model()
        created = self.edit_run()
        runtime._clear_active_probe()
        runtime._persist_probe_context({'mode': 'edit', 'run': str(created['run']),
                                        'target': str(created['target']), 'kind': 'edit-probe'},
                                       self.runs)
        try:
            with mock.patch.object(runtime.model_router, 'is_paid', return_value=True), \
                 mock.patch.object(runtime.model_router, 'rates_for', return_value={'output': '4'}):
                plan = runtime.authorize_probe(model, explicit=True, settings=self.settings,
                                               root=self.root, runs=self.runs)
        finally:
            runtime._clear_persisted_probe_context(self.runs)
        self.assertEqual(plan['mode'], 'edit')
        self.assertTrue(plan['preconditions']['checks']['scope']['run_declares_edit_probe'])
        self.assertTrue(plan['preconditions']['checks']['scope']['target_inside_run_workspace'])

    def test_reconstructed_failed_edit_round_keeps_the_deny_and_real_cost(self):
        run = self.runs / '20261014-063830-534891'
        (run / 'workspace').mkdir(parents=True)
        registry.patch_task(run, id=run.name, kind='edit', status='completed', exit_code=1,
                            sandbox_mode='danger-full-access', project=str(run / 'workspace'),
                            probe_target='edit-probe-a25c9c19.txt', probe_nonce='a' * 32, model_id='cloud',
                            subagent_capability={
                                'blocked': '受控探测必须以 --probe/--edit-probe 入口在显式探测上下文中调用'},
                            cost_requests={'p1': {'state': 'settled', 'cost_cny': '0.00745952',
                                                  'billable': True, 'at': '2026-10-14T06:38:40+00:00',
                                                  'usage': {'prompt_tokens': 10280, 'completion_tokens': 612}}})
        (run / 'subagent-ledger.jsonl').write_text('', encoding='utf-8')
        rebuilt = runtime.reconstruct_edit_round(run, model=paid_model())
        self.assertEqual(rebuilt['kind'], runtime.EDIT_PROBE_EVIDENCE_KIND)
        self.assertFalse(rebuilt['measured'])
        self.assertFalse(rebuilt['read_only_probe'])
        self.assertEqual(rebuilt['mode'], 'edit')
        self.assertIn('上下文', rebuilt['probe']['deny'])
        self.assertEqual(rebuilt['gateway']['aggregate']['settled_cny'], 0.00745952)
        self.assertFalse(rebuilt['write_check']['exists'])
        self.assertFalse(rebuilt['reconstruction']['dispatched'])

    def test_edit_probe_records_file_scope_random_value_and_sha(self):
        model = paid_model()
        evidence_path = self.root / 'edit-evidence.json'
        patches = self.patched(model, self.fake_edit_executor(model))
        with patches[0], patches[1], patches[2], patches[3], patches[4] as dispatch:
            evidence = runtime.run_edit_probe('cloud', evidence_path=evidence_path, root=self.root, runs=self.runs)
        self.assertEqual(dispatch.call_count, 1)
        self.assertTrue(evidence['measured'])
        self.assertEqual(evidence['mode'], 'edit')
        self.assertEqual(evidence['sandbox_mode'], 'danger-full-access')
        self.assertFalse(evidence['read_only_probe'])
        self.assertEqual(evidence['writes_project_files'], [])
        self.assertFalse(evidence['writes_models_json'])
        check = evidence['write_check']
        self.assertTrue(check['exists'])
        self.assertTrue(check['content_matches_nonce'])
        self.assertTrue(check['inside_run_workspace'])
        on_disk = Path(check['target_on_disk'])
        self.assertEqual(check['sha256'], hashlib.sha256(on_disk.read_bytes()).hexdigest())
        self.assertEqual(on_disk.read_text(encoding='utf-8').strip(), check['expected_nonce'])
        self.assertEqual(evidence['task']['kind'], 'edit')
        self.assertEqual(evidence['task']['file_scope'], [check['relative_target']])
        child = evidence['child_identity']
        self.assertEqual(child['child_ids'], ['child-session'])
        self.assertTrue(child['distinct_from_parent_session'])
        self.assertTrue(child['child_model_matches_parent'])
        self.assertTrue(child['paired_lifecycle'])
        self.assertEqual(evidence['preset_proof']['matched'], 'missing')
        self.assertEqual(evidence['gateway']['per_child'], 'unavailable')
        self.assertEqual(evidence['gateway']['aggregate']['requests'], 3)
        self.assertEqual(evidence['gateway']['aggregate']['unknown_calls'], 1)
        self.assertEqual(check['attribution']['subagent_tool_calls'], 1)
        self.assertFalse(check['attribution']['parent_recorded_touch_of_target'])
        self.assertTrue(evidence_path.is_file())

    def test_edit_probe_writes_no_supported_claim_and_touches_no_user_project(self):
        model = paid_model(subagent={'policy': 'allow', 'evidence': []})
        executor = self.fake_edit_executor(model)
        patches = self.patched(model, executor)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            evidence = runtime.run_edit_probe('cloud', root=self.root, runs=self.runs)
        self.assertFalse(registry.REGISTRY.exists())
        self.assertEqual(evidence['writes_project_files'], [])
        self.assertIn('models.json', evidence['policy_note'])
        # A chat-only/empty grant still cannot open edit through the normal gate.
        self.assertEqual(caps.evaluate(model, mode='edit')['decision'], caps.VERDICT_DENY)

    def test_edit_probe_failure_is_recorded_once_without_a_retry(self):
        model = paid_model()
        executor = self.fake_edit_executor(model, fail=True)
        patches = self.patched(model, executor)
        with patches[0], patches[1], patches[2], patches[3], patches[4] as dispatch:
            evidence = runtime.run_edit_probe('cloud', root=self.root, runs=self.runs)
        self.assertEqual(dispatch.call_count, 1)
        self.assertFalse(evidence['measured'])
        self.assertTrue(evidence['write_check']['exists'] is False)
        self.assertEqual(evidence['probe']['exit_code'], 1)

    def test_edit_evidence_is_a_separate_file_from_the_chat_evidence(self):
        self.assertNotEqual(Path(runtime.EDIT_PROBE_EVIDENCE_PATH), Path(runtime.EVIDENCE_PATH))
        model = paid_model()
        executor = self.fake_edit_executor(model)
        path = self.root / 'edit-evidence.json'
        patches = self.patched(model, executor)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            runtime.run_edit_probe('cloud', evidence_path=path, root=self.root, runs=self.runs)
            runtime.run_edit_probe('cloud', evidence_path=path, root=self.root, runs=self.runs)
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['kind'], runtime.EDIT_PROBE_EVIDENCE_KIND)
        self.assertEqual(saved['round_count'], 2)
        self.assertEqual(saved['round_totals']['rounds'], 2)
        self.assertEqual(saved['round_totals']['unknown_calls'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
