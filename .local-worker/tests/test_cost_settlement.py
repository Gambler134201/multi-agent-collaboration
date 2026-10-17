"""Usage-based settlement tests: real gateway mock, legacy-reserve exclusion and idempotency."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import budget_relay as relay
import model_registry as registry
import model_router as router

PEAK = datetime.fromisoformat('2026-10-13T14:00:00+08:00')
VALLEY = datetime.fromisoformat('2026-10-13T18:01:00+08:00')
USAGE = {'prompt_tokens': 1000, 'completion_tokens': 50, 'total_tokens': 1050,
         'prompt_cache_hit_tokens': 400, 'prompt_cache_miss_tokens': 600}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return predicate()


class UsageNormalizationTests(unittest.TestCase):
    def test_missing_or_partial_usage_is_not_valid(self):
        for usage in (None, 'x', {}, {'total_tokens': 1050}, {'prompt_tokens': 10},
                      {'completion_tokens': 10}, {'prompt_tokens': True, 'completion_tokens': 5},
                      {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}):
            with self.subTest(usage=usage):
                result = router.normalize_usage(usage)
                self.assertFalse(result['valid'])
                self.assertTrue(result['note'])

    def test_cache_split_and_cache_missing_bills_full_miss(self):
        full = router.normalize_usage(USAGE)
        self.assertTrue(full['valid'])
        self.assertEqual((full['usage']['prompt_cache_hit_tokens'], full['usage']['prompt_cache_miss_tokens']), (400, 600))
        missing = router.normalize_usage({'prompt_tokens': 100, 'completion_tokens': 10})
        self.assertTrue(missing['valid'])
        self.assertEqual((missing['usage']['prompt_cache_hit_tokens'], missing['usage']['prompt_cache_miss_tokens']), (0, 100))
        self.assertTrue(any('缓存' in issue for issue in missing['issues']))
        inconsistent = router.normalize_usage({'prompt_tokens': 100, 'completion_tokens': 10,
                                               'prompt_cache_hit_tokens': 10, 'prompt_cache_miss_tokens': 10})
        self.assertFalse(inconsistent['valid'])

    def test_invalid_fields_are_ignored_and_reasoning_is_not_double_counted(self):
        invalid = router.normalize_usage({'prompt_tokens': 10, 'completion_tokens': 5,
                                          'prompt_cache_hit_tokens': -1, 'total_tokens': 'x'})
        self.assertFalse(invalid['valid'])
        inclusive = router.normalize_usage({**USAGE, 'completion_tokens_details': {'reasoning_tokens': 20}})
        self.assertEqual(inclusive['usage']['completion_tokens'], 50)
        separate = router.normalize_usage({'prompt_tokens': 10, 'completion_tokens': 5,
                                           'completion_tokens_details': {'reasoning_tokens': 80}})
        self.assertFalse(separate['valid'])

    def test_usage_cost_uses_hit_miss_and_output_rates(self):
        rates = {'cache_hit_input': '1', 'cache_miss_input': '2', 'output': '8'}
        cost = router.usage_cost(rates, router.normalize_usage(USAGE))
        self.assertEqual(cost, Decimal(400 + 1200 + 400) / Decimal(1000000))


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.paid = {'id': 'paid', 'name': 'paid', 'provider': 'deepseek', 'api_base': 'https://api.deepseek.com',
                     'model': 'deepseek-flash', 'enabled': True, 'api_key_set': True,
                     'max_input_tokens': 1000000, 'max_output_tokens': 64000}
        self.custom = {'id': 'custom', 'name': 'custom', 'provider': 'custom', 'api_base': 'http://127.0.0.1:1/v1',
                       'model': 'm', 'enabled': True, 'api_key_set': True, 'max_input_tokens': 1000000,
                       'max_output_tokens': 64000, 'pricing_cny': {'cache_hit_input': 1, 'cache_miss_input': 2, 'output': 8}}
        self.local = {'id': 'local', 'name': 'local', 'provider': 'local', 'model': 'qwen', 'enabled': True,
                      'max_input_tokens': 262144, 'max_output_tokens': 8192}
        self.on = {'enabled': True, 'paid_allowed': True, 'enforce_paid_budget': True,
                   'enforce_model_call_limit': False, 'task_budget_cny': 1.0, 'daily_budget_cny': 5.0,
                   'max_model_calls': 30}

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def task(self, name='t'):
        run = registry.task_dir(name)
        registry.patch_task(run, id=name, model_calls=0, route={'budget_cny': 1.0})
        return run

    def day_state(self, day, settings=None):
        with patch.object(relay, 'day_key', return_value=day):
            return relay.daily_state(settings or self.on)

    def test_settlement_uses_actual_usage_not_the_output_cap(self):
        run = self.task('actual')
        amount = relay.reserve(run, self.paid, self.on, 500, 64000, request_id='r1', moment=PEAK)
        state = registry.read_task(run)
        day = state['cost_requests']['r1']['day']
        self.assertEqual(amount, router.cost(router.rates_for(self.paid, PEAK, ceiling=True), 500, 64000))
        self.assertGreater(amount, Decimal('0.5'))
        result = relay.settle(run, 'r1', USAGE)
        expected = router.usage_cost(router.rates_for(self.paid, PEAK), router.normalize_usage(USAGE))
        self.assertEqual(result['state'], 'settled')
        self.assertAlmostEqual(result['cost_cny'], float(expected), places=12)
        state = registry.read_task(run)
        self.assertAlmostEqual(state['cost_settled_cny'], float(expected), places=12)
        self.assertEqual(state['cost_unsettled_cny'], 0.0)
        daily = self.day_state(day)
        self.assertAlmostEqual(daily['settled_cny'], float(expected), places=12)
        self.assertEqual(daily['unsettled_cny'], 0.0)
        self.assertNotAlmostEqual(daily['settled_cny'], float(amount))
        self.assertTrue(daily['has_actual'])

    def test_repeated_usage_events_and_repeated_settle_do_not_add_twice(self):
        run = self.task('idempotent')
        relay.reserve(run, self.paid, self.on, 500, 1000, request_id='r1', moment=PEAK)
        first = relay.settle(run, 'r1', USAGE)
        second = relay.settle(run, 'r1', USAGE)
        third = relay.settle(run, 'r1', {'prompt_tokens': 9, 'completion_tokens': 9})
        self.assertFalse(first['duplicate'])
        self.assertTrue(second['duplicate'])
        self.assertTrue(third['duplicate'])
        state = registry.read_task(run)
        self.assertAlmostEqual(state['cost_settled_cny'], first['cost_cny'], places=12)
        self.assertEqual(state['cost_settled_calls'], 1)
        self.assertEqual(state['usage']['total_tokens'], 1050)

    def test_price_and_date_are_snapshotted_at_request_time(self):
        run = self.task('snapshot')
        relay.reserve(run, self.paid, self.on, 500, 1000, request_id='peak', moment=PEAK)
        peak = relay.settle(run, 'peak', USAGE)
        relay.reserve(run, self.paid, self.on, 500, 1000, request_id='valley', moment=VALLEY)
        state = registry.read_task(run)
        self.assertEqual(state['cost_requests']['peak']['day'], '2026-10-13')
        self.assertEqual(state['cost_requests']['peak']['rates']['output'], '8')
        self.assertEqual(state['cost_requests']['valley']['rates']['output'], '4')
        # Settle long after the request; the stored request-time price must be used.
        self.assertTrue(relay.settle(run, 'peak', USAGE)['duplicate'])
        valley = relay.settle(run, 'valley', USAGE)
        self.assertEqual(peak['cost_cny'], 2 * valley['cost_cny'])
        self.assertEqual(self.day_state('2026-10-13')['settled_cny'], peak['cost_cny'] + valley['cost_cny'])
        self.assertIsNone(self.day_state('2026-10-14')['settled_cny'])

    def test_missing_usage_stays_pending_and_still_consumes_budget(self):
        settings = {**self.on, 'daily_budget_cny': 0.01}
        run = self.task('unknown')
        relay.reserve(run, self.custom, settings, 1000, 1000, request_id='u1')
        result = relay.settle(run, 'u1', None, outcome='http_error')
        self.assertEqual(result['state'], 'unknown')
        state = registry.read_task(run)
        self.assertEqual(state['cost_settled_cny'], 0.0)
        self.assertGreater(state['cost_unsettled_cny'], 0)
        self.assertEqual(state['cost_unknown_calls'], 1)
        day = state['cost_requests']['u1']['day']
        daily = self.day_state(day, settings)
        self.assertIsNone(daily['settled_cny'])
        self.assertFalse(daily['has_actual'])
        self.assertGreater(daily['unsettled_cny'], 0)
        self.assertEqual(daily['unknown_calls'], 1)
        with self.assertRaisesRegex(ValueError, '预算'):
            relay.reserve(run, self.custom, settings, 1000, 1000, request_id='u2')

    def test_settled_cost_reaching_cap_stops_later_requests(self):
        settings = {**self.on, 'daily_budget_cny': 0.000025}
        run = self.task('cap')
        relay.reserve(run, self.custom, settings, 1, 1, request_id='c1')
        relay.settle(run, 'c1', {'prompt_tokens': 1000, 'completion_tokens': 1000, 'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 1000})
        state = registry.read_task(run)
        self.assertGreater(state['cost_settled_cny'], 0.000025)  # one in-flight request may cross the cap
        with self.assertRaisesRegex(ValueError, '预算'):
            relay.reserve(run, self.custom, settings, 1, 1, request_id='c2')

    def test_settled_cost_reaching_task_limit_stops_later_requests(self):
        run = self.task('taskcap')
        registry.patch_task(run, route={'budget_cny': 0.003})
        relay.reserve(run, self.custom, self.on, 1, 1, request_id='t1')
        relay.settle(run, 't1', {'prompt_tokens': 1000, 'completion_tokens': 1000, 'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 1000})
        state = registry.read_task(run)
        self.assertGreater(state['cost_settled_cny'], 0.003)
        with self.assertRaisesRegex(ValueError, '预算'):
            relay.reserve(run, self.custom, self.on, 1, 1, request_id='t2')

    def test_routing_remaining_uses_settled_cost_not_the_reservation(self):
        run = self.task('remaining')
        amount = relay.reserve(run, self.paid, self.on, 500, 64000, request_id='r1', moment=PEAK)
        state = registry.read_task(run)
        day = state['cost_requests']['r1']['day']
        relay.settle(run, 'r1', USAGE)
        daily = self.day_state(day)
        self.assertGreater(amount, Decimal('0.5'))
        self.assertAlmostEqual(daily['remaining_cny'], 5.0 - daily['settled_cny'], places=9)
        self.assertGreater(daily['remaining_cny'], 4.9)

    def test_budget_off_records_settlement_without_blocking(self):
        settings = {**self.on, 'enforce_paid_budget': False, 'task_budget_cny': 0, 'daily_budget_cny': 0}
        run = self.task('soft')
        relay.reserve(run, self.custom, settings, 1, 64000, request_id='s1')
        result = relay.settle(run, 's1', USAGE)
        state = registry.read_task(run)
        self.assertEqual(result['state'], 'settled')
        self.assertGreater(state['cost_settled_cny'], 0)
        self.assertGreater(state['budget_reserved_cny'], state['cost_settled_cny'])

    def test_legacy_ledger_reservations_are_raw_history_not_a_block(self):
        legacy_reserved = '92.058166'
        registry.write_json(relay.ledger_path(),
                            {'days': {'2026-10-13': {'reserved_cny': legacy_reserved, 'calls': 7}}})
        daily = self.day_state('2026-10-13')
        self.assertIsNone(daily['settled_cny'])
        self.assertFalse(daily['has_actual'])
        # The old value stays readable as a raw compatibility/audit field...
        self.assertEqual(daily['legacy_reserved_cny'], float(legacy_reserved))
        self.assertEqual(daily['unsettled_cny'], 0.0)
        self.assertEqual(daily['budget_used_cny'], 0.0)
        self.assertNotEqual(daily['settled_cny'], 0)
        self.assertEqual(json.loads(relay.ledger_path().read_text(encoding='utf-8'))
                         ['days']['2026-10-13']['reserved_cny'], legacy_reserved)
        # ... but it never zeroes remaining/unresolved nor blocks routing or the gateway.
        self.assertEqual(daily['remaining_cny'], 5.0)
        self.assertFalse(daily['unresolved'])
        body = {'kind': 'edit', 'engine': 'dsh', 'route_mode': 'manual', 'model_id': 'custom', 'prompt': 'task',
                'routing': {'category': 'code_small', 'complexity': 'simple', 'input_tokens': 500,
                            'output_tokens': 1000, 'expected_calls': 1}}
        route = router.choose(body, [self.local, self.custom], self.on,
                              daily_remaining=daily['remaining_cny'], daily_uncertain=daily['unresolved'])
        self.assertEqual(route['model_id'], 'custom')
        run = self.task('after-legacy')
        with patch.object(relay, 'day_key', return_value='2026-10-13'):
            amount = relay.reserve(run, self.custom, self.on, 500, 1000, request_id='r1')
        self.assertGreater(amount, 0)
        daily = self.day_state('2026-10-13')
        self.assertAlmostEqual(daily['unsettled_cny'], float(amount), places=12)
        self.assertTrue(daily['unresolved'])  # a current in-flight request is still unresolved
        self.assertEqual(daily['legacy_reserved_cny'], float(legacy_reserved))

    def test_legacy_task_reservations_are_raw_history_not_a_block(self):
        run = registry.task_dir('old-task')
        registry.patch_task(run, id='old-task', model_calls=3, budget_reserved_cny=2.25, route={'budget_cny': 5})
        state = registry.read_task(run)
        costs = relay.task_cost_state(state)
        self.assertIsNone(costs['settled_cny'])
        self.assertNotIn('legacy_unsettled_cny', costs)
        self.assertEqual(costs['unsettled_cny'], Decimal(0))
        self.assertEqual(costs['used_cny'], Decimal(0))
        self.assertEqual(state['budget_reserved_cny'], 2.25)  # raw history preserved on disk
        amount = relay.reserve(run, self.custom, self.on, 500, 1000, request_id='r1')
        self.assertGreater(amount, 0)
        stored = registry.read_task(run)
        self.assertEqual(stored['cost_legacy_unsettled_cny'], 2.25)  # compat field kept raw
        self.assertGreater(stored['cost_unsettled_cny'], 0)

    def test_concurrent_reserve_and_settle_stay_consistent(self):
        run = self.task('concurrent')
        settings = {**self.on, 'enforce_paid_budget': False, 'task_budget_cny': 0, 'daily_budget_cny': 0}
        errors = []

        def worker(index):
            try:
                relay.reserve(run, self.custom, settings, 100, 100, request_id=f'c{index}')
                relay.settle(run, f'c{index}', USAGE)
            except Exception as exc:  # pragma: no cover - reported through the list
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        state = registry.read_task(run)
        self.assertEqual(state['model_calls'], 5)
        self.assertEqual(state['cost_settled_calls'], 5)
        expected = router.usage_cost(self.custom['pricing_cny'], router.normalize_usage(USAGE))
        self.assertAlmostEqual(state['cost_settled_cny'], float(expected * 5), places=12)
        self.assertEqual(state['cost_unsettled_cny'], 0.0)

    def test_large_future_output_is_advisory_before_exact_cap_is_reached(self):
        run = self.task('near-cap')
        registry.patch_task(run, cost_settled_cny=0.999, cost_settled_calls=1, cost_unsettled_cny=0)
        relay.reserve(run, self.custom, self.on, 1000, 64000, request_id='next')
        relay.settle(run, 'next', USAGE)
        run = self.task('exact-cap')
        registry.patch_task(run, cost_settled_cny=1, cost_settled_calls=1, cost_unsettled_cny=0)
        with self.assertRaisesRegex(ValueError, '达到'):
            relay.reserve(run, self.custom, self.on, 1, 1, request_id='blocked')

    def test_repeated_request_id_is_blocked_before_duplicate_reservation(self):
        soft = {**self.on, 'enforce_paid_budget': False}
        run = self.task('duplicate-id')
        relay.reserve(run, self.custom, soft, 1000, 1000, request_id='same')
        with self.assertRaisesRegex(ValueError, '重复'):
            relay.reserve(run, self.custom, soft, 1000, 1000, request_id='same')
        self.assertEqual(registry.read_task(run)['model_calls'], 1)

    def test_partial_stream_usage_and_missing_cache_cost_stay_unconfirmed(self):
        soft = {**self.on, 'enforce_paid_budget': False}
        run = self.task('partial')
        relay.reserve(run, self.custom, soft, 1000, 1000, request_id='partial')
        with relay.Relay(run, self.custom, soft) as server:
            server._local.request_id = 'partial'
            server.record_usage(USAGE)
            self.assertEqual(server.finish_request('partial', 'stream_interrupted')['state'], 'unknown')
        relay.reserve(run, self.custom, soft, 1000, 1000, request_id='no-cache')
        self.assertEqual(relay.settle(run, 'no-cache', {'prompt_tokens': 100, 'completion_tokens': 10})['state'], 'unknown')
        self.assertEqual(registry.read_task(run)['cost_settled_calls'], 0)

    def test_settlement_write_failure_keeps_task_pending_and_recovers_once(self):
        soft = {**self.on, 'enforce_paid_budget': False}
        run = self.task('failed-write')
        relay.reserve(run, self.custom, soft, 1000, 1000, request_id='retry')
        with patch.object(registry, 'write_json', side_effect=PermissionError('injected')):
            with self.assertRaises(PermissionError):
                relay.settle(run, 'retry', USAGE)
        self.assertEqual(registry.read_task(run)['cost_requests']['retry']['state'], 'pending')
        result = relay.settle(run, 'retry', USAGE)
        self.assertFalse(result['duplicate'])
        self.assertTrue(relay.settle(run, 'retry', USAGE)['duplicate'])

    def test_generic_cached_tokens_field_is_priced_without_guessing(self):
        normalized = router.normalize_usage({'prompt_tokens': 100, 'completion_tokens': 10,
                                             'prompt_tokens_details': {'cached_tokens': 60}})
        self.assertTrue(normalized['valid'])
        self.assertTrue(normalized['cache_complete'])
        self.assertEqual(normalized['usage']['prompt_cache_miss_tokens'], 40)


class RelaySettlementTests(unittest.TestCase):
    setUp = SettlementTests.setUp
    tearDown = SettlementTests.tearDown
    task = SettlementTests.task
    day_state = SettlementTests.day_state

    def upstream(self, mode):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if mode == 'error':
                    self.send_response(500)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if mode == 'stream':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    usage = json.dumps({'choices': [], 'usage': USAGE}).encode()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n')
                    self.wfile.write(b'data: ' + usage + b'\n\n')
                    self.wfile.write(b'data: ' + usage + b'\n\n')  # duplicate usage event
                    self.wfile.write(b'data: [DONE]\n\n')
                else:
                    event = {'choices': [{'message': {'content': 'OK'}}]}
                    if mode != 'nousage':
                        event['usage'] = USAGE
                    data = json.dumps(event).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/v1'

    def send(self, server, model, stream, max_tokens=64000):
        opener = build_opener(ProxyHandler({}))
        body = {'model': model['model'], 'messages': [{'role': 'user', 'content': 'Hi'}],
                'max_tokens': max_tokens, 'stream': stream}
        request = Request(server.url + '/chat/completions', data=json.dumps(body).encode(),
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + server.token})
        return opener.open(request, timeout=5)

    def settle_and_read(self, run, request_id):
        def settled():
            record = registry.read_task(run)['cost_requests'].get(request_id) or {}
            return record.get('state') != 'pending'
        self.assertTrue(wait_for(settled), 'request did not settle in time')
        return registry.read_task(run)

    def test_stream_and_nonstream_settle_on_actual_usage_once(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                relay.ledger_path().unlink(missing_ok=True)
                port = self.upstream('stream' if stream else 'json')
                model = {**self.custom, 'api_base': port, 'bypass_proxy': True}
                run = self.task('relay-' + ('stream' if stream else 'json'))
                registry.patch_task(run, route={'budget_cny': 5.0})
                with relay.Relay(run, model, self.on) as server:
                    request_id = None
                    with self.send(server, model, stream) as response:
                        response.read()
                    request_id = next(iter(registry.read_task(run)['cost_requests']))
                state = self.settle_and_read(run, request_id)
                record = state['cost_requests'][request_id]
                expected = router.usage_cost(model['pricing_cny'], router.normalize_usage(USAGE))
                self.assertEqual(record['state'], 'settled')
                self.assertAlmostEqual(state['cost_settled_cny'], float(expected), places=12)
                self.assertEqual(state['cost_settled_calls'], 1)
                self.assertEqual(state['usage']['total_tokens'], 1050)
                self.assertGreater(state['budget_reserved_cny'], 0.4)
                self.assertNotAlmostEqual(state['cost_settled_cny'], state['budget_reserved_cny'])
                daily = self.day_state(record['day'])
                self.assertAlmostEqual(daily['settled_cny'], float(expected), places=12)
                self.assertEqual(daily['unsettled_cny'], 0.0)

    def test_stream_without_usage_is_pending_not_free(self):
        port = self.upstream('nousage')
        model = {**self.custom, 'api_base': port, 'bypass_proxy': True}
        run = self.task('relay-nousage')
        registry.patch_task(run, route={'budget_cny': 5.0})
        with relay.Relay(run, model, self.on) as server:
            with self.send(server, model, False) as response:
                response.read()
            request_id = next(iter(registry.read_task(run)['cost_requests']))
        state = self.settle_and_read(run, request_id)
        record = state['cost_requests'][request_id]
        self.assertEqual(record['state'], 'unknown')
        self.assertIsNone(record['cost_cny'])
        self.assertGreater(state['cost_unsettled_cny'], 0)
        self.assertEqual(state['cost_unknown_calls'], 1)
        self.assertEqual(state['cost_settled_cny'], 0.0)

    def test_http_failure_keeps_conservative_reservation_as_pending(self):
        port = self.upstream('error')
        model = {**self.custom, 'api_base': port, 'bypass_proxy': True}
        run = self.task('relay-error')
        registry.patch_task(run, route={'budget_cny': 5.0})
        with relay.Relay(run, model, self.on) as server:
            with self.assertRaises(HTTPError) as error:
                self.send(server, model, False)
            self.assertEqual(error.exception.code, 502)
            request_id = next(iter(registry.read_task(run)['cost_requests']))
        state = self.settle_and_read(run, request_id)
        record = state['cost_requests'][request_id]
        self.assertEqual(record['state'], 'unknown')
        self.assertIsNone(record['cost_cny'])
        self.assertEqual(state['cost_settled_cny'], 0.0)
        self.assertGreater(state['cost_unsettled_cny'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
