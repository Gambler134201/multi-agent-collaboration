import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_registry as registry
import workflow_settings as settings
import model_router as router
import budget_relay as relay
import panel_server as panel
import install_workflow_rules as rules


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.local = {'id': 'local', 'name': 'local', 'provider': 'local', 'model': 'qwen', 'enabled': True,
                      'max_input_tokens': 262144, 'max_output_tokens': 8192, 'preferred_tasks': ['单文件修改', '信息整理'],
                      'max_task_complexity': 'simple', 'cost_preference': 'economy'}
        self.paid = {'id': 'paid', 'name': 'paid', 'provider': 'deepseek', 'api_base': 'https://api.deepseek.com',
                     'model': 'deepseek-flash', 'enabled': True, 'api_key_set': True, 'max_input_tokens': 1000000,
                     'max_output_tokens': 64000, 'preferred_tasks': ['多文件修改', '复杂逻辑分析', '疑难问题排查'],
                     'max_task_complexity': 'complex', 'cost_preference': 'balanced',
                     'billing': {'balance': {'status': 'ready', 'is_available': True, 'balance_infos': [{'currency': 'CNY', 'total_balance': '10'}]}}}
        self.on = {**settings.DEFAULTS, 'enabled': True, 'paid_allowed': True}
        self.body = {'kind': 'edit', 'engine': 'dsh', 'route_mode': 'auto', 'prompt': 'task', 'files': ['a.py'],
                     'routing': {'category': 'code_small', 'complexity': 'simple', 'input_tokens': 8192, 'output_tokens': 4096, 'expected_calls': 3}}

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def choose(self, body=None, **kwargs):
        return router.choose(body or self.body, [self.local, self.paid], kwargs.pop('settings', self.on), **kwargs)

    def test_missing_and_corrupt_settings_fail_closed_without_creating_service(self):
        self.assertFalse(settings.snapshot()['enabled'])
        self.assertFalse((registry.HERE / 'workflow.json').exists())
        (registry.HERE / 'workflow.json').write_text('{bad', encoding='utf-8')
        state = settings.snapshot()
        self.assertFalse(state['enabled'])
        self.assertFalse(state['paid_allowed'])
        self.assertFalse(state['readable'])

    def test_persistent_switch_and_validation(self):
        settings.save_settings({'enabled': True})
        self.assertTrue(settings.snapshot()['enabled'])
        self.assertFalse(settings.snapshot()['paid_allowed'])
        for body in [{'enabled': 1}, {'task_budget_cny': True}, {'daily_budget_cny': float('nan')}, {'max_model_calls': 0}, {'unknown': 1}]:
            with self.assertRaises(ValueError):
                settings.save_settings(body)
        settings.save_settings({'enabled': False})
        self.assertFalse(settings.snapshot()['enabled'])

    def test_local_first_and_off_rejects_automatic_but_manual_is_allowed(self):
        self.assertEqual(self.choose()['model_id'], 'local')
        with self.assertRaisesRegex(ValueError, '总开关'):
            self.choose(settings=settings.DEFAULTS)
        manual = {**self.body, 'route_mode': 'manual', 'model_id': 'local'}
        self.assertEqual(self.choose(manual, settings=settings.DEFAULTS)['model_id'], 'local')

    def test_complex_task_routes_paid_with_explained_exclusion(self):
        body = {**self.body, 'routing': {**self.body['routing'], 'category': 'code_multi', 'complexity': 'complex'}}
        row = self.choose(body)
        self.assertEqual(row['model_id'], 'paid')
        self.assertIn('复杂度', row['excluded'][0]['reason'])
        self.assertGreater(row['estimated_cost_cny'], 0)

    def test_paid_disabled_or_budget_or_stale_balance_never_falls_back(self):
        body = {**self.body, 'routing': {**self.body['routing'], 'complexity': 'complex', 'category': 'debug'}}
        for values in [{'paid_allowed': False}, {'task_budget_cny': 0}, {'daily_budget_cny': 0}]:
            with self.assertRaises(ValueError):
                self.choose(body, settings={**self.on, **values})
        self.paid['billing']['balance']['stale'] = True
        with self.assertRaisesRegex(ValueError, '余额'):
            self.choose(body)

    def test_context_and_output_limits_apply_even_when_manual(self):
        for key, value in [('min_input_tokens', 300000), ('min_output_tokens', 9000)]:
            body = {**self.body, 'route_mode': 'manual', 'model_id': 'local', 'routing': {**self.body['routing'], key: value}}
            with self.assertRaises(ValueError):
                self.choose(body)

    def test_notes_assessment_candidates_and_failed_model_exclusion(self):
        body = {**self.body, 'routing': {**self.body['routing'], 'category': 'other', 'eligible_model_ids': ['local'], 'assessment': '能力说明允许此任务'}}
        self.assertEqual(self.choose(body)['model_id'], 'local')
        body['routing']['failed_model_ids'] = ['local']
        with self.assertRaisesRegex(ValueError, '失败'):
            self.choose(body)

    def test_current_peak_valley_cache_and_retry_cost(self):
        peak = datetime(2026, 10, 13, 14, 0, tzinfo=relay.billing.CHINA)
        valley = datetime(2026, 10, 13, 18, 1, tzinfo=relay.billing.CHINA)
        body = {**self.body, 'route_mode': 'manual', 'model_id': 'paid'}
        a = self.choose(body, moment=peak)['estimated_cost_cny']
        b = self.choose(body, moment=valley)['estimated_cost_cny']
        self.assertAlmostEqual(a, b * 2)
        body['routing']['cache_fraction'] = 1
        self.assertLess(self.choose(body, moment=valley)['estimated_cost_cny'], b)
        history = [{'model_id': 'paid', 'status': 'failed', 'route': {'requirements': {'category': 'code_small'}}}]
        self.assertEqual(self.choose(body, moment=valley, history=history)['estimated_cost_cny'], self.choose(body, moment=valley)['estimated_cost_cny'])

    def test_unknown_provider_needs_full_prices_and_key(self):
        self.paid.update(provider='glm', api_base='https://example.invalid/v1', pricing_cny=None)
        body = {**self.body, 'route_mode': 'manual', 'model_id': 'paid'}
        with self.assertRaisesRegex(ValueError, '单价'):
            self.choose(body)
        self.paid['pricing_cny'] = {'cache_hit_input': 1, 'cache_miss_input': 2, 'output': 8}
        self.assertEqual(self.choose(body)['model_id'], 'paid')

    def test_bad_connection_and_request_count_require_reassessment(self):
        self.local['last_check'] = {'ok': False}
        with self.assertRaisesRegex(ValueError, '连接测试'):
            self.choose(settings={**self.on, 'paid_allowed': False})
        with self.assertRaisesRegex(ValueError, '请求次数'):
            self.choose({**self.body, 'routing': {**self.body['routing'], 'expected_calls': 7}})

    def test_rule_install_preserves_content_and_is_idempotent(self):
        home = registry.HERE / 'codex'
        home.mkdir()
        path = home / 'AGENTS.override.md'
        path.write_text('User rule stays.\n', encoding='utf-8')
        self.assertEqual(rules.install(home), path)
        first = path.read_text(encoding='utf-8')
        self.assertTrue(first.startswith('User rule stays.'))
        rules.install(home)
        self.assertEqual(path.read_text(encoding='utf-8'), first)

    def test_rule_block_documents_stages_handovers_and_new_session_rework(self):
        text = rules.block()
        guides = rules.ROOT / '.local-worker/workflow-guides'
        for guide in ('dispatch.md', 'limits.md', 'review.md', 'stages.md', 'delegation.md', 'opening.md'):
            self.assertIn(guide, text)
        text += '\n'.join(path.read_text(encoding='utf-8') for path in guides.glob('*.md'))
        for token in ('stage_store.py', 'task_handover.py', '--stage', '--reuse-session', 'final-accept', '待恢复',
                      'prerequisite handovers', 'stays cancelled', 'independent acceptance', 'stale reference'):
            self.assertIn(token, text)

    def test_acceptance_array_is_preserved_and_not_stringified(self):
        body = {**self.body, 'routing': {**self.body['routing'], 'acceptance': ['one', 'two']}}
        self.assertEqual(router.requirements(body)['acceptance'], ['one', 'two'])
        body['routing']['acceptance'] = 'one；two'
        self.assertEqual(router.requirements(body)['acceptance'], 'one；two')

    def test_budget_off_routes_without_money_caps_but_still_requires_paid_permission(self):
        soft = {**self.on, 'enforce_paid_budget': False, 'task_budget_cny': 0, 'daily_budget_cny': 0}
        body = {**self.body, 'route_mode': 'manual', 'model_id': 'paid'}
        route = self.choose(body, settings=soft, daily_remaining=0)
        self.assertFalse(route['budget_enforced'])
        self.assertEqual(route['budget_cny'], 0)
        with self.assertRaisesRegex(ValueError, '付费执行'):
            self.choose(body, settings={**soft, 'paid_allowed': False})
        self.paid['billing']['balance']['balance_infos'][0]['total_balance'] = '0'
        with self.assertRaisesRegex(ValueError, '余额不足'):
            self.choose(body, settings=soft)

    def test_budget_off_records_cost_and_keeps_request_limit(self):
        soft = {**self.on, 'enforce_paid_budget': False, 'task_budget_cny': 0, 'daily_budget_cny': 0, 'max_model_calls': 1}
        run = registry.task_dir('advisory-task')
        registry.patch_task(run, id='advisory-task', model_calls=0, route={'budget_cny': 0})
        relay.reserve(run, self.paid, soft, 1000, 1000)
        self.assertGreater(relay.daily_state(soft)['reserved_cny'], 0)
        with self.assertRaisesRegex(ValueError, '请求次数'):
            relay.reserve(run, self.paid, soft, 1000, 1000)

    def test_budget_mode_persists_without_changing_other_settings(self):
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'task_budget_cny': 5, 'daily_budget_cny': 20, 'max_model_calls': 30})
        before = settings.snapshot()
        settings.save_settings({'enforce_paid_budget': False})
        after = settings.snapshot()
        self.assertFalse(after['enforce_paid_budget'])
        self.assertEqual({k:v for k,v in before.items() if k != 'enforce_paid_budget'}, {k:v for k,v in after.items() if k != 'enforce_paid_budget'})
        with self.assertRaises(ValueError):
            settings.save_settings({'enforce_paid_budget': 0})

    def test_optional_call_limit_routes_large_estimate_and_records_all_calls(self):
        free = {**self.on, 'enforce_model_call_limit': False, 'enforce_paid_budget': False, 'max_model_calls': 1}
        body = {**self.body, 'routing': {**self.body['routing'], 'expected_calls': 50}}
        self.assertEqual(self.choose(body, settings=free)['model_id'], 'local')
        run = registry.task_dir('no-call-ceiling')
        registry.patch_task(run, id='no-call-ceiling', model_calls=0, route={'budget_cny': 0})
        for _ in range(3):
            relay.reserve(run, self.local, free, 1000, 1000)
        self.assertEqual(registry.read_task(run)['model_calls'], 3)
        with self.assertRaisesRegex(ValueError, '请求次数'):
            relay.reserve(run, self.local, {**free, 'enforce_model_call_limit': True}, 1000, 1000)

    def test_queued_auto_is_not_started_after_switch_off(self):
        run = registry.task_dir('queued-auto')
        registry.patch_task(run, id='queued-auto', model_id='local', kind='chat', status='queued', engine='dsh', route={'mode': 'auto'})
        with patch.object(panel.subprocess, 'Popen') as process:
            panel.execute_task('queued-auto')
        process.assert_not_called()
        state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'failed')
        self.assertIn('总开关', state['error'])

    def test_reserved_budget_survives_restart_and_daily_change(self):
        run = registry.task_dir('budget-task')
        registry.patch_task(run, id='budget-task', model_calls=0, budget_reserved_cny=0, route={'budget_cny': 1})
        relay.reserve(run, self.paid, self.on, 1000, 1000)
        amount = relay.daily_state(self.on)['reserved_cny']
        self.assertGreater(amount, 0)
        next_day = (datetime.now(relay.billing.CHINA) + timedelta(days=1)).date().isoformat()
        with patch.object(relay, 'day_key', return_value=next_day):
            self.assertEqual(relay.daily_state(self.on)['reserved_cny'], 0)
        self.assertEqual(relay.daily_state(self.on)['reserved_cny'], amount)

    def test_corrupt_ledger_blocks_paid_request(self):
        relay.ledger_path().write_text('{bad', encoding='utf-8')
        self.assertEqual(relay.daily_state(self.on)['remaining_cny'], 0)
        run = registry.task_dir('broken-ledger')
        registry.patch_task(run, model_calls=0, route={'budget_cny': 1})
        with self.assertRaisesRegex(ValueError, '账本'):
            relay.reserve(run, self.paid, self.on, 10, 10)


class RelayTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    tearDown = WorkflowTests.tearDown
    def test_gateway_auth_stream_usage_and_limits_prevent_upstream_calls(self):
        received = []
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5,"prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":3}}\n\ndata: [DONE]\n\n')
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        model = {**self.paid, 'provider': 'custom', 'api_base': f'http://127.0.0.1:{upstream.server_port}/v1', 'api_key': 'test-upstream-only',
                 'bypass_proxy': True, 'pricing_cny': {'cache_hit_input': 1, 'cache_miss_input': 2, 'output': 8}, 'max_output_tokens': 1000}
        run = registry.task_dir('relay-task')
        registry.patch_task(run, id='relay-task', model_calls=0, route={'budget_cny': 1})
        opener = build_opener(ProxyHandler({}))
        try:
            with relay.Relay(run, model, {**self.on, 'max_model_calls': 1}) as server:
                payload = {'model': model['model'], 'messages': [{'role': 'user', 'content': 'Hi'}], 'max_tokens': 1000, 'stream': True}
                def send(token=None, body=None):
                    headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (token or server.token)}
                    return opener.open(Request(server.url + '/chat/completions', data=json.dumps(body or payload).encode(), headers=headers), timeout=3)
                with self.assertRaises(HTTPError):
                    send('wrong')
                self.assertEqual(len(received), 0)
                with send() as response:
                    self.assertIn(b'OK', response.read())
                with self.assertRaises(HTTPError) as error:
                    send()
                self.assertEqual(error.exception.code, 402)
                self.assertEqual(len(received), 1)
                state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
                self.assertEqual(state['usage']['total_tokens'], 5)
                self.assertGreater(state['budget_reserved_cny'], 0)
                self.assertIn('次数', state['budget_error'])
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_budget_rejection_never_opens_remote_connection(self):
        run = registry.task_dir('small-cap')
        registry.patch_task(run, model_calls=0, route={'budget_cny': 0.0001}, cost_settled_cny=0.0001,
                            cost_settled_calls=1, cost_unsettled_cny=0)
        with relay.Relay(run, self.paid, self.on) as server, patch.object(relay, 'build_opener') as remote:
            opener = build_opener(ProxyHandler({}))
            body = {'model': 'deepseek-flash', 'messages': [{'role': 'user', 'content': 'Hi'}], 'max_tokens': 64000}
            request = Request(server.url + '/chat/completions', data=json.dumps(body).encode(), headers={'Authorization': 'Bearer ' + server.token})
            with self.assertRaises(HTTPError):
                opener.open(request, timeout=3)
            remote.assert_not_called()


class ExecutionScopePanelTests(unittest.TestCase):
    """The panel entry refuses an out-of-scope model instead of queueing it.

    Offline: the registry is a temporary directory, the executor submit is a
    stub, no provider is called and no real task record is created.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        registry.write_json(registry.REGISTRY, {'default_model': 'home', 'models': [
            {'id': 'home', 'name': 'home', 'provider': 'local', 'api_base': 'http://127.0.0.1:1234/v1',
             'model': 'qwen', 'secret': '', 'enabled': True, 'bypass_proxy': True,
             'max_input_tokens': 262144, 'max_output_tokens': 8192, 'last_check': None,
             'created_at': registry.now(), 'preferred_tasks': ['单文件修改'], 'cost_preference': 'economy',
             'max_task_complexity': 'simple', 'agent_preset': 'native'},
            {'id': 'cloud', 'name': 'cloud', 'provider': 'deepseek', 'api_base': 'https://api.deepseek.com',
             'model': 'deepseek-flash', 'secret': '', 'api_key_env': 'DS_TEST', 'enabled': True,
             'max_input_tokens': 1000000, 'max_output_tokens': 64000, 'last_check': None,
             'created_at': registry.now(), 'preferred_tasks': ['单文件修改'], 'cost_preference': 'balanced',
             'max_task_complexity': 'complex', 'agent_preset': 'standard',
             'billing': {'balance': {'status': 'ready', 'is_available': True, 'stale': False,
                                     'balance_infos': [{'currency': 'CNY', 'total_balance': '10'}]}}}]})

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def submit(self, body):
        submitted = []
        with patch.object(panel.dsh_worker, 'availability', return_value={'ready': True}), \
             patch.object(panel.EXECUTOR, 'submit', side_effect=lambda fn, task_id: submitted.append(task_id)):
            row = panel.submit_task(body)
        return row, submitted

    def test_an_out_of_scope_manual_model_is_refused_and_not_queued(self):
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'local_only'})
        body = {'prompt': '只读咨询', 'kind': 'chat', 'engine': 'direct', 'route_mode': 'manual',
                'model_id': 'cloud'}
        with self.assertRaisesRegex(ValueError, '执行模型范围'):
            self.submit(body)
        self.assertFalse(registry.RUNS.exists() and any(registry.RUNS.iterdir()))

    def test_a_manual_default_outside_the_scope_is_refused(self):
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'paid_only'})
        body = {'prompt': '只读咨询', 'kind': 'chat', 'engine': 'direct', 'route_mode': 'manual'}
        with self.assertRaisesRegex(ValueError, '执行模型范围'):
            self.submit(body)

    def test_an_out_of_scope_automatic_candidate_is_never_chosen_or_substituted(self):
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'local_only'})
        body = {'prompt': '只读咨询', 'kind': 'chat', 'engine': 'dsh', 'route_mode': 'auto',
                'routing': {'category': 'code_small', 'complexity': 'simple'}}
        row, submitted = self.submit(body)
        self.assertEqual(row['model_id'], 'home')
        self.assertEqual(submitted, [row['id']])
        excluded = {item['model_id']: item['reason'] for item in row['route']['excluded']}
        self.assertIn('cloud', excluded)
        self.assertIn('付费模型', excluded['cloud'])
        self.assertNotEqual(row['model_id'], 'cloud')

    def test_no_candidate_inside_the_scope_is_refused_with_a_reason(self):
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'local_only'})
        data = json.loads(registry.REGISTRY.read_text(encoding='utf-8'))
        data['models'] = [row for row in data['models'] if row['id'] == 'cloud']
        registry.write_json(registry.REGISTRY, data)
        body = {'prompt': '只读咨询', 'kind': 'chat', 'engine': 'dsh', 'route_mode': 'auto',
                'routing': {'category': 'code_small', 'complexity': 'simple'}}
        with self.assertRaises(ValueError) as error:
            self.submit(body)
        self.assertIn('执行模型范围', str(error.exception))
        self.assertIn('cloud', str(error.exception))
        self.assertFalse(registry.RUNS.exists() and any(registry.RUNS.iterdir()))

    def test_in_scope_models_still_queue_and_are_not_re_routed(self):
        settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'all'})
        row, submitted = self.submit({'prompt': '只读咨询', 'kind': 'chat', 'engine': 'direct',
                                      'route_mode': 'manual', 'model_id': 'home'})
        self.assertEqual(row['model_id'], 'home')
        self.assertEqual(submitted, [row['id']])
        local_only = settings.save_settings({'model_scope': 'local_only'})
        self.assertEqual(local_only['model_scope'], 'local_only')
        row, submitted = self.submit({'prompt': '只读咨询', 'kind': 'chat', 'engine': 'dsh',
                                      'route_mode': 'auto',
                                      'routing': {'category': 'code_small', 'complexity': 'simple'}})
        self.assertEqual(row['model_id'], 'home')
        self.assertEqual(row['route']['model_scope']['value'], 'local_only')


if __name__ == '__main__':
    unittest.main(verbosity=2)
