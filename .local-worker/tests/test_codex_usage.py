from pathlib import Path
import json
import io
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_usage as usage


class UsageTests(unittest.TestCase):
    def sample(self, used=25):
        return {'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': used, 'windowDurationMins': 300, 'resetsAt': 1791904018}}}

    def wait_cache(self, cache):
        # Join the real cache worker, not sleeps or repeated network calls.
        for thread in threading.enumerate():
            if thread.name == 'codex-usage-read':
                thread.join(timeout=2)
        self.assertFalse(cache.refreshing)

    def test_multibucket_preferred_and_private_fields_removed(self):
        data = self.sample()
        data.update(accountId='private-account', accessToken='private-token')
        data['rateLimitsByLimitId'] = {
            'extra': {'limitName': '额外模型', 'primary': {'usedPercent': 140, 'windowDurationMins': 60, 'resetsAt': None}},
            'codex': {'planType': 'plus', 'secondary': {'usedPercent': 24, 'windowDurationMins': 10080, 'resetsAt': 1792472640}}}
        result = usage.normalize(data)
        self.assertEqual([b['id'] for b in result['buckets']], ['codex', 'extra'])
        self.assertEqual(result['buckets'][0]['windows'][0]['remaining_percent'], 76)
        self.assertEqual(result['buckets'][1]['windows'][0]['remaining_percent'], 0)
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn('300', json.dumps(result))

    def test_missing_values_are_unknown_not_zero(self):
        data = {'rateLimits': {'primary': {'usedPercent': None, 'windowDurationMins': None, 'resetsAt': None}}}
        window = usage.normalize(data)['buckets'][0]['windows'][0]
        self.assertIsNone(window['remaining_percent'])
        self.assertIsNone(window['resets_at'])
        self.assertIsNone(window['window_minutes'])
        self.assertEqual(usage.normalize({})['buckets'], [])
        invalid = usage.normalize({'rateLimits': {'primary': {'usedPercent': True, 'windowDurationMins': float('nan'), 'resetsAt': float('inf')}}})
        self.assertIsNone(invalid['buckets'][0]['windows'][0]['remaining_percent'])

    def test_cache_throttles_state_poll_and_manual_refresh(self):
        now = [0.0]
        calls = []
        def query():
            calls.append(1)
            return self.sample()
        cache = usage.UsageCache(query=query, clock=lambda: now[0])
        cache.snapshot()
        self.wait_cache(cache)
        for _ in range(20):
            state = cache.snapshot(force=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state['buckets'][0]['windows'][0]['remaining_percent'], 75)
        now[0] = 10
        cache.snapshot(force=True)
        self.wait_cache(cache)
        self.assertEqual(len(calls), 2)
        now[0] = 69
        cache.snapshot()
        self.assertEqual(len(calls), 2)
        now[0] = 70
        cache.snapshot()
        self.wait_cache(cache)
        self.assertEqual(len(calls), 3)

    def test_inflight_refresh_is_single_and_nonblocking(self):
        release = threading.Event()
        calls = []
        def query():
            calls.append(1)
            release.wait(timeout=2)
            return self.sample()
        cache = usage.UsageCache(query=query)
        state = cache.snapshot()
        self.assertTrue(state['refreshing'])
        for _ in range(20):
            cache.snapshot(force=True)
        release.set()
        self.wait_cache(cache)
        self.assertEqual(len(calls), 1)

    def test_failed_refresh_preserves_marked_old_data_and_hides_errors(self):
        now = [0]
        def query():
            if now[0] == 0:
                return self.sample()
            raise RuntimeError('private-token provider detail')
        cache = usage.UsageCache(query=query, clock=lambda: now[0])
        cache.snapshot()
        self.wait_cache(cache)
        now[0] = 60
        cache.snapshot()
        self.wait_cache(cache)
        state = cache.snapshot()
        self.assertTrue(state['stale'])
        self.assertEqual(state['buckets'][0]['windows'][0]['remaining_percent'], 75)
        self.assertNotIn('private-token', json.dumps(state))

    def test_first_failure_does_not_invent_quota(self):
        def query():
            raise usage.UsageError('未找到本机 Codex 程序')
        cache = usage.UsageCache(query=query)
        cache.snapshot()
        self.wait_cache(cache)
        state = cache.snapshot()
        self.assertEqual(state['status'], 'error')
        self.assertEqual(state['buckets'], [])
        self.assertIsNone(state['updated_at'])

    def test_rpc_only_reads_limits_and_reaps_owned_process(self):
        class RecordingInput(io.StringIO):
            def close(self):
                self.closed_by_client = True
        class FakeProcess:
            stdin = RecordingInput()
            stdout = io.StringIO(json.dumps({'id': 1, 'result': {}})+'\n'+json.dumps({'id': 2, 'result': self.sample()})+'\n')
            waited = False
            def wait(proc, timeout):
                proc.waited = True
                return 0
        process = FakeProcess()
        with patch.object(usage, 'find_codex', return_value='codex.exe'), patch.object(usage.subprocess, 'Popen', return_value=process) as spawn:
            result = usage.query_limits()
        methods = [json.loads(line)['method'] for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(methods, ['initialize', 'initialized', 'account/rateLimits/read'])
        self.assertEqual(spawn.call_args.args[0], ['codex.exe', 'app-server', '--stdio'])
        self.assertEqual(result, self.sample())
        self.assertTrue(process.waited)
        self.assertTrue(process.stdin.closed_by_client)
        self.assertTrue(process.stdout.closed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
