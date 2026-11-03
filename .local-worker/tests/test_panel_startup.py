"""Incremental startup checks: temporary HTTP, fake clocks, no inference."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel_startup as startup
import panel_server as panel


class StartupTests(unittest.TestCase):
    def test_concurrent_opens_create_one_process(self):
        root, lock, launched = Path.cwd(), threading.Lock(), threading.Event()
        starts = []
        def start(timeout):
            starts.append(timeout)
            time.sleep(.02)
            launched.set()
        def probe(timeout):
            return str(root) if launched.is_set() else None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: startup.ensure(root, lock, probe, start), range(4)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(starts), 1)

    def test_lock_wait_counts_against_total_budget(self):
        lock = threading.Lock(); lock.acquire()
        start = mock.Mock()
        before = time.monotonic()
        try:
            with self.assertRaisesRegex(startup.StartupError, 'startup_busy'):
                startup.ensure(Path.cwd(), lock, mock.Mock(), start, timeout=.03)
        finally:
            lock.release()
        self.assertLess(time.monotonic() - before, .3)
        start.assert_not_called()

    def test_launcher_timeout_but_confirmed_backend_is_success(self):
        root = Path.cwd()
        probe = mock.Mock(side_effect=[None, str(root)])
        start = mock.Mock(side_effect=startup.StartupError('launcher_timeout', 'fixture'))
        self.assertTrue(startup.ensure(root, threading.Lock(), probe, start))
        start.assert_called_once()

    def test_timeout_without_ready_does_not_become_success_or_retry(self):
        class Clock:
            now = 0
            def monotonic(self): return self.now
            def sleep(self, seconds): self.now += seconds
        clock = Clock()
        def start(timeout):
            clock.now += timeout
            raise startup.StartupError('launcher_timeout', 'fixture')
        launched = mock.Mock(side_effect=start)
        with mock.patch.object(startup, 'time', clock), self.assertRaisesRegex(startup.StartupError, 'launcher_timeout'):
            startup.ensure(Path.cwd(), threading.Lock(), lambda timeout: None, launched)
        launched.assert_called_once()
        self.assertLessEqual(clock.now, startup.STARTUP_BUDGET)

    def test_delayed_ready_uses_remaining_budget_and_never_relaunches(self):
        class Clock:
            now = 0
            def monotonic(self): return self.now
            def sleep(self, seconds): self.now += seconds
        clock = Clock()
        def launch(timeout): clock.now += 25
        def probe(timeout): return str(Path.cwd()) if clock.now >= 30 else None
        start = mock.Mock(side_effect=launch)
        with mock.patch.object(startup, 'time', clock):
            self.assertTrue(startup.ensure(Path.cwd(), threading.Lock(), probe, start))
        start.assert_called_once()
        self.assertLess(clock.now, startup.STARTUP_BUDGET)

    def test_launcher_nonzero_is_not_retried_and_identity_errors_are_not_swallowed(self):
        for code in ('MD_START_FOREIGN', 'launcher_exit', 'spawn_failed'):
            start = mock.Mock(side_effect=startup.StartupError(code, 'fixture'))
            with self.subTest(code=code), self.assertRaisesRegex(startup.StartupError, code):
                startup.ensure(Path.cwd(), threading.Lock(), lambda timeout: None, start)
            start.assert_called_once()

    def test_foreign_installation_is_never_replaced(self):
        start = mock.Mock()
        with self.assertRaisesRegex(startup.StartupError, 'foreign_installation'):
            startup.ensure(Path.cwd(), threading.Lock(), lambda timeout: str(Path.cwd() / 'other'), start)
        start.assert_not_called()

    def test_spawn_error_and_nonzero_diagnostics_are_safe_and_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            launcher = Path(folder) / 'launcher.ps1'; launcher.touch()
            for error, code in [(OSError('private fixture secret'), 'spawn_failed'),
                                (subprocess.TimeoutExpired('private fixture secret', 1), 'launcher_timeout')]:
                with mock.patch.object(startup.subprocess, 'run', side_effect=error), self.assertRaises(startup.StartupError) as raised:
                    startup.launch(folder, 'pwsh', launcher, 'http://127.0.0.1:9123', 10)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn('secret', str(raised.exception))
            def fail(*args, **kwargs):
                kwargs['stdout'].write(b'private fixture secret\xff MD_START_FOREIGN')
                return mock.Mock(returncode=1)
            with mock.patch.object(startup.subprocess, 'run', side_effect=fail), self.assertRaises(startup.StartupError) as raised:
                startup.launch(folder, 'pwsh', launcher, 'http://127.0.0.1:9123', 10)
            self.assertEqual(raised.exception.code, 'MD_START_FOREIGN')
            self.assertNotIn('secret', str(raised.exception))


class ReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'
        cls.opener = build_opener(ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def test_minimal_health_skips_models_accounts_tasks_and_settings(self):
        with mock.patch.object(panel.registry, 'list_models', side_effect=AssertionError('models')), \
             mock.patch.object(panel.workflow_settings, 'snapshot', side_effect=AssertionError('settings')), \
             mock.patch.object(panel, 'panel_snapshot_tasks', side_effect=AssertionError('tasks')), \
             mock.patch.object(panel.host_balance, 'state', side_effect=AssertionError('account')):
            self.assertEqual(startup.workspace(self.base), str(panel.HERE.parent))
        with self.opener.open(self.base + '/api/ready') as response:
            data = json.load(response)
        self.assertEqual(set(data), {'service', 'ready', 'workspace', 'view','approved_plan_notifications'})
        self.assertEqual(data['approved_plan_notifications'],1)

    def test_health_keeps_host_and_origin_guards(self):
        for headers in [{'Host': 'evil.test'}, {'Origin': 'https://evil.test'}]:
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                self.opener.open(Request(self.base + '/api/ready', headers=headers))
            self.assertEqual(error.exception.code, 403)

    def test_no_redirect_and_invalid_identity_fail_closed(self):
        class Bad(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(302); self.send_header('Location', 'https://invalid.test'); self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Bad)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with self.assertRaisesRegex(startup.StartupError, 'identity_unknown'):
                startup.workspace(f'http://127.0.0.1:{server.server_port}')
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_legacy_fallback_is_only_used_on_404(self):
        requests = []
        class Legacy(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                requests.append(self.path)
                self.send_response(404 if self.path == '/api/ready' else 200); self.end_headers()
                if self.path == '/api/state': self.wfile.write(json.dumps({'workspace': str(Path.cwd())}).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Legacy)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            self.assertEqual(startup.workspace(f'http://127.0.0.1:{server.server_port}'), str(Path.cwd()))
            self.assertEqual(requests, ['/api/ready', '/api/state'])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_slow_stream_does_not_extend_readiness_deadline(self):
        class Slow(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(200); self.end_headers()
                try:
                    for _ in range(20):
                        self.wfile.write(b' '); self.wfile.flush(); time.sleep(.02)
                except OSError:
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            before = time.monotonic()
            with self.assertRaises(startup.StartupError):
                startup.workspace(f'http://127.0.0.1:{server.server_port}', timeout=.07)
            self.assertLess(time.monotonic() - before, .3)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
