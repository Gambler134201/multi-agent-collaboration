"""Token metering tests: provenance, trust, window semantics and no false rejections.

Everything here is offline and mock-based. No generation request is ever sent to
a real provider or engine, and the only network use is loopback test servers.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import budget_relay as relay
import model_registry as registry
import token_meter as meter


def cjk(chars):
    return '这是一段用于计量测试的中文文本，' * (chars // 16 + 1)


class MeterCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        self.local = {'id': 'home', 'name': 'home', 'provider': 'local', 'model': 'qwen',
                      'api_base': 'https://example.invalid/v1', 'enabled': True,
                      'max_input_tokens': 262144, 'max_output_tokens': 64000, 'bypass_proxy': True}
        self.paid = {'id': 'paid', 'name': 'paid', 'provider': 'deepseek', 'model': 'deepseek-flash',
                     'api_base': 'https://api.deepseek.com', 'enabled': True, 'api_key_set': True,
                     'max_input_tokens': 1000000, 'max_output_tokens': 64000}
        self.on = {'enabled': True, 'paid_allowed': True, 'enforce_paid_budget': False,
                   'enforce_model_call_limit': False, 'task_budget_cny': 0, 'daily_budget_cny': 0,
                   'max_model_calls': 30, 'timeout': 5}
        meter.reset_probe_cache()
        meter.reset_metrics_cache()
        meter.reset_calibration()

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        meter.reset_probe_cache()
        meter.reset_metrics_cache()
        meter.reset_calibration()
        self.temp.cleanup()

    def verified_engine(self, model, tokens, covers_tools=True, covers_template=True):
        """Evidence record + callable for an engine chat counter verified for this identity."""
        evidence = {'verified_sources': [{
            'source': meter.SOURCE_ENGINE_TOKENIZE, 'verified': True,
            'identity': meter.cache_identity(model).as_dict(),
            'verified_by': 'mock 引擎完整聊天计数与模型版本/模板一致'}]}
        return evidence, (lambda body: {'tokens': tokens, 'covers_tools': covers_tools,
                                        'covers_template': covers_template})


class EstimateTests(MeterCase):
    def test_opt_in_task_output_limit_bounds_explicit_sdk_maximum(self):
        result = meter.resolve_output_target({'max_tokens': 64000}, self.local, task_target=8192)
        self.assertEqual(result['target'], 8192)
        self.assertTrue(result['task_limit_applied'])
        self.assertEqual(self.local['max_output_tokens'], 64000)
        self.assertEqual(meter.resolve_output_target({'max_tokens': 1000}, self.local, task_target=8192)['target'], 1000)

    def test_verified_encoding_of_transport_json_is_still_not_rendered_chat(self):
        evidence = {'verified_sources': [{'source': meter.SOURCE_OFFLINE_TOKENIZER,
            'verified': True, 'identity': meter.cache_identity(self.local).as_dict(), 'encoding': 'mock'}]}
        spec = meter.WindowSpec(kind=meter.WINDOW_COMBINED, input_window=1000,
                                output_cap=1000, source='mock', verified=True)
        with patch.object(meter, 'count_with_offline_tokenizer', return_value={'count': 9000, 'encoding': 'mock'}):
            result = meter.measure(self.local, {'messages': [{'role': 'user', 'content': 'Hi'}], 'max_tokens': 10},
                                   evidence=evidence, declared_window=spec, allow_offline_tokenizer=True)
        self.assertEqual(result['input']['precision'], meter.APPROXIMATE)
        self.assertNotEqual(result['guard'], 'reject')

    def test_bare_counter_without_full_template_and_tools_coverage_cannot_reject(self):
        evidence, _ = self.verified_engine(self.local, 99999)
        spec = meter.WindowSpec(kind=meter.WINDOW_COMBINED, input_window=1000,
                                output_cap=1000, source='mock', verified=True)
        result = meter.measure(self.local, {'messages': [{'role': 'user', 'content': 'Hi'}], 'max_tokens': 10},
                               evidence=evidence, declared_window=spec, engine_tokenize=lambda _: 99999)
        self.assertEqual(result['input']['precision'], meter.APPROXIMATE)
        self.assertNotEqual(result['guard'], 'reject')

    def test_byte_proxy_is_labelled_approximate_and_never_exact(self):
        body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': cjk(4000)}]}
        record = meter.measure(self.local, body)
        self.assertEqual(record['input']['precision'], meter.APPROXIMATE)
        self.assertEqual(record['input']['source'], meter.SOURCE_BYTE_PROXY)
        self.assertNotEqual(record['sources']['precision'], meter.EXACT)
        self.assertEqual(record['guard'], 'proceed')

    def test_old_byte_formula_would_have_rejected_but_meter_does_not(self):
        content = cjk(130000)
        body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': content}], 'max_tokens': 4096}
        raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
        legacy_bound = len(raw) + 1024 + 64
        self.assertGreater(legacy_bound + 4096, self.local['max_input_tokens'])
        self.assertLess(len(raw), relay.PROTOCOL_BYTE_CEILING)
        record = meter.measure(self.local, body)
        self.assertEqual(record['guard'], 'proceed')
        self.assertTrue(any('近似' in warning for warning in record['warnings']))

    def test_estimate_has_value_unit_source_trust_and_identity(self):
        body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'hello'}],
                'tools': [{'type': 'function', 'function': {'name': 'read', 'description': 'read a file'}}]}
        record = meter.measure(self.local, body)
        self.assertIsInstance(record['input']['tokens'], int)
        self.assertEqual(record['output']['unit'], 'tokens')
        self.assertIn(record['input']['source'], {row['source'] for row in meter.implemented_candidates()})
        self.assertIn(record['sources']['precision'], meter.CONFIDENCES)
        self.assertEqual(record['identity']['model'], 'qwen')
        self.assertEqual(record['identity']['api_base'], 'https://example.invalid/v1')

    def test_tools_and_template_are_part_of_the_measured_payload(self):
        with_tools = {'messages': [{'role': 'user', 'content': 'hi'}],
                      'tools': [{'type': 'function', 'function': {'name': 'x' * 400}}]}
        without = {'messages': [{'role': 'user', 'content': 'hi'}]}
        body_record = meter.measure(self.local, with_tools)
        plain_record = meter.measure(self.local, without)
        self.assertGreater(body_record['input']['tokens'], plain_record['input']['tokens'])
        # The count deliberately still labels itself as not covering tools exactly.
        self.assertFalse(body_record['sources']['covers_tools'])
        self.assertFalse(body_record['sources']['covers_template'])

    def test_record_summary_is_compact_but_keeps_provenance(self):
        record = meter.measure(self.local, {'messages': [{'role': 'user', 'content': cjk(200)}]})
        summary = meter.record_summary(record)
        self.assertEqual(summary['sources'], record['sources'])
        self.assertEqual(summary['guard'], record['guard'])
        self.assertEqual(summary['window']['enforceable'], record['window']['enforceable'])
        self.assertIn('estimate_class_bytes', summary['input'])
        self.assertNotIn('estimate_classes', summary['input'])
        self.assertLess(len(json.dumps(summary, ensure_ascii=False)),
                        len(json.dumps(record, ensure_ascii=False)))
        self.assertEqual(meter.record_summary(None), {})

    def test_count_that_ignores_fragments_cannot_be_promoted_to_exact(self):
        evidence, counter = self.verified_engine(self.local, 10, covers_tools=False)
        body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'hi'}],
                'tools': [{'type': 'function', 'function': {'name': 'x'}}]}
        record = meter.measure(self.local, body, evidence=evidence, engine_tokenize=counter)
        self.assertEqual(record['input']['precision'], meter.APPROXIMATE)
        self.assertEqual(record['input']['source'], meter.SOURCE_BYTE_PROXY)
        self.assertEqual(record['guard'], 'proceed')


class WindowSemanticsTests(MeterCase):
    def test_combined_and_split_are_not_the_same_rule(self):
        combined = meter.WindowSpec(kind=meter.WINDOW_COMBINED, input_window=1000, output_cap=1000,
                                    source='mock', verified=True)
        split = meter.WindowSpec(kind=meter.WINDOW_SPLIT, input_window=1000, output_cap=100,
                                 source='mock', verified=True)
        body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'x'}], 'max_tokens': 500}
        evidence, counter = self.verified_engine(self.local, 900)
        # prompt 900 + output 500 = 1400 > 1000 combined window
        self.assertEqual(meter.measure(self.local, body, evidence=evidence,
                                       declared_window=combined, engine_tokenize=counter)['guard'], 'reject')
        # prompt 900 <= input window 1000, but output 500 > output capability 100
        self.assertEqual(meter.measure(self.local, body, evidence=evidence,
                                       declared_window=split, engine_tokenize=counter)['guard'], 'reject')
        # and a request that fits both is allowed under both rules
        small = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'x'}], 'max_tokens': 50}
        for spec in (combined, split):
            self.assertEqual(meter.measure(self.local, small, evidence=evidence,
                                           declared_window=spec, engine_tokenize=counter)['guard'], 'proceed')

    def test_vendor_window_shapes_are_distinct_per_provider(self):
        self.assertEqual(meter.window_spec(self.local).kind, meter.WINDOW_COMBINED)
        self.assertEqual(meter.window_spec(self.paid).kind, meter.WINDOW_SPLIT)
        custom = {**self.paid, 'provider': 'custom'}
        self.assertEqual(meter.window_spec(custom).kind, meter.WINDOW_SPLIT)

    def test_registry_limits_alone_never_enforce(self):
        body = {'model': 'deepseek-flash', 'messages': [{'role': 'user', 'content': 'x' * 200000}],
                'max_tokens': 64000}
        record = meter.measure(self.paid, body)
        self.assertEqual(record['guard'], 'proceed')
        self.assertFalse(record['window']['semantics_verified'])
        self.assertEqual(record['window']['kind'], meter.WINDOW_SPLIT)
        self.assertEqual(record['window']['fit']['state'], 'unknown')

    def test_exact_count_with_unverified_window_only_warns(self):
        evidence, counter = self.verified_engine(self.local, 999999)
        body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 10}
        record = meter.measure(self.local, body, evidence=evidence, engine_tokenize=counter)
        self.assertEqual(record['input']['precision'], meter.EXACT)
        self.assertNotEqual(record['guard'], 'reject')
        self.assertEqual(record['guard'], 'warn')
        self.assertTrue(any('窗口' in warning for warning in record['warnings']))

    def test_verified_exact_over_window_rejects_and_legal_request_sends(self):
        evidence, counter = self.verified_engine(self.local, 900)
        spec = meter.WindowSpec(kind=meter.WINDOW_COMBINED, input_window=1000, output_cap=1000,
                                source='mock 已验证', verified=True)
        over = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'x'}], 'max_tokens': 500}
        record = meter.measure(self.local, over, evidence=evidence, declared_window=spec,
                               engine_tokenize=counter)
        self.assertEqual(record['guard'], 'reject')
        self.assertIn('qwen', meter.guard_message(record) + record['model'])
        legal = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'x'}], 'max_tokens': 50}
        self.assertEqual(meter.measure(self.local, legal, evidence=evidence, declared_window=spec,
                                       engine_tokenize=counter)['guard'], 'proceed')

    def test_evidence_must_match_model_and_template_identity(self):
        evidence, counter = self.verified_engine(self.local, 999999)
        spec = meter.WindowSpec(kind=meter.WINDOW_COMBINED, input_window=1000, output_cap=1000,
                                source='mock', verified=True)
        body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'x'}], 'max_tokens': 400}
        # A different model version, another api_base, or another template must not reuse it.
        for other in ({**self.local, 'model': 'qwen-v2'},
                      {**self.local, 'api_base': 'https://other.invalid/v1'},
                      {**self.local, 'thinking_info': {'api_base': self.local['api_base'],
                                                       'model': 'qwen', 'template_sha256': 'b' * 64}}):
            record = meter.measure(other, body, evidence=evidence, declared_window=spec,
                                   engine_tokenize=counter)
            self.assertEqual(record['input']['precision'], meter.APPROXIMATE)
            self.assertEqual(record['guard'], 'proceed')
        record = meter.measure(self.local, body, evidence=evidence, declared_window=spec,
                               engine_tokenize=counter)
        self.assertEqual(record['guard'], 'reject')


class OutputTargetTests(MeterCase):
    def test_model_capability_and_request_target_are_recorded_separately(self):
        body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 1000}
        resolved = meter.resolve_output_target(body, self.local)
        self.assertEqual(resolved['target'], 1000)
        self.assertEqual(resolved['capability_ceiling'], 64000)
        self.assertEqual(resolved['origin'], 'explicit_max_tokens')
        self.assertFalse(resolved['capped_to_model_limit'])

    def test_task_level_request_output_tokens_is_used_and_capped_to_capability(self):
        resolved = meter.resolve_output_target({}, self.local, 200000)
        self.assertEqual(resolved['target'], 64000)
        self.assertEqual(resolved['origin'], 'task_request_output_tokens')
        self.assertTrue(resolved['capped_to_model_limit'])

    def test_missing_target_is_an_estimate_not_a_reasoning_cap(self):
        resolved = meter.resolve_output_target({}, self.local)
        self.assertEqual(resolved['origin'], 'configured_model_default')
        self.assertEqual(resolved['target'], 64000)
        self.assertIsNone(resolved['requested'])
        self.assertEqual(resolved['capability_ceiling'], 64000)

    def test_invalid_targets_are_rejected(self):
        for value in (0, -1, True, 'x', 1.5):
            with self.assertRaises(ValueError):
                meter.resolve_output_target({'max_tokens': value}, self.local)
        with self.assertRaises(ValueError):
            meter.resolve_output_target({}, self.local, 0)

    def test_late_bound_resolution_prefers_body_then_task_then_estimate(self):
        self.assertEqual(meter.resolve_request_output_target({'max_tokens': 123}),
                         (123, 'explicit_max_tokens'))
        self.assertEqual(meter.resolve_request_output_target({'request_output_tokens': 321}),
                         (321, 'task_request_output_tokens'))
        self.assertEqual(meter.resolve_request_output_target({}),
                         (4096, 'fallback_estimate_4096'))
        for bad in (0, -1, True, 'x', 1.5):
            with self.assertRaises(ValueError):
                meter.resolve_request_output_target({'max_tokens': bad})


class ProbeTests(MeterCase):
    def test_probe_uses_only_bounded_read_only_requests(self):
        calls = []

        def fake_post(origin, path, headers, bypass, payload, timeout=meter.PROBE_TIMEOUT):
            calls.append(('POST', path, timeout, dict(payload)))
            raise OSError('no such endpoint')

        def fake_get(origin, path, headers, bypass, timeout=meter.PROBE_TIMEOUT):
            calls.append(('GET', path, timeout, None))
            if path == '/props':
                return {'build_info': 'Strata 0.1.39', 'model_alias': 'qwen',
                        'default_generation_settings': {'n_ctx': 262144}, 'chat_template': 'T'}
            if path == '/metrics':
                return {'engine': {'max_context': 262144},
                        'live': {'state': 'idle', 'prompt_tokens': None},
                        'requests': [{'prompt_tokens': 42}]}
            raise OSError('no')

        with patch.object(meter, '_http_post', side_effect=fake_post), \
                patch.object(meter, '_http_get', side_effect=fake_get, create=True):
            report = meter.probe_metering_capability(self.local, force=True)
        self.assertEqual(report['generation_requests'], 0)
        self.assertEqual(report['tokenize_endpoint'], 'unverified')
        self.assertEqual(report['apply_template_endpoint'], 'unverified')
        self.assertEqual(report['engine_reported_prompt_tokens'], 'observed_in_metrics')
        self.assertTrue(report['read_only'])
        self.assertTrue(any('未发现' in note for note in report['notes']))
        self.assertEqual(report['engine_context_window'], 262144)

    def test_probe_caches_success_and_does_not_repeat_reads(self):
        calls = []

        def fake_get(origin, path, headers, bypass, timeout=meter.PROBE_TIMEOUT):
            calls.append(path)
            return {'chat_template': 'T', 'model_alias': 'qwen', 'build_info': 'Strata 0.1.39'}

        with patch.object(meter, '_http_post', side_effect=OSError('no')), \
                patch.object(meter, '_http_get', side_effect=fake_get, create=True):
            meter.probe_metering_capability(self.local, force=True)
        first = len(calls)
        cached = meter.probe_metering_capability(self.local)
        self.assertTrue(cached.get('cached'))
        self.assertEqual(len(calls), first)
        self.assertIsNotNone(meter.cached_probe(self.local))

    def test_probe_reports_network_failure_as_a_metering_limitation(self):
        with patch.object(meter, '_http_get', side_effect=OSError('offline'), create=True), \
                patch.object(meter, '_http_post', side_effect=OSError('offline')):
            report = meter.probe_metering_capability(self.local, force=True)
        self.assertEqual(report['generation_requests'], 0)
        self.assertTrue(report['errors'])
        self.assertIn('未发现', report['conclusion'])

    def test_engine_metrics_are_read_at_most_once_per_ttl(self):
        reads = []

        def fake_get(origin, path, headers, bypass, timeout=meter.PROBE_TIMEOUT):
            reads.append(path)
            return {'engine': {'max_context': 262144}, 'live': {'state': 'idle'}}

        now = [1000.0]
        with patch.object(meter, '_http_get', side_effect=fake_get, create=True):
            meter.read_engine_metrics_cached(self.local, clock=lambda: now[0])
            meter.read_engine_metrics_cached(self.local, clock=lambda: now[0])
        self.assertEqual(reads, ['/metrics'])
        now[0] += meter.METRICS_TTL + 1
        with patch.object(meter, '_http_get', side_effect=fake_get, create=True):
            meter.read_engine_metrics_cached(self.local, clock=lambda: now[0])
        self.assertEqual(reads, ['/metrics', '/metrics'])

    def test_engine_metrics_failure_is_not_a_model_failure(self):
        with patch.object(meter, '_http_get', side_effect=OSError('down'), create=True):
            self.assertIsNone(meter.read_engine_metrics(self.local))
            self.assertIsNone(meter.read_engine_metrics_cached(self.local))


class EvidenceFileTests(MeterCase):
    def test_missing_or_corrupt_evidence_is_empty_not_fatal(self):
        path = Path(self.temp.name) / 'evidence.json'
        self.assertEqual(meter.read_evidence(path), {})
        path.write_text('{bad', encoding='utf-8')
        self.assertEqual(meter.read_evidence(path), {})
        path.write_text(json.dumps({'verified_sources': 'oops'}), encoding='utf-8')
        self.assertIsNone(meter.verified_source(self.local, None, meter.read_evidence(path)))

    def test_evidence_records_are_validated_field_by_field(self):
        identity = meter.cache_identity(self.local).as_dict()
        for row in ({'source': meter.SOURCE_ENGINE_TOKENIZE, 'identity': identity},
                    {'source': meter.SOURCE_ENGINE_TOKENIZE, 'verified': False, 'identity': identity},
                    {'source': meter.SOURCE_OFFLINE_TOKENIZER, 'verified': True, 'identity': identity},
                    {'source': meter.SOURCE_BYTE_PROXY, 'verified': True, 'identity': identity}):
            self.assertIsNone(meter.verified_source(self.local, None, {'verified_sources': [row]}))


class CalibrationTests(MeterCase):
    def test_calibration_links_source_to_reported_prompt_tokens_without_claiming_precision(self):
        record = meter.measure(self.local, {'model': 'qwen',
                                            'messages': [{'role': 'user', 'content': 'hello'}]})
        observation = meter.record_calibration(self.local, record, {'prompt_tokens': 1234,
                                                                    'completion_tokens': 7})
        self.assertEqual(observation['actual_prompt_tokens'], 1234)
        self.assertEqual(observation['precision'], meter.APPROXIMATE)
        self.assertIn('事后校准', observation['note'])
        self.assertIn('不是发出前精确计数', observation['note'])
        snapshot = meter.calibration_snapshot()
        key = next(iter(snapshot))
        self.assertEqual(snapshot[key]['observed'], 1)
        self.assertEqual(snapshot[key]['actual_total'], 1234)

    def test_calibration_accumulates_class_bytes_for_observability_only(self):
        body = {'messages': [{'role': 'user', 'content': cjk(400) + 'ascii text'}]}
        record = meter.measure(self.local, body)
        observation = meter.record_calibration(self.local, record, {'prompt_tokens': 1000})
        self.assertIn('cjk_bytes', observation['estimate_classes'])
        self.assertGreater(observation['estimate_classes']['cjk_bytes'], 0)
        snapshot = meter.calibration_snapshot()
        row = snapshot[next(iter(snapshot))]
        self.assertEqual(row['class_tokens']['actual'], 1000)
        self.assertIn('cjk_bytes', row['bytes_per_actual_token'])
        self.assertIn('不回灌', row['note'])
        # The snapshot is descriptive only; the estimate itself is unchanged.
        again = meter.measure(self.local, body)
        self.assertEqual(again['input']['tokens'], record['input']['tokens'])

    def test_calibration_ignores_missing_or_invalid_usage(self):
        record = meter.measure(self.local, {'model': 'qwen', 'messages': []})
        for usage in (None, {}, {'prompt_tokens': 0}, {'prompt_tokens': True}, {'prompt_tokens': 'x'}):
            self.assertIsNone(meter.record_calibration(self.local, record, usage))

    def test_relay_records_meter_provenance_and_usage_calibration(self):
        received = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                data = json.dumps({'choices': [{'message': {'content': 'OK'}}],
                                   'usage': {'prompt_tokens': 4321, 'completion_tokens': 2,
                                             'total_tokens': 4323,
                                             'prompt_cache_hit_tokens': 0,
                                             'prompt_cache_miss_tokens': 4321}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            model = {'id': 'custom', 'name': 'custom', 'provider': 'custom', 'model': 'm',
                     'api_base': f'http://127.0.0.1:{upstream.server_port}/v1', 'api_key': 'k',
                     'bypass_proxy': True, 'max_input_tokens': 1000000, 'max_output_tokens': 64000,
                     'pricing_cny': {'cache_hit_input': 1, 'cache_miss_input': 2, 'output': 8}}
            run = registry.task_dir('meter-relay')
            registry.patch_task(run, id='meter-relay', model_calls=0, route={'budget_cny': 0},
                                request_output_tokens=777)
            opener = build_opener(ProxyHandler({}))
            with relay.Relay(run, model, self.on) as server:
                body = {'model': 'm', 'messages': [{'role': 'user', 'content': cjk(500)}],
                        'tools': [{'type': 'function', 'function': {'name': 'read'}}]}
                request = Request(server.url + '/chat/completions',
                                  data=json.dumps(body, ensure_ascii=False).encode(),
                                  headers={'Content-Type': 'application/json',
                                           'Authorization': 'Bearer ' + server.token})
                with opener.open(request, timeout=5) as response:
                    response.read()
                self.assertEqual(len(received), 1, 'legal request must reach the upstream')
                # The task-level output target is honoured even without max_tokens.
                self.assertEqual(received[0]['max_tokens'], 777)
            state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
            self.assertEqual(state['request_output_tokens'], 777)
            self.assertEqual(state['meter_source'], meter.SOURCE_BYTE_PROXY)
            self.assertEqual(state['meter_precision'], meter.APPROXIMATE)
            self.assertIsInstance(state['meter_input_tokens'], int)
            self.assertEqual(state['meter_output_target'], 777)
            self.assertEqual(state['meter_output_origin'], 'task_request_output_tokens')
            self.assertEqual(state['meter_guard'], 'proceed')
            self.assertEqual(state['meter_identity']['model'], 'm')
            self.assertEqual(state['meter_record']['output']['capability_ceiling'], 64000)
            self.assertIn('meter_warnings', state)
            self.assertTrue(state['usage_calibration']['actual_prompt_tokens'] == 4321)
            self.assertFalse(state['usage_calibration']['precision'] == meter.EXACT)
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_relay_still_sends_when_metering_itself_fails(self):
        received = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                received.append(1)
                data = b'{"choices":[{"message":{"content":"OK"}}]}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            model = {'id': 'custom', 'name': 'custom', 'provider': 'custom', 'model': 'm',
                     'api_base': f'http://127.0.0.1:{upstream.server_port}/v1', 'api_key': 'k',
                     'bypass_proxy': True, 'max_input_tokens': 1000, 'max_output_tokens': 64,
                     'pricing_cny': {'cache_hit_input': 1, 'cache_miss_input': 2, 'output': 8}}
            run = registry.task_dir('meter-broken')
            registry.patch_task(run, id='meter-broken', model_calls=0, route={'budget_cny': 0})
            opener = build_opener(ProxyHandler({}))
            with patch.object(relay.token_meter, 'measure_for_relay',
                              side_effect=RuntimeError('metering down')):
                with relay.Relay(run, model, self.on) as server:
                    body = {'model': 'm', 'messages': [{'role': 'user', 'content': 'x' * 5000}]}
                    request = Request(server.url + '/chat/completions',
                                      data=json.dumps(body).encode(),
                                      headers={'Content-Type': 'application/json',
                                               'Authorization': 'Bearer ' + server.token})
                    with opener.open(request, timeout=5) as response:
                        response.read()
            self.assertEqual(len(received), 1, 'metering failure must not block the request')
            state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
            self.assertEqual(state['meter_error'], 'RuntimeError')
            self.assertIn('计量不可用', state['meter_guard_note'])
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_relay_applies_verified_exact_over_window_rejection(self):
        run = registry.task_dir('exact-over')
        registry.patch_task(run, id='exact-over', model_calls=0, route={'budget_cny': 0})
        body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 500}
        spec = meter.WindowSpec(kind=meter.WINDOW_COMBINED, input_window=1000, output_cap=1000,
                                source='mock', verified=True)
        evidence, counter = self.verified_engine(self.local, 900)

        def measure_request(model, body_arg, **kwargs):
            return meter.measure(model, body_arg, evidence=evidence, declared_window=spec,
                                 engine_tokenize=counter)

        with patch.object(relay.token_meter, 'measure_for_relay', side_effect=measure_request), \
                patch.object(relay, 'build_opener') as remote:
            with relay.Relay(run, self.local, self.on) as server:
                opener = build_opener(ProxyHandler({}))
                request = Request(server.url + '/chat/completions',
                                  data=json.dumps(body).encode(),
                                  headers={'Content-Type': 'application/json',
                                           'Authorization': 'Bearer ' + server.token})
                with self.assertRaises(HTTPError) as error:
                    opener.open(request, timeout=5)
            self.assertEqual(error.exception.code, 402)
            remote.assert_not_called()
        state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
        self.assertIn('已证实的上下文窗口', state['budget_error'])

    def test_protocol_byte_ceiling_still_applies(self):
        run = registry.task_dir('too-big')
        registry.patch_task(run, id='too-big', model_calls=0, route={'budget_cny': 0})
        with patch.object(relay, 'PROTOCOL_BYTE_CEILING', 2000), \
                patch.object(relay, 'build_opener') as remote:
            with relay.Relay(run, self.local, self.on) as server:
                opener = build_opener(ProxyHandler({}))
                body = {'model': 'qwen',
                        'messages': [{'role': 'user', 'content': 'x' * 3000}]}
                request = Request(server.url + '/chat/completions',
                                  data=json.dumps(body).encode(),
                                  headers={'Content-Type': 'application/json',
                                           'Authorization': 'Bearer ' + server.token})
                with self.assertRaises(HTTPError) as error:
                    opener.open(request, timeout=5)
            self.assertEqual(error.exception.code, 402)
            remote.assert_not_called()
        state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
        self.assertIn('字节数', state['budget_error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
