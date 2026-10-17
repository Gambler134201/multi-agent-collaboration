"""Recovery/reliability acceptance for transient faults (B4/B1/B3/B11/B15/A2).

Every test is offline and deterministic: a temporary registry/run tree, scripted
fake openers and a fake ACP process.  No live model call, no real service restart
and no paid traffic.
"""
import io
import json
import concurrent.futures
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import budget_relay
import dsh_worker
import model_registry as registry
import model_router
import panel_server as panel
import recovery_retry
import stage_store
import task_handover
import task_timing
import thinking_config
import workflow_settings


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------- fakes

class FakeResponse:
    def __init__(self, payload=b'', status=200, content_type='application/json', chunks=None):
        self.status = status
        self._payload = payload
        self._chunks = chunks
        self.headers = {'Content-Type': content_type}

    def read(self):
        return self._payload

    def __iter__(self):
        if self._chunks is not None:
            yield from self._chunks
        else:
            yield self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FailingStreamResponse(FakeResponse):
    def __init__(self, first, error):
        super().__init__(status=200, content_type='text/event-stream')
        self._first = first
        self._error = error

    def __iter__(self):
        yield self._first
        raise self._error


class ScriptedOpener:
    """Deterministic stand-in for urllib's opener; records every attempt."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.timeouts = []

    def open(self, request, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        if not self.script:
            raise AssertionError('unexpected extra upstream attempt: the retry bound was exceeded')
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAcpProcess:
    def __init__(self, stdout_text, returncode=0, exit_after=None):
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO('')
        self.pid = 987654
        self.returncode = returncode
        self.exit_after = exit_after
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.exit_after is not None and self.polls > self.exit_after:
            return 0
        return None if self.exit_after is not None else self.returncode

    def wait(self, timeout=None):
        return self.returncode


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'

    def tearDown(self):
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def seed_home(self, **row):
        registry.write_json(registry.HERE / 'settings.json', {
            'model': 'qwen', 'api_base': row.get('api_base', 'http://127.0.0.1:1234/v1'),
            'api_key_env': '', 'max_input_tokens': 32768, 'max_output_tokens': 4096})
        registry.list_models()
        if row:
            registry.upsert_model({'name': '家里的 Qwen', 'provider': 'local',
                                   'api_base': row.get('api_base', 'http://127.0.0.1:1234/v1'),
                                   'model': row.get('model', 'qwen'), **row}, 'home')

    def task(self, name='t1', **fields):
        run = registry.RUNS / name
        run.mkdir(parents=True, exist_ok=True)
        registry.patch_task(run, id=name, status='running', kind='chat')
        if fields:
            registry.patch_task(run, **fields)
        return run


# ============================================================== c1 attribution

class OutcomeAttributionTests(Sandbox):
    def test_resolved_transient_fault_is_not_an_active_fatal_error(self):
        state = {'gateway_error': '模型接口暂时不可用（HTTP 503）',
                 'fault_events': [
                     {'channel': 'gateway', 'category': 'upstream_unavailable', 'class': 'transient',
                      'seq': 1, 'resolved': True, 'resolved_at': 'x', 'http_status': 503}]}
        self.assertIsNone(recovery_retry.unresolved_fault(state))
        self.assertEqual(recovery_retry.recovery_evidence(state)['resolved'], 1)

    def test_unresolved_transient_fault_is_still_fatal(self):
        state = {'fault_events': [{'channel': 'gateway', 'category': 'timeout', 'class': 'transient',
                                   'seq': 1, 'resolved': False}]}
        fault = recovery_retry.unresolved_fault(state)
        self.assertIsNotNone(fault)
        self.assertIn('等待超时', fault['message'])

    def test_hard_faults_are_never_resolved_by_a_later_success(self):
        for category in ('auth', 'paid', 'protocol', 'identity'):
            events = [recovery_retry.compact_event('budget', category, http=401)]
            resolved, changed = recovery_retry.resolve_transient(events, 'now', 'req-2')
            self.assertEqual(changed, 0, category)
            self.assertFalse(resolved[0]['resolved'], category)
            self.assertIsNotNone(recovery_retry.unresolved_fault({'fault_events': resolved}), category)

    def test_transient_events_are_resolved_by_a_later_proven_request(self):
        events = [recovery_retry.compact_event('gateway', 'connect'), recovery_retry.compact_event('gateway', 'connect')]
        resolved, changed = recovery_retry.resolve_transient(events, 'now', 'req-3')
        self.assertEqual(changed, 2)
        self.assertTrue(all(row['resolved'] and row['recovered_by'] == 'req-3' for row in resolved))
        self.assertIsNone(recovery_retry.unresolved_fault({'fault_events': resolved}))

    def test_legacy_error_without_structured_events_keeps_failing_closed(self):
        fault = recovery_retry.unresolved_fault({'gateway_error': '旧的通用错误'})
        self.assertIsNotNone(fault)
        self.assertTrue(fault['legacy'])
        fault = recovery_retry.unresolved_fault({'budget_error': '旧的预算错误'})
        self.assertIsNotNone(fault)

    def test_compact_events_never_carry_a_secret_or_a_body(self):
        event = recovery_retry.compact_event('gateway', 'auth', http=401,
                                             detail=recovery_retry.category_label('auth'), at='now')
        text = json.dumps(event, ensure_ascii=False)
        for leak in ('sk-', 'Bearer ', 'prompt', 'authorization'):
            self.assertNotIn(leak, text)
        self.assertEqual(event['http_status'], 401)
        self.assertEqual(event['class'], 'hard')

    def test_classification_maps_status_and_transport_without_the_message(self):
        cases = [
            (HTTPError('u', 401, 'secret sk-abc', {}, None), 'auth', 401),
            (HTTPError('u', 402, 'x', {}, None), 'paid', 402),
            (HTTPError('u', 429, 'x', {}, None), 'rate_limit', 429),
            (HTTPError('u', 503, 'x', {}, None), 'upstream_unavailable', 503),
            (HTTPError('u', 400, 'x', {}, None), 'protocol', 400),
            (URLError(ConnectionRefusedError(10061, 'refused')), 'connect', None),
            (TimeoutError('slow'), 'timeout', None),
            (ConnectionResetError(10054, 'reset'), 'reset', None),
        ]
        for exc, category, status in cases:
            info = recovery_retry.classify_exception(exc)
            self.assertEqual(info['category'], category, exc)
            self.assertEqual(info['http_status'], status, exc)


class WorkerOutcomeAttributionTests(Sandbox):
    """The worker must not fail a real completed result over an old transient field."""

    def setUp(self):
        super().setUp()
        self.seed_home()
        self.saved_here = dsh_worker.HERE
        dsh_worker.HERE = registry.HERE

    def tearDown(self):
        dsh_worker.HERE = self.saved_here
        super().tearDown()

    def _run(self, name, events, extra=None, process=None):
        project = registry.HERE / ('project-' + name)
        project.mkdir(exist_ok=True)
        run = registry.RUNS / name
        run.mkdir(parents=True, exist_ok=True)
        (run / 'prompt.txt').write_text('只读任务：验证结果归因', encoding='utf-8')
        model = registry.get_model('home')
        state = {'id': name, 'kind': 'chat', 'project': str(project), 'files': [],
                 'timeout': 30, 'model_id': model['id']}
        registry.patch_task(run, id=name, kind='chat', status='running',
                            model_calls=0, route={'budget_cny': 0})
        if extra:
            registry.patch_task(run, **extra)
        relay = SimpleNamespace(url='http://127.0.0.1:9/v1', token='relay-token')
        preset = {'id': 'standard', 'revision': 'r', 'source': 's', 'overrides': [],
                  'policy_overrides': [], 'declaration': {'id': 'agent-preset', 'config': {'plugins': []}}}
        process = process or FakeAcpProcess(events)
        with patch.object(dsh_worker, 'availability', return_value={'ready': True}), \
             patch.object(dsh_worker.preset_runtime, 'load', return_value=preset), \
             patch.object(dsh_worker.preset_runtime, 'verify', return_value={}), \
             patch.object(dsh_worker.record_protection, 'verify_proof', return_value={'matched': 'parent', 'offline_fixture': True}), \
             patch.object(dsh_worker.subprocess, 'Popen', return_value=process), \
             patch('sys.stdout', new=io.StringIO()):
            code = dsh_worker._run_task(run, state, model, relay)
        return code, registry.read_task(run)

    COMPLETED = '\n'.join(json.dumps(item, ensure_ascii=False) for item in [
        {'type': 'session', 'session_id': 'sess-1'},
        {'type': 'text_delta', 'text': '已完成'},
        {'type': 'result', 'result': {'status': 'completed', 'stopReason': 'end_turn'}}]) + '\n'

    def test_resolved_transient_field_no_longer_poisons_a_completed_result(self):
        resolved = {'channel': 'gateway', 'category': 'upstream_unavailable', 'class': 'transient',
                    'seq': 1, 'resolved': True, 'resolved_at': 'x', 'http_status': 503}
        code, final = self._run('resolved-ok', self.COMPLETED,
                                extra={'gateway_error': '模型接口暂时不可用（HTTP 503）',
                                       'fault_events': [resolved], 'fault_status': 'recovered'})
        self.assertEqual(code, 0)
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(final['fault_status'], 'recovered')
        # Historical diagnostics are preserved, not erased.
        self.assertEqual(final['fault_events'][0]['category'], 'upstream_unavailable')
        self.assertNotIn('error', final)

    def test_unresolved_hard_fault_still_fails_the_outcome(self):
        hard = {'channel': 'budget', 'category': 'auth', 'class': 'hard', 'seq': 1,
                'resolved': False, 'http_status': 401}
        code, final = self._run('hard-fail', self.COMPLETED,
                                extra={'budget_error': '模型接口认证被拒绝（HTTP 401）',
                                       'fault_events': [hard]})
        self.assertEqual(code, 1)
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['fault_status'], 'unresolved')
        self.assertIn('认证被拒绝', final['error'])
        self.assertEqual(recovery_retry.fault_summary(final['fault_events']), '模型接口认证被拒绝（HTTP 401）')

    def test_mixed_history_with_one_unresolved_entry_still_fails(self):
        events = [{'channel': 'gateway', 'category': 'connect', 'class': 'transient', 'seq': 1, 'resolved': True},
                  {'channel': 'gateway', 'category': 'protocol', 'class': 'hard', 'seq': 2, 'resolved': False}]
        code, final = self._run('mixed-fail', self.COMPLETED,
                                extra={'gateway_error': '模型接口协议错误（HTTP 400）', 'fault_events': events})
        self.assertEqual(code, 1)
        self.assertEqual(final['status'], 'failed')
        self.assertIn('协议错误', final['error'])

    def test_legacy_record_without_events_still_fails(self):
        code, final = self._run('legacy-fail', self.COMPLETED,
                                extra={'gateway_error': '模型接口请求失败，请检查连接'})
        self.assertEqual(code, 1)
        self.assertEqual(final['status'], 'failed')
        self.assertIn('模型接口请求失败', final['error'])

    def test_a_recovered_connection_fault_keeps_the_real_outcome_and_evidence(self):
        events = [{'channel': 'gateway', 'category': 'connect', 'class': 'transient', 'seq': 1,
                   'resolved': True, 'resolved_at': 'x', 'recovered_by': 'req-9'}]
        code, final = self._run('recovered-evidence', self.COMPLETED, extra={'fault_events': events})
        self.assertEqual(code, 0)
        self.assertEqual(final['fault_status'], 'recovered')
        self.assertEqual(final['fault_evidence']['resolved'], 1)
        self.assertEqual(final['fault_evidence']['unresolved'], 0)


# =============================================================== c2 thinking probe

class ThinkingProbeTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.seed_home()

    def verified(self, **overrides):
        info = {'adapter': 'strata', 'supported': ['default', 'off', 'low', 'medium', 'xhigh'],
                'default_effort': 'xhigh', 'default_enabled': True, 'status': 'verified',
                'api_base': 'http://127.0.0.1:1234/v1', 'model': 'qwen',
                'checked_at': registry.now(), 'source': 'http://127.0.0.1:1234/props + /settings'}
        info.update(overrides)
        return info

    def home(self, **overrides):
        return {'id': 'home', 'name': '家里的 Qwen', 'provider': 'local',
                'api_base': 'http://127.0.0.1:1234/v1', 'model': 'qwen', 'enabled': True,
                'thinking_level': 'default', 'thinking_info': self.verified(), **overrides}

    def test_probe_defaults_are_two_attempts_with_a_thirty_second_read_timeout(self):
        self.assertEqual(thinking_config.PROBE_ATTEMPTS, 2)
        self.assertEqual(thinking_config.PROBE_READ_TIMEOUT, 30)
        self.assertEqual(thinking_config.PROBE_RETRY_DELAY, 1.5)

    def test_probe_retries_once_and_passes_the_thirty_second_timeout(self):
        opener = ScriptedOpener([FakeResponse(), FakeResponse()])
        calls = {'n': 0}

        def once(model, timeout=30):
            calls['n'] += 1
            self.assertEqual(timeout, 30)
            if calls['n'] == 1:
                raise URLError(ConnectionRefusedError(10061, 'refused'))
            return self.verified()

        with patch.object(thinking_config, 'build_opener', return_value=opener), \
             patch.object(thinking_config, '_probe_once', side_effect=once):
            info = thinking_config.probe(self.home(), sleep=lambda _: None)
        self.assertEqual(calls['n'], 2)
        self.assertEqual(info['status'], 'verified')

    def test_exact_model_mismatch_is_fatal_and_never_retried(self):
        calls = {'n': 0}

        def once(model, timeout=30):
            calls['n'] += 1
            raise thinking_config.ThinkingMismatch('引擎当前加载的模型与此卡片不一致')

        with patch.object(thinking_config, '_probe_once', side_effect=once):
            with self.assertRaises(thinking_config.ThinkingMismatch):
                thinking_config.probe(self.home(), sleep=lambda _: None)
        self.assertEqual(calls['n'], 1)

    def test_exhausted_probe_reports_the_last_failure(self):
        opener = ScriptedOpener([FakeResponse(), FakeResponse()])

        def once(model, timeout=30):
            raise URLError(ConnectionRefusedError(10061, 'refused'))

        with patch.object(thinking_config, 'build_opener', return_value=opener), \
             patch.object(thinking_config, '_probe_once', side_effect=once):
            with self.assertRaises(URLError):
                thinking_config.probe(self.home(), sleep=lambda _: None)

    def test_cancellation_stops_the_delay_and_the_attempt(self):
        started = time.monotonic()
        self.assertFalse(thinking_config._interruptible_delay(5.0, cancel=lambda: True))
        self.assertLess(time.monotonic() - started, 0.5)
        with self.assertRaises(thinking_config.ThinkingCancelled):
            thinking_config.probe(self.home(), cancel=lambda: True)

    def test_cancellation_during_the_retry_delay_is_respected(self):
        def once(model, timeout=30):
            raise URLError(ConnectionRefusedError(10061, 'refused'))

        with patch.object(thinking_config, '_probe_once', side_effect=once):
            with self.assertRaises(thinking_config.ThinkingCancelled):
                thinking_config.probe(self.home(), delay=5.0, cancel=lambda: True, sleep=lambda _: None)

    def test_default_level_degrades_without_injecting_unverified_controls(self):
        def once(model, timeout=30):
            raise TimeoutError('metadata timeout')

        model = self.home(thinking_info=None)
        with patch.object(thinking_config, '_probe_once', side_effect=once):
            info, evidence = thinking_config.prepare_for_task(model, sleep=lambda _: None)
        self.assertIn(evidence['status'], {'degraded', 'unknown'})
        self.assertFalse(evidence['verified_for_task'])
        self.assertEqual(evidence['category'], 'metadata_unavailable')
        self.assertEqual(info['status'], 'degraded')
        self.assertIn('未核实', info['label'])
        # No thinking control is injected from unverified metadata.
        body = {}
        thinking_config.apply(body, {**model, 'thinking_info': info})
        self.assertEqual(body, {})
        # The registry was not mutated: the user preset stays immutable and no
        # failed probe is ever stored as verified metadata.
        row = registry.get_model('home')
        self.assertEqual(row.get('thinking_level', 'default'), 'default')
        self.assertIsNone(row.get('thinking_info'))

    def test_degraded_metadata_is_never_labelled_as_freshly_verified(self):
        model = self.home(thinking_info=None)
        with patch.object(thinking_config, '_probe_once',
                          side_effect=URLError(ConnectionRefusedError(10061, 'refused'))):
            info, evidence = thinking_config.prepare_for_task(model, sleep=lambda _: None)
        self.assertNotEqual(info['status'], 'verified')
        self.assertNotIn('已读取', json.dumps(evidence, ensure_ascii=False))
        self.assertFalse(evidence['verified_for_task'])

    def test_a_successful_probe_refreshes_only_the_engine_metadata_cache(self):
        model = self.home(thinking_info=None)
        fresh = self.verified()
        with patch.object(thinking_config, 'probe', return_value=fresh):
            info, evidence = thinking_config.prepare_for_task(model)
        self.assertEqual(evidence['status'], 'verified')
        self.assertTrue(evidence['verified_for_task'])
        self.assertEqual(info['default_effort'], 'xhigh')
        row = registry.get_model('home')
        self.assertEqual(row['thinking_info']['status'], 'verified')
        self.assertEqual(row.get('thinking_level', 'default'), 'default')

    def test_explicit_level_uses_a_fresh_same_identity_cache(self):
        cached = self.verified(checked_at=iso(datetime.now(timezone.utc) - timedelta(minutes=5)))
        model = self.home(thinking_level='medium', thinking_info=cached)
        info, evidence = thinking_config.prepare_for_task(model)
        self.assertEqual(evidence['status'], 'cached')
        self.assertFalse(evidence['verified_for_task'])
        self.assertLessEqual(evidence['cache_age_seconds'], 3600)
        self.assertIn('medium', info['supported'])
        body = {}
        thinking_config.apply(body, {**model, 'thinking_info': info})
        self.assertEqual(body['reasoning_effort'], 'medium')

    def test_explicit_level_refuses_an_expired_cache(self):
        stale = self.verified(checked_at=iso(datetime.now(timezone.utc) - timedelta(hours=2)))
        model = self.home(thinking_level='medium', thinking_info=stale)
        with self.assertRaisesRegex(thinking_config.ThinkingUnavailable, '缓存已超过'):
            thinking_config.prepare_for_task(model)

    def test_explicit_level_refuses_mismatched_and_unverified_metadata(self):
        wrong_identity = self.verified(api_base='http://127.0.0.1:9999/v1')
        with self.assertRaises(thinking_config.ThinkingUnavailable):
            thinking_config.prepare_for_task(self.home(thinking_level='medium', thinking_info=wrong_identity))
        unverified = self.verified(status='unknown')
        with self.assertRaises(thinking_config.ThinkingUnavailable):
            thinking_config.prepare_for_task(self.home(thinking_level='medium', thinking_info=unverified))

    def test_explicit_level_with_a_failed_probe_and_no_cache_is_refused_before_inference(self):
        model = self.home(thinking_level='low', thinking_info=None)
        with patch.object(thinking_config, '_probe_once',
                          side_effect=TimeoutError('metadata timeout')):
            with self.assertRaisesRegex(thinking_config.ThinkingUnavailable, '未发送任何推理请求'):
                thinking_config.prepare_for_task(model, sleep=lambda _: None)

    def test_official_deepseek_needs_no_probe(self):
        info, evidence = thinking_config.prepare_for_task(
            {'provider': 'deepseek', 'api_base': 'https://api.deepseek.com', 'model': 'deepseek-flash',
             'thinking_level': 'default'})
        self.assertEqual(evidence['status'], 'documented')
        self.assertFalse(evidence['verified_for_task'])
        self.assertEqual(info['adapter'], 'deepseek')

    def test_profile_labels_aged_verified_metadata_as_stale(self):
        stale = self.verified(checked_at=iso(datetime.now(timezone.utc) - timedelta(hours=3)))
        info = thinking_config.profile(self.home(thinking_info=stale))
        self.assertTrue(info['stale'])
        self.assertIn('缓存已过期', info['label'])
        # Display only: validation for the already-recorded preset is unchanged.
        self.assertEqual(thinking_config.validate(self.home(thinking_info=stale)), 'default')


# ================================================================= c3 relay retry

class RelayRetryTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.model = {'id': 'home', 'name': '家里的 Qwen', 'provider': 'local',
                      'api_base': 'http://127.0.0.1:1/v1', 'model': 'qwen', 'api_key': '',
                      'bypass_proxy': True, 'thinking_level': 'default',
                      'max_input_tokens': 32768, 'max_output_tokens': 4096, 'thinking_info': None}
        self.run = self.task('relay', status='running', model_calls=0, route={'budget_cny': 0},
                             cost_requests={}, kind='chat')
        self.body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'Hi'}], 'max_tokens': 32}

    def call(self, opener):
        with patch.object(budget_relay, 'build_opener', return_value=opener):
            with budget_relay.Relay(self.run, self.model, workflow_settings.DEFAULTS) as relay:
                request = Request(relay.url + '/chat/completions', data=json.dumps(self.body).encode(),
                                  headers={'Authorization': 'Bearer ' + relay.token})
                client = build_opener(ProxyHandler({}))
                try:
                    with client.open(request, timeout=20) as response:
                        return response.status, response.read()
                except HTTPError as error:
                    return error.code, error.read()

    def state(self):
        return registry.read_task(self.run)

    def test_socket_allowance_is_300_and_the_task_default_stays_unlimited(self):
        self.assertEqual(budget_relay.UPSTREAM_SOCKET_TIMEOUT, 300)
        self.assertEqual(task_timing.normalize(0), 0)
        self.assertIsNone(task_timing.deadline(0))
        payload = b'{"choices":[{"message":{"content":"OK"}}]}'
        opener = ScriptedOpener([FakeResponse(payload)])
        status, body = self.call(opener)
        self.assertEqual(status, 200)
        self.assertEqual(opener.timeouts, [300])

    def test_one_safe_transport_retry_recovers_a_connect_phase_failure(self):
        payload = b'{"choices":[{"message":{"content":"OK"}}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}'
        opener = ScriptedOpener([URLError(ConnectionRefusedError(10061, 'refused')), FakeResponse(payload)])
        with patch.object(recovery_retry, 'DEFAULT_RETRY_DELAY_SECONDS', 0.0):
            status, body = self.call(opener)
        self.assertEqual(status, 200)
        self.assertIn(b'OK', body)
        self.assertEqual(opener.calls, 2)
        state = self.state()
        self.assertEqual(state['transport_retries'], 1)
        self.assertEqual(state['fault_events'][0]['category'], 'connect')
        self.assertTrue(state['fault_events'][0]['resolved'])
        self.assertEqual(state['fault_status'], 'recovered')
        # One logical request: one reservation, one ledger receipt, truthful calls.
        self.assertEqual(state['model_calls'], 1)
        self.assertEqual(len(state['cost_requests']), 1)
        self.assertIn('无法连接', state['gateway_error'])

    def test_an_exhausted_transient_retry_is_a_permanent_failure(self):
        opener = ScriptedOpener([URLError(ConnectionRefusedError(10061, 'refused')),
                                 URLError(ConnectionRefusedError(10061, 'refused'))])
        with patch.object(recovery_retry, 'DEFAULT_RETRY_DELAY_SECONDS', 0.0):
            status, body = self.call(opener)
        self.assertEqual(status, 502)
        self.assertNotIn(b'qwen', body)
        self.assertEqual(opener.calls, 2)
        state = self.state()
        self.assertEqual(state['transport_retries'], 1)
        self.assertEqual([row['category'] for row in state['fault_events']], ['connect', 'connect'])
        self.assertTrue(all(not row['resolved'] for row in state['fault_events']))
        self.assertIsNotNone(recovery_retry.unresolved_fault(state))

    def test_an_ambiguous_timeout_is_never_replayed(self):
        opener = ScriptedOpener([TimeoutError('socket timed out')])
        status, body = self.call(opener)
        self.assertEqual(status, 502)
        self.assertEqual(opener.calls, 1)
        state = self.state()
        self.assertNotIn('transport_retries', state)
        self.assertEqual(state['fault_events'][0]['category'], 'timeout')
        self.assertIn('不自动重试', state['fault_events'][0]['detail'])

    def test_partial_stream_content_is_never_replayed(self):
        first = b'data: {"choices":[{"delta":{"content":"A"}}]}\n'
        opener = ScriptedOpener([FailingStreamResponse(first, ConnectionResetError(10054, 'reset'))])
        with patch.object(recovery_retry, 'DEFAULT_RETRY_DELAY_SECONDS', 0.0):
            try:
                status, body = self.call(opener)
            except Exception:
                status, body = 200, first
        self.assertEqual(opener.calls, 1)
        state = self.state()
        self.assertNotIn('transport_retries', state)
        self.assertEqual(state['fault_events'][0]['category'], 'reset')
        self.assertTrue(any('不重放' in row.get('detail', '') for row in state['fault_events']))
        self.assertEqual(state['fault_events'][0]['class'], 'transient')

    def test_a_429_with_a_bounded_retry_after_is_honoured(self):
        error = HTTPError('http://127.0.0.1:1/v1/chat/completions', 429, 'slow down',
                          {'Retry-After': '0'}, None)
        payload = b'{"choices":[{"message":{"content":"OK"}}]}'
        opener = ScriptedOpener([error, FakeResponse(payload)])
        status, body = self.call(opener)
        self.assertEqual(status, 200)
        self.assertEqual(opener.calls, 2)
        state = self.state()
        self.assertEqual(state['transport_retry_delay'], 0.0)
        self.assertEqual(state['fault_events'][0]['http_status'], 429)

    def test_a_429_with_an_unbounded_retry_after_is_not_retried(self):
        error = HTTPError('http://127.0.0.1:1/v1/chat/completions', 429, 'later',
                          {'Retry-After': '600'}, None)
        opener = ScriptedOpener([error])
        status, body = self.call(opener)
        self.assertEqual(status, 502)
        self.assertEqual(opener.calls, 1)
        state = self.state()
        self.assertNotIn('transport_retries', state)
        self.assertIn('Retry-After', state['fault_events'][0]['detail'])

    def test_a_protocol_error_is_recorded_with_its_status_and_not_retried(self):
        error = HTTPError('http://127.0.0.1:1/v1/chat/completions', 400, 'bad request', {}, None)
        opener = ScriptedOpener([error])
        status, body = self.call(opener)
        self.assertEqual(status, 502)
        self.assertEqual(opener.calls, 1)
        state = self.state()
        self.assertEqual(state['fault_events'][0]['category'], 'protocol')
        self.assertEqual(state['fault_events'][0]['http_status'], 400)
        self.assertEqual(state['fault_events'][0]['class'], 'hard')

    def test_cancellation_stops_the_backoff_immediately(self):
        (self.run / 'cancelled.flag').touch()
        with budget_relay.Relay(self.run, self.model, workflow_settings.DEFAULTS) as relay:
            relay._local.transport_retry_used = False
            started = time.monotonic()
            self.assertFalse(relay._wait_before_retry(5.0))
            self.assertLess(time.monotonic() - started, 0.5)
            decision = relay.transport_fault(URLError(ConnectionRefusedError(10061, 'refused')),
                                             started=False, request_id='req-1')
        self.assertFalse(decision['retry'])
        self.assertEqual(decision['reason'], 'cancelled')

    def test_a_retry_is_never_sent_after_a_response_was_delivered(self):
        decision = recovery_retry.transport_retry_allowed(
            category='connect', response_started=True, retry_used=False)
        self.assertFalse(decision['retry'])
        self.assertEqual(decision['reason'], 'response_delivered')

    def test_only_one_retry_is_allowed_per_logical_request(self):
        decision = recovery_retry.transport_retry_allowed(
            category='upstream_unavailable', http_status=503, retry_used=True)
        self.assertFalse(decision['retry'])
        self.assertEqual(decision['reason'], 'retry_used')

    def test_client_side_broken_pipe_is_never_a_provider_fault_or_a_retry(self):
        opener = ScriptedOpener([BrokenPipeError(32, 'broken pipe')])
        try:
            self.call(opener)
        except Exception:
            pass
        self.assertEqual(opener.calls, 1)
        state = self.state()
        self.assertNotIn('fault_events', state)


# ================================================================= c4 exit grace

class ExitGraceTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.seed_home()
        self.saved_here = dsh_worker.HERE
        dsh_worker.HERE = registry.HERE

    def tearDown(self):
        dsh_worker.HERE = self.saved_here
        super().tearDown()

    def _run(self, name, process):
        project = registry.HERE / ('project-' + name)
        project.mkdir(exist_ok=True)
        run = registry.RUNS / name
        run.mkdir(parents=True, exist_ok=True)
        (run / 'prompt.txt').write_text('只读任务：退出宽限', encoding='utf-8')
        model = registry.get_model('home')
        state = {'id': name, 'kind': 'chat', 'project': str(project), 'files': [],
                 'timeout': 0, 'model_id': model['id']}
        registry.patch_task(run, id=name, kind='chat', status='running', model_calls=0,
                            route={'budget_cny': 0})
        relay = SimpleNamespace(url='http://127.0.0.1:9/v1', token='relay-token')
        preset = {'id': 'standard', 'revision': 'r', 'source': 's', 'overrides': [],
                  'policy_overrides': [], 'declaration': {'id': 'agent-preset', 'config': {'plugins': []}}}
        killed = []
        with patch.object(dsh_worker, 'availability', return_value={'ready': True}), \
             patch.object(dsh_worker.preset_runtime, 'load', return_value=preset), \
             patch.object(dsh_worker.preset_runtime, 'verify', return_value={}), \
             patch.object(dsh_worker.record_protection, 'verify_proof', return_value={'matched': 'parent', 'offline_fixture': True}), \
             patch.object(dsh_worker.subprocess, 'Popen', return_value=process), \
             patch.object(dsh_worker.subprocess, 'run',
                          side_effect=lambda *a, **k: killed.append(a)), \
             patch('sys.stdout', new=io.StringIO()):
            code = dsh_worker._run_task(run, state, model, relay)
        return code, registry.read_task(run), killed

    def test_the_grace_is_thirty_seconds_and_unlimited_runtime_is_preserved(self):
        self.assertEqual(dsh_worker.EXIT_GRACE_SECONDS, 30)
        self.assertIsNone(task_timing.deadline(0))

    def test_a_slow_teardown_keeps_the_validated_completed_outcome(self):
        events = '\n'.join(json.dumps(item, ensure_ascii=False) for item in [
            {'type': 'session', 'session_id': 's1'},
            {'type': 'text_delta', 'text': '已完成'},
            {'type': 'result', 'result': {'status': 'completed', 'stopReason': 'end_turn'}}]) + '\n'
        process = FakeAcpProcess(events, exit_after=10 ** 9)
        with patch.object(dsh_worker, 'EXIT_GRACE_SECONDS', 0.05):
            code, final, killed = self._run('slow-exit', process)
        self.assertEqual(code, 0)
        self.assertEqual(final['status'], 'completed')
        self.assertTrue(final['exit_grace_exhausted'])
        self.assertEqual(final['exit_grace_seconds'], 0.05)
        self.assertTrue(killed, 'the leftover process must still be cleaned up')

    def test_a_real_executor_failure_is_still_recorded_as_failed(self):
        process = FakeAcpProcess(json.dumps({'type': 'error', 'text': '子进程崩溃'}) + '\n', returncode=1)
        code, final, _ = self._run('real-fail', process)
        self.assertEqual(code, 1)
        self.assertEqual(final['status'], 'failed')
        self.assertFalse(final.get('exit_grace_exhausted', False))
        self.assertIn('error', final)


# ============================================================= c5 restart recovery

class RestartRecoveryTests(Sandbox):
    def test_queued_task_becomes_recovery_pending_not_failed(self):
        run = self.task('queued-task', status='queued', source='panel', kind='chat')
        actions = panel.recover_pending_tasks(entries=[{'run': run}])
        state = registry.read_task(run)
        self.assertEqual(state['status'], 'recovery_pending')
        self.assertEqual(state['recovery']['kind'], 'queued_not_started')
        self.assertFalse(state['recovery']['auto_resend'])
        self.assertNotIn('error', state)
        self.assertIn('未自动重发', state['phase'])
        self.assertEqual([row['task_id'] for row in actions], ['queued-task'])

    def test_running_task_with_a_vanished_worker_becomes_recovery_pending(self):
        run = self.task('running-task', status='running', source='panel', worker_pid=999999999,
                        started_at=registry.now())
        panel.recover_pending_tasks(entries=[{'run': run}])
        state = registry.read_task(run)
        self.assertEqual(state['status'], 'recovery_pending')
        self.assertEqual(state['recovery']['kind'], 'worker_gone')

    def test_running_task_with_a_live_worker_is_left_untouched(self):
        run = self.task('live-task', status='running', source='panel', worker_pid=1234)
        with patch.object(panel, 'recorded_worker_alive', return_value=True):
            actions = panel.recover_pending_tasks(entries=[{'run': run}])
        self.assertEqual(actions, [])
        self.assertEqual(registry.read_task(run)['status'], 'running')

    def test_completed_tasks_are_never_touched(self):
        run = self.task('done-task', status='completed', source='panel')
        panel.recover_pending_tasks(entries=[{'run': run}])
        self.assertEqual(registry.read_task(run)['status'], 'completed')

    def test_pending_recovery_is_inspectable_and_read_only(self):
        run = self.task('inspect-task', status='queued', source='panel', kind='chat',
                        title='看看这个', stage_id=None)
        panel.recover_pending_tasks(entries=[{'run': run}])
        with patch.object(panel.project_storage, 'iter_task_dirs', return_value=[{'run': run}]):
            rows = panel.pending_task_recovery(None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['task_id'], 'inspect-task')
        self.assertFalse(rows[0]['creates_new_record'])
        self.assertIn('/recover', rows[0]['resume'])
        # Listing must not have changed the record.
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')

    def test_explicit_recovery_requires_confirmation_and_a_dead_worker(self):
        run = self.task('recover-task', status='recovery_pending', source='panel', kind='chat',
                        worker_pid=999999999)
        with self.assertRaisesRegex(ValueError, 'confirm=true'):
            panel.recover_task('recover-task', {})
        with patch.object(panel, 'recorded_worker_alive', return_value=True):
            with self.assertRaisesRegex(ValueError, '仍在运行'):
                panel.recover_task('recover-task', {'confirm': True})
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')

    def test_explicit_recovery_resumes_the_same_record_without_creating_another(self):
        run = self.task('resume-task', status='recovery_pending', source='panel', kind='chat',
                        worker_pid=None, project='', dsh_session_key='desk-resume-task')
        submitted = []
        fake = SimpleNamespace(submit=lambda fn, task_id: submitted.append((fn.__name__, task_id)))
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'EXECUTOR', fake):
            result = panel.recover_task('resume-task', {'confirm': True})
        self.assertTrue(result['ok'])
        self.assertFalse(result['creates_new_record'])
        self.assertEqual(submitted, [('execute_task', 'resume-task')])
        state = registry.read_task(run)
        self.assertEqual(state['status'], 'queued')
        self.assertEqual(state['id'], 'resume-task')
        self.assertEqual(state['dsh_session_key'], 'desk-resume-task')
        self.assertTrue(state['recovery']['confirmed'])
        # A repeated recovery action must not enqueue a duplicate.
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'EXECUTOR', fake):
            with self.assertRaisesRegex(ValueError, '只有待恢复'):
                panel.recover_task('resume-task', {'confirm': True})
        self.assertEqual(len(submitted), 1)

    def test_recovery_of_a_task_that_is_already_active_is_refused(self):
        run = self.task('active-task', status='recovery_pending', source='panel', kind='chat')
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'ACTIVE', {'active-task': object()}):
            with self.assertRaisesRegex(ValueError, '已经在执行队列'):
                panel.recover_task('active-task', {'confirm': True})
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')

    def test_stage_linked_recovery_reuses_the_supported_stage_requeue(self):
        goal = stage_store.create_goal('恢复目标', project='')
        stage = stage_store.add_stage(goal['id'], '阶段一', objective='恢复测试',
                                      acceptance=['c1'], actor='main')
        run = self.task('stage-task', status='running', source='panel', kind='edit',
                        stage_id=stage['id'], goal_id=goal['id'])
        stage_store.claim_dispatch(stage['id'], 'stage-task')
        panel.recover_pending_tasks(entries=[{'run': run}])
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'EXECUTOR', SimpleNamespace(submit=lambda *a: None)):
            panel.recover_task('stage-task', {'confirm': True})
        self.assertEqual(registry.read_task(run)['status'], 'queued')
        after = stage_store.get_stage(stage['id'])
        # The stage is atomically claimed for the recovered task (no ready-stage
        # window in which another job could be admitted alongside it).
        self.assertEqual(after['status'], stage_store.DISPATCHED)
        self.assertEqual(after['active_task_id'], 'stage-task')
        # The original acceptance conditions, candidates and task lineage survive.
        self.assertEqual(after['acceptance'], ['c1'])
        self.assertEqual(after['task_ids'], ['stage-task'])

    def test_stage_reconciliation_maps_a_recovery_pending_task_to_an_active_stage(self):
        goal = stage_store.create_goal('同步目标', project='')
        stage = stage_store.add_stage(goal['id'], '阶段一', objective='x', acceptance=['c1'], actor='main')
        run = self.task('sync-task', status='running', source='panel', kind='edit',
                        stage_id=stage['id'], goal_id=goal['id'])
        stage_store.claim_dispatch(stage['id'], 'sync-task')
        panel.recover_pending_tasks(entries=[{'run': run}])
        stage_store.recover()
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.RECOVERY_PENDING)
        self.assertEqual([row['stage_id'] for row in stage_store.pending_recovery()], [stage['id']])
        # Reconciliation must never treat pending recovery as terminal.
        self.assertNotIn(stage_store.get_stage(stage['id'])['status'], stage_store.ACCEPTED_STATES)

    def test_a_durably_cancelled_task_can_never_be_recovered(self):
        run = self.task('cancelled-by-flag', status='recovery_pending', source='panel', kind='chat')
        (run / 'cancelled.flag').touch()
        submitted = []
        fake = SimpleNamespace(submit=lambda fn, task_id: submitted.append(task_id))
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'EXECUTOR', fake):
            with self.assertRaisesRegex(ValueError, '已取消'):
                panel.recover_task('cancelled-by-flag', {'confirm': True})
        self.assertEqual(submitted, [])
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')

    def test_restart_reconciliation_never_revives_a_cancelled_task(self):
        run = self.task('cancelled-queued', status='queued', source='panel', kind='chat',
                        recovery={'kind': 'x'})
        (run / 'cancelled.flag').touch()
        actions = panel.recover_pending_tasks(entries=[{'run': run}])
        self.assertEqual(actions, [])
        state = registry.read_task(run)
        self.assertEqual(state['status'], 'queued')
        self.assertEqual(state['recovery'], {'kind': 'x'})
        self.assertNotEqual(state['recovery'].get('kind'), 'worker_gone')

    def test_cancelled_stage_and_acceptance_guards_stay_authoritative(self):
        goal = stage_store.create_goal('取消目标', project='')
        stage = stage_store.add_stage(goal['id'], '阶段一', objective='x', acceptance=['c1'], actor='main')
        run = self.task('cancelled-task', status='running', source='panel', kind='edit',
                        stage_id=stage['id'], goal_id=goal['id'])
        stage_store.claim_dispatch(stage['id'], 'cancelled-task')
        stage_store.cancel_stage(stage['id'], '主脑取消', actor='main')
        panel.recover_pending_tasks(entries=[{'run': run}])
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'EXECUTOR', SimpleNamespace(submit=lambda *a: None)):
            with self.assertRaises(ValueError):
                panel.recover_task('cancelled-task', {'confirm': True})
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.CANCELLED)


# =========================================================== c6 stale connectivity

class StaleConnectivityTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.old = iso(datetime.now(timezone.utc) - timedelta(hours=3))
        self.local = {'id': 'local', 'name': 'local', 'provider': 'local', 'model': 'qwen',
                      'api_base': 'http://127.0.0.1:1234/v1', 'enabled': True,
                      'max_input_tokens': 262144, 'max_output_tokens': 8192,
                      'preferred_tasks': ['单文件修改'], 'max_task_complexity': 'simple',
                      'cost_preference': 'economy',
                      'last_check': {'ok': False, 'kind': 'generation', 'at': self.old,
                                     'message': '生成接口连接失败'}}
        self.body = {'kind': 'edit', 'engine': 'dsh', 'route_mode': 'auto', 'prompt': 'task',
                     'files': ['a.py'],
                     'routing': {'category': 'code_small', 'complexity': 'simple',
                                 'input_tokens': 8192, 'output_tokens': 4096, 'expected_calls': 3}}
        self.on = {**workflow_settings.DEFAULTS, 'enabled': True}

    def test_an_old_failed_check_no_longer_excludes_the_model(self):
        verdict = model_router.connectivity_verdict(self.local)
        self.assertFalse(verdict['blocked'])
        self.assertTrue(verdict['stale'])
        self.assertIn('可达性', verdict['reason'])
        route = model_router.choose(self.body, [self.local], self.on)
        self.assertEqual(route['model_id'], 'local')
        self.assertTrue(any('可达性' in warning for warning in route['warnings']))

    def test_a_fresh_failed_generation_check_still_blocks_automatic_routing(self):
        fresh = {**self.local, 'last_check': {'ok': False, 'kind': 'generation', 'at': registry.now()}}
        verdict = model_router.connectivity_verdict(fresh)
        self.assertTrue(verdict['blocked'])
        with self.assertRaisesRegex(ValueError, '连接测试'):
            model_router.choose(self.body, [fresh], self.on)

    def test_an_undated_legacy_failure_keeps_the_conservative_block(self):
        legacy = {**self.local, 'last_check': {'ok': False}}
        self.assertTrue(model_router.connectivity_verdict(legacy)['blocked'])

    def test_a_failed_reachability_check_is_never_a_capability_verdict(self):
        reach = {**self.local, 'last_check': {'ok': False, 'kind': 'reachability', 'at': registry.now()}}
        verdict = model_router.connectivity_verdict(reach)
        self.assertFalse(verdict['blocked'])
        self.assertEqual(verdict['kind'], 'reachability')

    def test_router_verdict_is_pure_and_needs_no_network(self):
        class NoClock:
            fromisoformat = staticmethod(datetime.fromisoformat)

            @staticmethod
            def now(*a, **k):
                raise AssertionError('the pure router verdict must not read the clock on its own')

        with patch.object(model_router, 'datetime', NoClock):
            verdict = model_router.connectivity_verdict(
                self.local, moment=datetime.now(timezone.utc))
        self.assertFalse(verdict['blocked'])
        self.assertTrue(verdict['stale'])

    def test_reachability_check_is_labelled_read_only_and_records_no_body(self):
        model = {**self.local, 'api_key': 'sk-secret', 'bypass_proxy': True}
        error = HTTPError('http://127.0.0.1:1234/v1/models', 500, 'sk-secret body', {}, None)

        def boom(*a, **k):
            raise error

        with patch.object(panel, 'remote_request', side_effect=boom):
            check = panel.reachability_check(model)
        self.assertFalse(check['ok'])
        self.assertEqual(check['kind'], 'reachability')
        self.assertFalse(check['inference'])
        self.assertEqual(check['http_status'], 500)
        self.assertNotIn('sk-secret', json.dumps(check, ensure_ascii=False))

    def test_the_pre_dispatch_gate_refuses_a_dead_loopback_engine_with_a_diagnosis(self):
        model = {**self.local, 'id': 'local'}
        registry.write_json(registry.REGISTRY, {'default_model': 'local', 'models': [model]})
        with patch.object(panel, 'reachability_check',
                          return_value={'ok': False, 'kind': 'reachability', 'inference': False,
                                        'category': 'connect', 'http_status': None,
                                        'message': '只读可达性检查未通过：无法连接模型接口。'}):
            gate = panel.connectivity_gate(model)
        self.assertTrue(gate['blocked'])
        self.assertIn('只读可达性检查未通过', gate['message'])
        self.assertIn('请确认模型引擎已启动', gate['message'])

    def test_the_pre_dispatch_gate_skips_healthy_models_without_extra_traffic(self):
        healthy = {**self.local, 'last_check': {'ok': True, 'kind': 'generation', 'at': registry.now()}}
        with patch.object(panel, 'reachability_check') as probe:
            gate = panel.connectivity_gate(healthy)
        probe.assert_not_called()
        self.assertFalse(gate['checked'])

    def test_a_cloud_card_without_a_models_endpoint_keeps_working(self):
        cloud = {**self.local, 'id': 'cloud', 'provider': 'custom',
                 'api_base': 'https://example.invalid/v1'}
        with patch.object(panel, 'reachability_check',
                          return_value={'ok': False, 'kind': 'reachability', 'inference': False,
                                        'category': 'protocol', 'http_status': 404,
                                        'message': '只读可达性检查未通过：模型接口协议错误（HTTP 404）。'}):
            gate = panel.connectivity_gate(cloud)
        self.assertFalse(gate['blocked'])
        self.assertTrue(gate['checked'])

    def test_the_manual_generation_test_is_labelled_as_inference(self):
        model = {**self.local, 'api_key': ''}
        registry.write_json(registry.REGISTRY, {'default_model': 'local', 'models': [model]})
        with patch.object(panel, 'remote_request') as request:
            request.return_value.__enter__.return_value = io.BytesIO(
                json.dumps({'choices': [{'message': {'content': 'OK'}}]}).encode())
            check = panel.probe_model('local')
        self.assertTrue(check['ok'])
        self.assertEqual(check['kind'], 'generation')
        self.assertTrue(check['inference'])

    def test_route_preview_performs_no_network_or_inference(self):
        with patch.object(panel, 'reachability_check') as probe, \
             patch.object(panel, 'remote_request') as request:
            route = model_router.choose(self.body, [self.local], self.on)
        probe.assert_not_called()
        request.assert_not_called()
        self.assertEqual(route['model_id'], 'local')


class RecoveryApiTests(unittest.TestCase):
    """HTTP-level proof that pending recovery is readable and explicitly resumable."""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        import threading
        cls.temp = tempfile.TemporaryDirectory(prefix='recovery-api-')
        cls.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(cls.temp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        registry.write_json(registry.HERE / 'settings.json', {
            'model': 'qwen', 'api_base': 'http://127.0.0.1:1234/v1', 'api_key_env': '',
            'max_input_tokens': 32768, 'max_output_tokens': 4096})
        registry.list_models()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.opener = build_opener(ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        registry.HERE, registry.REGISTRY, registry.RUNS = cls.saved
        cls.temp.cleanup()

    def request(self, path, body=None):
        headers = {'Content-Type': 'application/json', 'X-Panel-Token': panel.TOKEN}
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.url + path, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_pending_recovery_is_visible_and_only_resumes_on_an_explicit_confirm(self):
        run = registry.RUNS / 'api-recovery'
        run.mkdir(parents=True, exist_ok=True)
        registry.patch_task(run, id='api-recovery', kind='chat', status='queued', source='panel',
                            title='待恢复任务')
        panel.recover_pending_tasks(entries=[{'run': run}])
        status, payload = self.request('/api/recovery')
        self.assertEqual(status, 200)
        ids = [row['task_id'] for row in payload['tasks']]
        self.assertIn('api-recovery', ids)
        self.assertIn('pending', payload)
        # No confirmation: refused, and the record is untouched.
        status, payload = self.request('/api/tasks/api-recovery/recover', {})
        self.assertEqual(status, 400)
        self.assertIn('confirm', payload['error'])
        submitted = []
        fake = SimpleNamespace(submit=lambda fn, task_id: submitted.append(task_id))
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'EXECUTOR', fake):
            status, payload = self.request('/api/tasks/api-recovery/recover', {'confirm': True})
        self.assertEqual(status, 202)
        self.assertFalse(payload['creates_new_record'])
        self.assertEqual(submitted, ['api-recovery'])
        # The list no longer shows it as pending, and a repeat is refused.
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'EXECUTOR', fake):
            status, payload = self.request('/api/tasks/api-recovery/recover', {'confirm': True})
        self.assertEqual(status, 400)
        self.assertEqual(len(submitted), 1)
        _, listing = self.request('/api/recovery')
        self.assertNotIn('api-recovery', [row['task_id'] for row in listing['tasks']])


class HardFaultCompactionTests(Sandbox):
    """c1 corrective round: a hard/unknown blocker must survive compaction."""

    def test_unresolved_hard_fault_survives_history_compaction(self):
        events = [recovery_retry.compact_event('budget', 'paid')]
        for _ in range(25):
            events = recovery_retry.append_event(events, recovery_retry.compact_event('gateway', 'connect'))
        events, _ = recovery_retry.resolve_transient(events, 'later', 'success')
        fault = recovery_retry.unresolved_fault(
            {'fault_events': events, 'budget_error': 'paid authorization denied'})
        self.assertIsNotNone(fault)
        self.assertEqual(fault['category'], 'paid')
        self.assertEqual(fault['class'], 'hard')
        self.assertTrue(any(row['category'] == 'paid' and not row.get('resolved') for row in events))
        # The newest MAX_FAULT_EVENTS transient rows stay, plus the pinned blocker.
        self.assertEqual(len([row for row in events if row['category'] == 'connect']),
                         recovery_retry.MAX_FAULT_EVENTS)
        self.assertEqual(len(events), recovery_retry.MAX_FAULT_EVENTS + 1)

    def test_repeated_compaction_never_drops_an_unknown_blocker(self):
        events = [recovery_retry.compact_event('gateway', 'mystery')]
        for _ in range(120):
            events = recovery_retry.append_event(events, recovery_retry.compact_event('gateway', 'connect'))
        self.assertTrue(any(row['category'] == 'mystery' for row in events))
        self.assertIsNotNone(recovery_retry.unresolved_fault({'fault_events': events}))

    def test_a_legacy_mirror_without_its_channel_fails_closed(self):
        events = [recovery_retry.compact_event('gateway', 'connect')]
        events, _ = recovery_retry.resolve_transient(events, 'later', 'ok')
        fault = recovery_retry.unresolved_fault(
            {'fault_events': events, 'budget_error': 'paid authorization denied'})
        self.assertIsNotNone(fault)
        self.assertTrue(fault['legacy'])
        self.assertEqual(fault['channel'], 'budget')

    def test_a_request_level_rejection_is_not_a_hard_blocker(self):
        events = [recovery_retry.compact_event('gateway', 'connect')]
        events, _ = recovery_retry.resolve_transient(events, 'later', 'ok')
        state = {'fault_events': events, 'budget_error': '已证实的上下文窗口不足',
                 'budget_error_scope': 'request'}
        self.assertIsNone(recovery_retry.unresolved_fault(state))
        # Without the explicit request scope the same mirror stays fail-closed.
        state.pop('budget_error_scope')
        self.assertIsNotNone(recovery_retry.unresolved_fault(state))

    def test_a_covered_channel_mirror_is_not_a_blocker(self):
        events = [{'channel': 'gateway', 'category': 'upstream_unavailable', 'class': 'transient',
                   'seq': 1, 'resolved': True, 'http_status': 503}]
        self.assertIsNone(recovery_retry.unresolved_fault(
            {'fault_events': events, 'gateway_error': '模型接口暂时不可用（HTTP 503）'}))

    def test_a_resolved_channel_stays_explained_after_compaction(self):
        events = [recovery_retry.compact_event('gateway', 'connect')]
        events, _ = recovery_retry.resolve_transient(events, 't1', 'ok1')
        for _ in range(25):
            events = recovery_retry.append_event(events, recovery_retry.compact_event('budget', 'record_io'))
        events, _ = recovery_retry.resolve_transient(events, 't2', 'ok2')
        # All faults were resolved; the pinned per-channel evidence keeps the
        # legacy mirror correctly explained instead of a false blocker.
        self.assertTrue(any(row['channel'] == 'gateway' for row in events))
        self.assertIsNone(recovery_retry.unresolved_fault(
            {'fault_events': events, 'gateway_error': '无法连接模型接口'}))


class RetryAdmissionProbeTests(unittest.TestCase):
    """c3 corrective round: ambiguous failures must never auto-replay."""

    def test_ambiguous_reset_and_gateway_5xx_never_replay(self):
        for category, status in [('reset', None), ('upstream_unavailable', 502),
                                 ('upstream_unavailable', 504), ('timeout', None)]:
            decision = recovery_retry.transport_retry_allowed(
                category=category, http_status=status, response_started=False)
            self.assertFalse(decision['retry'], (category, status))
            self.assertEqual(decision['reason'], 'not_safe_to_retry', (category, status))

    def test_retryable_transport_needs_explicit_acceptance_evidence(self):
        decision = recovery_retry.transport_retry_allowed(category='connect', acceptance='ambiguous')
        self.assertFalse(decision['retry'])
        self.assertEqual(decision['reason'], 'acceptance_unproven')
        self.assertTrue(recovery_retry.transport_retry_allowed(category='connect')['retry'])
        self.assertTrue(recovery_retry.transport_retry_allowed(category='dns')['retry'])

    def test_pre_send_evidence_cannot_launder_an_injected_reset(self):
        decision = recovery_retry.transport_retry_allowed(category='reset', acceptance='pre_send')
        self.assertFalse(decision['retry'])
        decision = recovery_retry.transport_retry_allowed(
            category='upstream_unavailable', http_status=504, acceptance='pre_send')
        self.assertFalse(decision['retry'])

    def test_non_finite_retry_after_is_never_a_wait(self):
        for value in ('nan', 'inf', '-inf', float('nan'), float('inf')):
            self.assertEqual(recovery_retry.retry_after_seconds(value), (None, 'unparsable'), value)
        decision = recovery_retry.transport_retry_allowed(category='rate_limit', retry_after=float('nan'))
        self.assertFalse(decision['retry'])
        self.assertEqual(decision['reason'], 'retry_after_too_long')
        decision = recovery_retry.transport_retry_allowed(
            category='rate_limit', retry_after_reason='unparsable')
        self.assertFalse(decision['retry'])
        self.assertEqual(decision['reason'], 'retry_after_invalid')


class RelayTerminalProofTests(Sandbox):
    """c1: a socket loop ending is not proof that a generation completed."""

    def setUp(self):
        super().setUp()
        self.model = {'id': 'home', 'name': '家里的 Qwen', 'provider': 'local',
                      'api_base': 'http://127.0.0.1:1/v1', 'model': 'qwen', 'api_key': '',
                      'bypass_proxy': True, 'thinking_level': 'default',
                      'max_input_tokens': 32768, 'max_output_tokens': 4096, 'thinking_info': None}
        self.run = self.task('relay-proof', status='running', model_calls=0, route={'budget_cny': 0},
                             cost_requests={}, kind='chat')
        self.body = {'model': 'qwen', 'messages': [{'role': 'user', 'content': 'Hi'}], 'max_tokens': 32}

    def call(self, opener):
        with patch.object(budget_relay, 'build_opener', return_value=opener):
            with budget_relay.Relay(self.run, self.model, workflow_settings.DEFAULTS) as relay:
                request = Request(relay.url + '/chat/completions', data=json.dumps(self.body).encode(),
                                  headers={'Authorization': 'Bearer ' + relay.token})
                client = build_opener(ProxyHandler({}))
                try:
                    with client.open(request, timeout=20) as response:
                        return response.status, response.read()
                except HTTPError as error:
                    return error.code, error.read()

    def state(self):
        return registry.read_task(self.run)

    def seed_unresolved(self):
        registry.patch_task(self.run,
                            fault_events=[recovery_retry.compact_event('gateway', 'connect')],
                            gateway_error='无法连接模型接口')

    def test_http_200_with_invalid_json_is_not_recovery_proof(self):
        self.seed_unresolved()
        opener = ScriptedOpener([FakeResponse(b'<html>gateway</html>')])
        status, _ = self.call(opener)
        self.assertEqual(status, 200)
        self.assertEqual(opener.calls, 1)
        state = self.state()
        self.assertNotEqual(state.get('fault_status'), 'recovered')
        self.assertIsNotNone(recovery_retry.unresolved_fault(state))
        self.assertTrue(any(row.get('category') == 'unknown' and not row.get('resolved')
                            for row in state['fault_events']))

    def test_http_200_with_error_json_is_not_recovery_proof(self):
        self.seed_unresolved()
        opener = ScriptedOpener([FakeResponse(b'{"error":{"message":"boom"}}')])
        status, _ = self.call(opener)
        self.assertEqual(status, 200)
        state = self.state()
        self.assertNotEqual(state.get('fault_status'), 'recovered')
        self.assertTrue(any(row.get('category') == 'protocol' and not row.get('resolved')
                            for row in state['fault_events']))
        self.assertIsNotNone(recovery_retry.unresolved_fault(state))

    def test_sse_without_a_terminal_completion_is_not_recovery_proof(self):
        self.seed_unresolved()
        chunk = b'data: {"choices":[{"delta":{"content":"A"}}]}\n'
        opener = ScriptedOpener([FakeResponse(b'', content_type='text/event-stream', chunks=[chunk])])
        status, _ = self.call(opener)
        self.assertEqual(status, 200)
        state = self.state()
        self.assertNotEqual(state.get('fault_status'), 'recovered')
        self.assertIsNotNone(recovery_retry.unresolved_fault(state))

    def test_a_valid_terminal_completion_still_recovers(self):
        self.seed_unresolved()
        opener = ScriptedOpener([FakeResponse(b'{"choices":[{"message":{"content":"OK"}}]}')])
        status, _ = self.call(opener)
        self.assertEqual(status, 200)
        state = self.state()
        self.assertEqual(state.get('fault_status'), 'recovered')
        self.assertTrue(all(row.get('resolved') for row in state['fault_events']))
        self.assertIsNone(recovery_retry.unresolved_fault(state))

    def test_sse_done_marker_still_recovers(self):
        self.seed_unresolved()
        chunks = [b'data: {"choices":[{"delta":{"content":"A"}}]}\n', b'data: [DONE]\n']
        opener = ScriptedOpener([FakeResponse(b'', content_type='text/event-stream', chunks=chunks)])
        status, _ = self.call(opener)
        self.assertEqual(status, 200)
        state = self.state()
        self.assertEqual(state.get('fault_status'), 'recovered')
        self.assertIsNone(recovery_retry.unresolved_fault(state))

    def test_sse_finish_reason_is_a_terminal_completion(self):
        self.seed_unresolved()
        chunks = [b'data: {"choices":[{"delta":{"content":"A"},"finish_reason":"stop"}]}\n']
        opener = ScriptedOpener([FakeResponse(b'', content_type='text/event-stream', chunks=chunks)])
        status, _ = self.call(opener)
        self.assertEqual(status, 200)
        self.assertEqual(self.state().get('fault_status'), 'recovered')

    def test_a_bare_reset_is_reported_not_replayed(self):
        opener = ScriptedOpener([ConnectionResetError(10054, 'reset')])
        status, _ = self.call(opener)
        self.assertEqual(status, 502)
        self.assertEqual(opener.calls, 1)
        state = self.state()
        self.assertNotIn('transport_retries', state)
        self.assertEqual(state['fault_events'][0]['category'], 'reset')
        self.assertIn('不自动重试', state['fault_events'][0]['detail'])

    def test_gateway_502_and_504_are_reported_not_replayed(self):
        for code in (502, 504):
            with self.subTest(code=code):
                self.run = self.task('relay-%d' % code, status='running', model_calls=0,
                                     route={'budget_cny': 0}, cost_requests={}, kind='chat')
                error = HTTPError('http://127.0.0.1:1/v1/chat/completions', code,
                                  'gateway', {}, None)
                opener = ScriptedOpener([error])
                status, _ = self.call(opener)
                self.assertEqual(status, 502)
                self.assertEqual(opener.calls, 1)
                state = self.state()
                self.assertEqual(state['fault_events'][0]['category'], 'upstream_unavailable')
                self.assertNotIn('transport_retries', state)

    def test_a_429_with_a_non_finite_retry_after_is_not_retried(self):
        error = HTTPError('http://127.0.0.1:1/v1/chat/completions', 429, 'slow down',
                          {'Retry-After': 'nan'}, None)
        opener = ScriptedOpener([error])
        status, _ = self.call(opener)
        self.assertEqual(status, 502)
        self.assertEqual(opener.calls, 1)
        state = self.state()
        self.assertNotIn('transport_retries', state)
        self.assertIn('Retry-After', state['fault_events'][0]['detail'])

    def test_a_request_level_guard_rejection_is_scoped_as_request(self):
        self.body = {'model': 'other', 'messages': [{'role': 'user', 'content': 'Hi'}],
                     'max_tokens': 32}
        status, _ = self.call(ScriptedOpener([]))
        self.assertEqual(status, 402)
        state = self.state()
        self.assertEqual(state['budget_error_scope'], 'request')
        # A recoverable local rejection must not poison the whole task outcome.
        self.assertIsNone(recovery_retry.unresolved_fault(state))


class RecordedProcessIdentityTests(Sandbox):
    """c5: unknown process state is not proof of death, slow startup is not reuse."""

    class FakeProcess:
        def __init__(self, cmdline, created):
            self._cmdline = cmdline
            self._created = created

        def cmdline(self):
            return self._cmdline

        def create_time(self):
            return self._created

    def worker_cmd(self):
        return ['python', str(panel.HERE / 'dsh_worker.py')]

    def test_unreadable_process_table_is_unknown_not_dead(self):
        with patch('psutil.Process', side_effect=psutil.AccessDenied(pid=12345)):
            verdict = panel.recorded_worker_alive({'worker_pid': 12345})
        self.assertEqual(verdict, panel.WORKER_STATE_UNKNOWN)

    def test_slow_startup_probe_keeps_a_live_worker(self):
        started = datetime.now(timezone.utc)
        created = started.timestamp() - 45  # created long before started_at was written
        with patch('psutil.Process', return_value=self.FakeProcess(self.worker_cmd(), created)):
            verdict = panel.recorded_worker_alive({'worker_pid': 4242, 'started_at': iso(started)})
        self.assertTrue(verdict)

    def test_a_newer_process_is_a_reused_pid_not_the_live_worker(self):
        started = datetime.now(timezone.utc) - timedelta(seconds=120)
        created = datetime.now(timezone.utc).timestamp()
        with patch('psutil.Process', return_value=self.FakeProcess(self.worker_cmd(), created)):
            verdict = panel.recorded_worker_alive({'worker_pid': 4242, 'started_at': iso(started)})
        self.assertFalse(verdict)

    def test_a_foreign_process_is_never_the_recorded_worker(self):
        with patch('psutil.Process', return_value=self.FakeProcess(['notepad.exe'], time.time())):
            verdict = panel.recorded_worker_alive({'worker_pid': 4242})
        self.assertFalse(verdict)

    def test_any_recorded_identity_proving_alive_wins(self):
        started = datetime.now(timezone.utc)
        alive = self.FakeProcess(self.worker_cmd(), started.timestamp() - 30)
        with patch('psutil.Process', side_effect=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid))
                   if pid == 1 else alive):
            verdict = panel.recorded_worker_alive({'worker_pid': 1, 'launcher_pid': 2,
                                                   'started_at': iso(started)})
        self.assertTrue(verdict)

    def test_recovery_refuses_an_unreadable_process_table(self):
        run = self.task('unknown-worker', status='recovery_pending', source='panel', kind='chat',
                        worker_pid=12345)
        submitted = []
        fake = SimpleNamespace(submit=lambda fn, tid: submitted.append(tid))
        with patch('psutil.Process', side_effect=psutil.AccessDenied(pid=12345)), \
             patch.object(panel, 'EXECUTOR', fake):
            with self.assertRaisesRegex(ValueError, '无法确认'):
                panel.recover_task('unknown-worker', {'confirm': True})
        self.assertEqual(submitted, [])
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')

    def test_restart_reconciliation_marks_an_unreadable_worker_unknown(self):
        run = self.task('unknown-restart', status='running', source='panel', worker_pid=12345,
                        started_at=iso(datetime.now(timezone.utc)))
        with patch('psutil.Process', side_effect=psutil.AccessDenied(pid=12345)):
            actions = panel.recover_pending_tasks(entries=[{'run': run}])
        state = registry.read_task(run)
        self.assertEqual(state['status'], 'recovery_pending')
        self.assertEqual(state['recovery']['kind'], 'worker_state_unknown')
        self.assertFalse(state['recovery']['auto_resend'])
        self.assertEqual([row['kind'] for row in actions], ['worker_state_unknown'])


class RecoveryConcurrencyTests(Sandbox):
    """c5: simultaneous confirmed recoveries must enqueue the task at most once."""

    def test_simultaneous_confirmed_recovery_submits_only_once(self):
        run = registry.RUNS / 'race-task'
        run.mkdir(parents=True, exist_ok=True)
        registry.patch_task(run, id='race-task', status='recovery_pending', engine='dsh',
                            model_id='home', kind='edit', project=str(run.parent), files=['a.py'])
        barrier = threading.Barrier(2)
        read = registry.read_task

        def simultaneous_snapshot(target):
            value = read(target)
            barrier.wait(timeout=8)
            return value

        submitted = []

        def queue_only(*args, **kwargs):
            submitted.append(args[1])

        def confirm():
            try:
                return panel.recover_task('race-task', {'confirm': True})
            except Exception as error:
                return {'error': type(error).__name__}

        with patch.object(registry, 'task_dir', return_value=run), \
             patch.object(registry, 'read_task', side_effect=simultaneous_snapshot), \
             patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel.EXECUTOR, 'submit', side_effect=queue_only), \
             patch.dict(panel.ACTIVE, {}, clear=True):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: confirm(), range(2)))
        self.assertEqual(len(submitted), 1)
        self.assertEqual(sum(1 for row in outcomes if row.get('ok')), 1)
        self.assertEqual(sum(1 for row in outcomes if row.get('error')), 1)
        self.assertEqual(registry.read_task(run)['status'], 'queued')

    def test_a_failed_enqueue_rolls_the_recovery_back(self):
        run = self.task('rollback-task', status='recovery_pending', source='panel', kind='chat')
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel.EXECUTOR, 'submit', side_effect=RuntimeError('executor closed')):
            with self.assertRaises(RuntimeError):
                panel.recover_task('rollback-task', {'confirm': True})
        state = registry.read_task(run)
        self.assertEqual(state['status'], 'recovery_pending')
        self.assertFalse(state['recovery']['confirmed'])
        self.assertIn('submit_error', state['recovery'])


class StageRecoveryAdmissionTests(Sandbox):
    """c5: stage recovery must run the real admission checks, never silently skip."""

    def test_a_missing_stage_blocks_recovery(self):
        run = self.task('missing-stage', status='recovery_pending', source='panel', kind='edit',
                        stage_id='stage-does-not-exist', goal_id='goal-does-not-exist')
        submitted = []
        fake = SimpleNamespace(submit=lambda fn, tid: submitted.append(tid))
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'EXECUTOR', fake):
            with self.assertRaisesRegex(ValueError, '阶段不存在'):
                panel.recover_task('missing-stage', {'confirm': True})
        self.assertEqual(submitted, [])
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')

    def test_an_unreadable_stage_blocks_recovery(self):
        run = self.task('broken-stage', status='recovery_pending', source='panel', kind='edit',
                        stage_id='stage-broken', goal_id='goal-broken')
        submitted = []
        fake = SimpleNamespace(submit=lambda fn, tid: submitted.append(tid))
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel.stage_store, 'get_stage',
                          side_effect=stage_store.StageError('阶段记录损坏')), \
             patch.object(panel, 'EXECUTOR', fake):
            with self.assertRaisesRegex(ValueError, '不可读'):
                panel.recover_task('broken-stage', {'confirm': True})
        self.assertEqual(submitted, [])
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')

    def test_a_cancelled_goal_blocks_recovery(self):
        goal = stage_store.create_goal('取消目标', project='')
        stage = stage_store.add_stage(goal['id'], '阶段一', objective='x', acceptance=['c1'], actor='main')
        run = self.task('goal-cancelled', status='recovery_pending', source='panel', kind='edit',
                        stage_id=stage['id'], goal_id=goal['id'])
        stage_store.set_goal_status(goal['id'], stage_store.GOAL_CANCELLED, actor='main')
        submitted = []
        fake = SimpleNamespace(submit=lambda fn, tid: submitted.append(tid))
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'EXECUTOR', fake):
            with self.assertRaisesRegex(ValueError, '目标已取消'):
                panel.recover_task('goal-cancelled', {'confirm': True})
        self.assertEqual(submitted, [])
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.READY)

    def test_a_failed_enqueue_releases_the_stage_claim(self):
        goal = stage_store.create_goal('回滚目标', project='')
        stage = stage_store.add_stage(goal['id'], '阶段一', objective='x', acceptance=['c1'], actor='main')
        run = self.task('stage-rollback', status='recovery_pending', source='panel', kind='edit',
                        stage_id=stage['id'], goal_id=goal['id'])
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel.EXECUTOR, 'submit', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                panel.recover_task('stage-rollback', {'confirm': True})
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.RECOVERY_PENDING)

    def test_a_missing_prerequisite_handover_blocks_recovery(self):
        goal = stage_store.create_goal('依赖目标', project='')
        first = stage_store.add_stage(goal['id'], '阶段一', objective='x', acceptance=['c1'], actor='main')
        second = stage_store.add_stage(goal['id'], '阶段二', objective='y', acceptance=['c2'],
                                       depends_on=[first['id']], actor='main')
        run = self.task('dep-task', status='recovery_pending', source='panel', kind='edit',
                        stage_id=second['id'], goal_id=goal['id'])
        submitted = []
        fake = SimpleNamespace(submit=lambda fn, tid: submitted.append(tid))
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel.task_handover, 'prerequisite_prompt',
                          side_effect=task_handover.HandoverError('前置阶段缺少已验收的成果交接')), \
             patch.object(panel, 'EXECUTOR', fake):
            with self.assertRaisesRegex(ValueError, '交接'):
                panel.recover_task('dep-task', {'confirm': True})
        self.assertEqual(submitted, [])
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')

    def test_recovery_refuses_a_model_outside_the_current_scope(self):
        goal = stage_store.create_goal('范围目标', project='')
        stage = stage_store.add_stage(goal['id'], '阶段一', objective='x', acceptance=['c1'], actor='main')
        run = self.task('scope-task', status='recovery_pending', source='panel', kind='edit',
                        model_id='cloud', stage_id=stage['id'], goal_id=goal['id'])
        registry.write_json(registry.REGISTRY, {'default_model': 'cloud', 'models': [
            {'id': 'cloud', 'name': 'cloud', 'provider': 'deepseek', 'api_base': 'https://api.deepseek.com',
             'model': 'deepseek-flash', 'secret': '', 'enabled': True, 'max_input_tokens': 1000000,
             'max_output_tokens': 64000, 'last_check': None, 'created_at': registry.now(),
             'preferred_tasks': [], 'cost_preference': 'balanced', 'max_task_complexity': 'complex',
             'agent_preset': 'standard'}]})
        workflow_settings.save_settings({'enabled': True, 'paid_allowed': True, 'model_scope': 'local_only'})
        submitted = []
        gate_calls = []
        real_gate = panel.scope_gate

        def recording_gate(model, settings, task_id=None):
            gate_calls.append((model.get('id'), settings.get('model_scope')))
            return real_gate(model, settings, task_id=task_id)

        fake = SimpleNamespace(submit=lambda fn, tid: submitted.append(tid))
        with patch.object(panel, 'recorded_worker_alive', return_value=False), \
             patch.object(panel, 'scope_gate', side_effect=recording_gate), \
             patch.object(panel, 'EXECUTOR', fake):
            with self.assertRaisesRegex(ValueError, '执行模型范围'):
                panel.recover_task('scope-task', {'confirm': True})
        self.assertEqual(gate_calls, [('cloud', 'local_only')])
        self.assertEqual(submitted, [])
        # Refused before any state write or stage claim: the task stays pending and
        # the stage is never claimed for a model the scope excludes.
        self.assertEqual(registry.read_task(run)['status'], 'recovery_pending')
        self.assertEqual(stage_store.get_stage(stage['id'])['status'], stage_store.READY)


if __name__ == '__main__':
    unittest.main(verbosity=2)
