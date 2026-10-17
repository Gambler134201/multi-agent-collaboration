"""Routing negative tests for capacity semantics, risk calibration and no-leak.

These pin the E1 contract: approximate token guesses only warn, explicit
declarations/declared minimums and the capability-complexity gate still block,
paid authorization and manual selection keep working, rework risk is
family-deduplicated from independent review evidence, and the public JSON keeps
prediction, actual ledger and unknown price apart without leaking secrets.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_registry as registry
import workflow_settings as settings
import model_router as router


SYSTEM_ERRORS = (
    '已达到本任务模型请求次数上限；需要主脑验收或重新拆分',
    '本次请求的保守费用会超出单任务或今日预算，已阻止发送',
    'Internal error: turn failed: Connection error.',
    '输入与输出的保守估计超过上下文窗口，请缩小上下文或降低输出上限',
)


def failed_record(task_id, model_id, category, error=None, parent=None):
    record = {'id': task_id, 'status': 'failed', 'model_id': model_id,
              'route': {'requirements': {'category': category}}}
    if error:
        record['error'] = error
    if parent:
        record['parent_task_id'] = parent
    return record


class RouterBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.local = {'id': 'local', 'name': 'local', 'provider': 'local', 'model': 'qwen', 'enabled': True,
                      'max_input_tokens': 262144, 'max_output_tokens': 8192,
                      'preferred_tasks': ['单文件修改', '信息整理'], 'max_task_complexity': 'simple',
                      'cost_preference': 'economy'}
        self.paid = {'id': 'paid', 'name': 'paid', 'provider': 'deepseek', 'api_base': 'https://api.deepseek.com',
                     'model': 'deepseek-flash', 'enabled': True, 'api_key_set': True, 'max_input_tokens': 1000000,
                     'max_output_tokens': 64000,
                     'preferred_tasks': ['多文件修改', '复杂逻辑分析', '疑难问题排查'],
                     'max_task_complexity': 'complex', 'cost_preference': 'balanced',
                     'billing': {'balance': {'status': 'ready', 'is_available': True,
                                             'balance_infos': [{'currency': 'CNY', 'total_balance': '10'}]}}}
        self.on = {**settings.DEFAULTS, 'enabled': True, 'paid_allowed': True}
        self.body = {'kind': 'edit', 'engine': 'dsh', 'route_mode': 'auto', 'prompt': 'task', 'files': ['a.py'],
                     'routing': {'category': 'code_small', 'complexity': 'simple', 'input_tokens': 8192,
                                 'output_tokens': 4096, 'expected_calls': 3}}

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def choose(self, body=None, **kwargs):
        return router.choose(body or self.body, kwargs.pop('models', [self.local, self.paid]),
                             kwargs.pop('settings', self.on), **kwargs)

    def manual(self, model_id, **routing):
        merged = {'category': 'code_small', 'complexity': 'simple', 'expected_calls': 1}
        merged.update(routing)
        return {**self.body, 'route_mode': 'manual', 'model_id': model_id, 'routing': merged}


class CapacitySemanticsTests(RouterBase):
    def test_approximate_estimates_warn_instead_of_hard_blocking(self):
        body = self.manual('local', estimated_input_tokens=300000, estimated_output_tokens=9000)
        route = self.choose(body)
        self.assertEqual(route['model_id'], 'local')
        self.assertTrue(route['warnings'])
        self.assertTrue(any('近似' in warning for warning in route['warnings']))
        self.assertEqual(route['requirements']['capacity']['estimates']['output_tokens'], 9000)
        self.assertEqual(route['requirements']['capacity']['estimate_semantics'], 'approximate')
        self.assertIn('近似', route['reason'])

    def test_explicit_declarations_and_declared_minimums_still_gate(self):
        for routing in ({'input_tokens': 300000}, {'output_tokens': 9000}):
            self.assertEqual(self.choose(self.manual('local', **routing))['model_id'], 'local')
        for routing in ({'min_input_tokens': 300000}, {'min_output_tokens': 9000}):
            with self.assertRaises(ValueError):
                self.choose(self.manual('local', **routing))

    def test_capability_complexity_gate_is_unchanged(self):
        body = {**self.body, 'routing': {**self.body['routing'], 'category': 'code_multi', 'complexity': 'complex'}}
        route = self.choose(body)
        self.assertEqual(route['model_id'], 'paid')
        self.assertIn('复杂度', route['excluded'][0]['reason'])

    def test_single_request_target_is_clamped_to_model_ceiling(self):
        route = self.choose({**self.manual('paid'), 'request_output_tokens': 32768})
        self.assertEqual(route['single_request_output']['task_target'], 32768)
        self.assertEqual(route['single_request_output']['model_ceiling'], 64000)
        self.assertEqual(route['single_request_output']['effective_target'], 32768)
        self.assertEqual(self.paid['max_output_tokens'], 64000)
        # The model ceiling is a capability bound; the task target only clamps it.
        body = {**self.manual('local'), 'request_output_tokens': 9000}
        clamped = self.choose(body)
        self.assertEqual(clamped['single_request_output']['effective_target'], 8192)

    def test_requirements_stay_backward_compatible_with_approximate_defaults(self):
        req = router.requirements(self.body)
        self.assertEqual((req['input_tokens'], req['output_tokens'], req['expected_calls']), (8192, 4096, 3))
        self.assertEqual(req['capacity']['declared'], {'input_tokens': 8192, 'output_tokens': 4096})
        default = router.requirements({'kind': 'edit', 'routing': {}})
        self.assertIsNone(default['capacity']['declared']['input_tokens'])
        self.assertEqual(default['capacity']['estimates']['input_tokens'], 4096)
        self.assertEqual(default['output_tokens'], 4096)

    def test_declared_minimum_round_trips_through_stored_requirements(self):
        req = router.requirements({'kind': 'edit', 'routing': {'min_output_tokens': 6000}})
        self.assertEqual(req['min_output_tokens'], 6000)
        self.assertEqual(req['capacity']['minimum']['output_tokens'], 6000)
        replayed = router.requirements({'kind': 'edit', 'routing': req})
        self.assertEqual(replayed['min_output_tokens'], 6000)
        absent = router.requirements({'kind': 'edit', 'routing': {}})
        self.assertNotIn('min_output_tokens', absent)
        self.assertNotIn('min_input_tokens', absent)

    def test_legacy_unclassified_history_still_lowers_expected_cost_backward_compatibly(self):
        body = self.manual('paid', expected_calls=3)
        base = self.choose(body)['estimated_cost_cny']
        history = [{'model_id': 'paid', 'status': 'failed',
                    'route': {'requirements': {'category': 'code_small'}}}]
        self.assertEqual(self.choose(body, history=history)['estimated_cost_cny'], base)

    def test_estimate_override_prices_and_round_trips_without_becoming_strict(self):
        body=self.manual('paid', input_tokens=4096, estimated_input_tokens=200000)
        route=self.choose(body)
        replay=self.choose({**body,'routing':route['requirements']})
        self.assertEqual(replay['requirements']['capacity']['estimates']['input_tokens'],200000)
        self.assertEqual(route['estimated_cost_cny'],replay['estimated_cost_cny'])
        base=self.choose(self.manual('paid',input_tokens=4096))
        self.assertGreater(route['estimated_cost_cny'],base['estimated_cost_cny'])


class AuthorizationTests(RouterBase):
    def test_manual_selection_declared_candidates_and_failed_ids_still_work(self):
        self.assertEqual(self.choose(self.manual('local'))['model_id'], 'local')
        body = {**self.body, 'routing': {**self.body['routing'], 'category': 'other',
                                         'eligible_model_ids': ['local'], 'failed_model_ids': ['local']}}
        with self.assertRaisesRegex(ValueError, '失败'):
            self.choose(body)

    def test_paid_permission_is_not_bypassed_and_never_silently_falls_back(self):
        complex_body = {**self.body, 'routing': {**self.body['routing'], 'category': 'code_multi',
                                                 'complexity': 'complex'}}
        with self.assertRaisesRegex(ValueError, '付费执行'):
            self.choose(complex_body, settings={**self.on, 'paid_allowed': False})
        route = self.choose(complex_body)
        self.assertEqual(route['model_id'], 'paid')

    def test_unknown_paid_price_is_an_error_not_zero(self):
        unknown = {**self.paid, 'provider': 'glm', 'api_base': 'https://example.invalid/v1', 'pricing_cny': None}
        with self.assertRaisesRegex(ValueError, '单价'):
            self.choose(self.manual('paid'), models=[unknown])
        self.assertIsNone(router.rates_for(unknown))
        self.assertFalse(router.pricing_info(unknown)['known'])
        self.assertTrue(router.pricing_info(self.local)['known'])
        self.assertEqual(router.pricing_info(self.local)['source'], 'self_hosted')


class ReworkRiskTests(RouterBase):
    def test_quality_family_is_deduplicated_and_system_failures_excluded(self):
        history = [
            {'id': 'Q1', 'status': 'completed', 'model_id': 'paid',
             'route': {'requirements': {'category': 'code_multi'}},
             'review': {'state': 'returned', 'executor_status': 'completed', 'returned_criteria': ['c1']}},
            failed_record('Q2', 'paid', 'code_multi', error='DSH 提前结束：max_tokens', parent='Q1'),
        ]
        history += [failed_record('S%d' % index, 'paid', 'code_multi', error=text)
                    for index, text in enumerate(SYSTEM_ERRORS)]
        risk = router.rework_risk(history, 'paid', 'code_multi')
        self.assertEqual(risk['verified_samples'], 1)
        self.assertEqual(risk['quality_rework_families'], 1)
        self.assertEqual(risk['families'], 5)
        self.assertEqual(risk['excluded']['system_families'], 4)
        self.assertEqual(risk['confidence'], 'low')
        self.assertTrue(any('校准成熟' in note for note in risk['notes']))
        self.assertEqual(risk['basis'], 'verified_independent_review')
        self.assertEqual(risk['retry_factor'], round(1 + 2 / 3, 4))

    def test_system_failures_do_not_become_a_price_multiplier(self):
        all_system = [failed_record('S%d' % index, 'paid', 'code_small', error=text)
                      for index, text in enumerate(SYSTEM_ERRORS * 3)]
        risk = router.rework_risk(all_system, 'paid', 'code_small')
        self.assertEqual(risk['basis'], 'no_samples')
        self.assertEqual(risk['retry_factor'], 1.0)
        self.assertEqual(risk['excluded']['system_families'], 12)

        mixed = [failed_record('S%d' % index, 'paid', 'code_small', error=text)
                 for index, text in enumerate(SYSTEM_ERRORS)]
        mixed += [failed_record('U%d' % index, 'paid', 'code_small') for index in range(5)]
        risk = router.rework_risk(mixed, 'paid', 'code_small')
        self.assertEqual(risk['verified_samples'], 0)
        self.assertEqual(risk['basis'], 'unverified_legacy_default')
        self.assertEqual(risk['excluded']['system_families'], 4)
        self.assertLess(risk['retry_factor'], 5.5)  # legacy counted all 9 failures

    def test_empty_history_is_an_honest_default(self):
        risk = router.rework_risk([], 'paid', 'code_small')
        self.assertEqual(risk['retry_factor'], 1.0)
        self.assertEqual(risk['basis'], 'no_samples')
        self.assertEqual(risk['confidence'], 'none')
        self.assertIsNone(risk['quality_rework_rate'])

    def test_cost_block_separates_prediction_observed_and_unknown(self):
        history = [{'id': 'h1', 'model_id': 'paid', 'status': 'completed',
                    'route': {'requirements': {'category': 'code_small'}},
                    'cost_settled_cny': 0.0123, 'cost_unknown_calls': 2, 'model_calls': 4}]
        route = self.choose(self.manual('paid'), history=history)
        cost = route['cost']
        self.assertEqual(cost['prediction']['kind'], 'prediction')
        self.assertAlmostEqual(cost['observed']['settled_cost_cny'], 0.0123)
        self.assertEqual(cost['observed']['unknown_cost_calls'], 2)
        self.assertEqual(cost['observed']['observed_calls'], 4)
        self.assertEqual(cost['observed']['settled_samples'], 1)
        self.assertTrue(cost['pricing']['known'])
        self.assertEqual(cost['pricing']['source'], 'official_schedule')
        prediction = cost['prediction']
        self.assertLessEqual(prediction['interval_low_cny'], prediction['estimated_cost_cny'])
        self.assertGreaterEqual(prediction['interval_high_cny'], prediction['estimated_cost_cny'])

    def test_public_route_json_does_not_leak_secrets(self):
        paid = {**self.paid, 'api_key': 'TOP-SECRET-ABC', 'billing_token': 'SECRET-TOKEN-XYZ'}
        route = self.choose(self.manual('paid'), models=[self.local, paid])
        blob = json.dumps(route, ensure_ascii=False)
        self.assertNotIn('TOP-SECRET-ABC', blob)
        self.assertNotIn('SECRET-TOKEN-XYZ', blob)
        self.assertNotIn('api_key', blob)
        self.assertNotIn('billing_token', blob)
        self.assertIn('estimated_cost_cny', route)
        self.assertIn('failure_risk', route)

    def test_risk_dictionary_is_json_serializable_and_read_only(self):
        history = [failed_record('S1', 'paid', 'code_small', error='429 Too Many Requests')]
        before = json.dumps(history, ensure_ascii=False, sort_keys=True)
        route = self.choose(self.manual('paid'), history=history)
        json.dumps(route, ensure_ascii=False)
        self.assertEqual(json.dumps(history, ensure_ascii=False, sort_keys=True), before)


class ExecutionScopeRoutingTests(RouterBase):
    """The global execution-model scope is an authoritative router gate."""

    def scoped(self, scope, body=None, **kwargs):
        return router.choose(body or self.body, kwargs.pop('models', [self.local, self.paid]),
                             {**self.on, 'model_scope': scope}, **kwargs)

    def test_default_all_scope_keeps_the_existing_route(self):
        route = self.scoped('all')
        self.assertEqual(route['model_id'], 'local')
        self.assertEqual(route['model_scope']['value'], 'all')
        self.assertEqual(route['model_scope']['candidate'], 'local')

    def test_preview_payload_is_unchanged_by_an_explicit_all_scope(self):
        # Compare identical observations; live pricing includes the query time.
        moment = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
        plain = self.choose(moment=moment)
        scoped = self.scoped('all', moment=moment)
        # The snapshot records wall-clock query time independently of the
        # pricing period passed above; comparing it makes this test race a clock.
        for route in (plain, scoped):
            datetime.fromisoformat(route.pop('pricing_snapshot'))
        self.assertEqual(plain, scoped)

    def test_a_manual_paid_model_is_refused_under_local_only(self):
        with self.assertRaisesRegex(ValueError, '执行模型范围'):
            self.scoped('local_only', self.manual('paid'))

    def test_a_manual_local_model_is_refused_under_paid_only(self):
        with self.assertRaisesRegex(ValueError, '执行模型范围'):
            self.scoped('paid_only', self.manual('local'))

    def test_excluded_reason_names_the_matching_class(self):
        with self.assertRaises(ValueError) as error:
            self.scoped('local_only', self.manual('paid'))
        self.assertIn('付费模型', str(error.exception))
        with self.assertRaises(ValueError) as error:
            self.scoped('paid_only', self.manual('local'))
        self.assertIn('本地/自建模型', str(error.exception))

    def test_scope_information_is_recorded_on_the_route(self):
        route = self.scoped('paid_only', self.manual('paid'))
        self.assertEqual(route['model_scope']['label'], '仅付费模型')
        self.assertTrue(route['model_scope']['candidate_paid'])
        self.assertFalse(route['model_scope']['is_default'])
        self.assertIn('仅付费模型', route['reason'])

    def test_invalid_scope_setting_is_refused_for_manual_too(self):
        for value in ('paid', 3, True, ''):
            with self.assertRaisesRegex(ValueError, '执行模型范围'):
                router.choose(self.manual('local'), [self.local, self.paid], {**self.on, 'model_scope': value})


if __name__ == '__main__':
    unittest.main()
