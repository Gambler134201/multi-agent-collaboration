"""Ephemeral HTTP controller tests for the loopback brain transport.

Uses the real panel Handler on a temporary loopback port and a temporary project
registry.  Only the executor submission is mocked; no model, provider or desktop
notification is touched.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_context
import brain_gateway
import brain_transport
import delegation_policy as policy
import model_registry as registry
import panel_server as panel
import project_storage


class LossServer(BaseHTTPRequestHandler):
    """A controller that performs the POST then drops the connection."""

    calls = 0
    capability = 'private-fixture-capability'

    def log_message(self, *_):
        pass

    def _json(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/api/session':
            return self._json(200, {'token': 'fixture-panel-token'})
        self._json(404, {'error': 'no'})

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        if self.path == '/api/brain/connect':
            return self._json(201, {'capability': self.capability,
                                    'context': {'project': body.get('project')},
                                    'limits': {}})
        if self.path == '/api/brain/call':
            type(self).calls += 1
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        self._json(404, {'error': 'no'})


class BrainHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.tmp.name)
        registry.REGISTRY = registry.HERE / 'models.json'
        registry.RUNS = registry.HERE / 'runs'
        registry.write_json(registry.HERE / 'workflow.json', {'enabled': True})
        self.project = registry.HERE / 'project'
        self.project.mkdir()
        registry.write_json(registry.REGISTRY, {'default_model': 'mock', 'models': [{
            'id': 'mock', 'name': 'Mock', 'provider': 'local', 'model': 'mock',
            'enabled': True, 'max_input_tokens': 32768, 'max_output_tokens': 4096,
            'preferred_tasks': [], 'max_task_complexity': 'simple',
        }]})
        brain_gateway._CAPABILITIES.clear()
        self.submitted = []
        self.patches = [
            mock.patch.object(panel.dsh_worker, 'availability',
                              return_value={'ready': True, 'message': ''}),
            mock.patch.object(panel.EXECUTOR, 'submit',
                              side_effect=lambda fn, task_id: self.submitted.append(task_id)),
        ]
        for patch in self.patches:
            patch.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]
        self.context = brain_context.build_context(
            project=str(self.project), host='test-host', owner='main-a', role='brain',
            operator_session='s-a', session_binding='user_bound')

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for patch in reversed(self.patches):
            patch.stop()
        brain_gateway._CAPABILITIES.clear()
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.tmp.cleanup()

    def connect(self):
        transport = brain_transport.BrainTransport(self.url, timeout=5)
        result = transport.connect(self.context.declaration())
        return transport, result

    def test_explicit_connect_returns_public_context_only(self):
        transport, result = self.connect()
        self.assertTrue(result['connected'])
        self.assertEqual(result['context']['owner']['id'], 'main-a')
        self.assertEqual(result['context']['project'], str(self.project))
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn('capability', json.dumps(result).lower())
        self.assertNotIn('fixture-panel-token', json.dumps(result))
        self.assertTrue(transport.connected)
        self.assertNotIn('private', repr(transport))
        self.assertEqual(transport.call('describe', {})['context']['host']['id'], 'test-host')

    def test_connect_requires_an_already_running_service(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        transport = brain_transport.BrainTransport('http://127.0.0.1:%d' % port, timeout=2)
        with self.assertRaises(brain_transport.Unavailable) as caught:
            transport.connect(self.context.declaration())
        self.assertIn('未自动启动服务', str(caught.exception))
        self.assertFalse(transport.connected)

    def test_backend_restart_requires_explicit_reconnect(self):
        transport, _ = self.connect()
        brain_gateway._CAPABILITIES.clear()  # simulated panel restart
        with self.assertRaises(brain_transport.NotConnected):
            transport.call('describe', {})
        self.assertFalse(transport.connected)
        transport.connect(self.context.declaration())
        self.assertEqual(transport.call('describe', {})['context']['owner']['id'], 'main-a')

    def test_http_dispatch_status_and_delivery_do_not_redispatch_or_wait(self):
        transport, _ = self.connect()
        result = transport.call('dispatch', {
            'request_id': 'http-1', 'prompt': 'Do the work', 'kind': 'edit',
            'files': ['a.py'], 'acceptance': ['one', 'two']})
        task_id = result['task']['task_id']
        self.assertEqual(self.submitted, [task_id])
        self.assertEqual(transport.call('status', {'task_id': task_id})['status'], 'queued')
        self.assertEqual(transport.call('status', {'task_id': task_id})['status'], 'queued')
        self.assertEqual(transport.call('delivery', {'task_id': task_id})['delivery_state'],
                         'in_progress')
        self.assertEqual(self.submitted, [task_id])
        # Same request id returns the original task and never queues again.
        replay = transport.call('dispatch', {
            'request_id': 'http-1', 'prompt': 'Do the work', 'kind': 'edit',
            'files': ['a.py'], 'acceptance': ['one', 'two']})
        self.assertTrue(replay['reused'])
        self.assertEqual(replay['task']['task_id'], task_id)
        self.assertEqual(self.submitted, [task_id])

    def test_no_controller_credential_is_persisted(self):
        transport, _ = self.connect()
        capability = transport._capability
        self.assertTrue(capability)
        task_id = transport.call('dispatch', {
            'request_id': 'http-secret', 'prompt': 'Do the work', 'kind': 'edit',
            'files': ['a.py'], 'acceptance': ['one']})['task']['task_id']
        task = registry.read_task(registry.task_dir(task_id, str(self.project)))
        self.assertNotIn(capability, json.dumps(task, ensure_ascii=False))
        conn = project_storage.connect(project_storage.records_db(str(self.project)), False)
        try:
            rows = conn.execute('SELECT * FROM brain_requests').fetchall()
        finally:
            conn.close()
        blob = json.dumps([dict(row) for row in rows], ensure_ascii=False)
        self.assertNotIn(capability, blob)
        self.assertNotIn('fixture-panel-token', blob)

    def test_lost_mutation_response_is_unknown_and_not_retried(self):
        LossServer.calls = 0
        server = ThreadingHTTPServer(('127.0.0.1', 0), LossServer)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = brain_transport.BrainTransport(
                'http://127.0.0.1:%d' % server.server_address[1], timeout=3)
            transport.connect(self.context.declaration())
            with self.assertRaises(brain_transport.OutcomeUnknown):
                transport.call('dispatch', {'request_id': 'loss-1', 'prompt': 'x'})
            self.assertEqual(LossServer.calls, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_base_url_is_strictly_loopback_and_path_free(self):
        for value in ('https://127.0.0.1:8765', 'http://example.com:8765',
                      'http://127.0.0.1:8765/api', 'http://user:pass@127.0.0.1:8765',
                      'http://127.0.0.1:8765?x=1', 'relative'):
            with self.subTest(value=value):
                with self.assertRaises(brain_transport.TransportError):
                    brain_transport.normalize_base_url(value)
        self.assertEqual(brain_transport.normalize_base_url('http://localhost:8765/'),
                         'http://localhost:8765')


    def test_malformed_successful_mutation_response_is_unknown(self):
        transport, _ = self.connect()
        with mock.patch.object(transport, '_request', return_value=(200, {'error':'malformed response'})):
            with self.assertRaises(brain_transport.OutcomeUnknown):
                transport.call('dispatch', {'request_id':'malformed-1'})

    def test_reflected_connection_credential_is_redacted(self):
        transport, _ = self.connect()
        private = transport._capability
        with mock.patch.object(transport, '_request', return_value=(400, {'error':'reflected '+private})):
            with self.assertRaises(brain_transport.CallRejected) as caught:
                transport.call('candidates', {})
        self.assertNotIn(private, str(caught.exception))
        self.assertNotIn(private, json.dumps(caught.exception.payload))


if __name__ == '__main__':
    unittest.main()
