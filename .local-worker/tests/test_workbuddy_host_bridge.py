import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workbuddy_executor as wb
import workbuddy_host_bridge as host


class FakeControl:
    server_pid = 100
    def __init__(self, profile, scenario='ok'):
        self.profile, self.scenario = profile, scenario
        self.requests = []
        self.frames = []
        self.closed = False

    def write(self, text):
        request = json.loads(text)
        self.requests.append(request)
        sid = request['params']['sessionId']
        if self.scenario == 'unknown':
            return
        if self.scenario == 'rejected':
            response = {'jsonrpc': '2.0', 'id': request['id'],
                        'error': {'code': -32000, 'message': 'private-error-do-not-expose'}}
        else:
            response = {'jsonrpc': '2.0', 'id': request['id'], 'result': {
                'sessionId': 'foreign' if self.scenario == 'foreign' else sid,
                'socketPath': '\\\\.\\pipe\\' + host.profile_prefix(self.profile) + 'data-' + sid + '-' +
                              host.hashlib.sha1(sid.encode()).hexdigest()[:8],
                'runtimeId': '11111111-1111-4111-8111-111111111111', 'pid': 200}}
        self.frames.append(json.dumps(response) + '\n')

    def readline(self, timeout=None):
        return self.frames.pop(0) if self.frames else ''

    def close(self):
        self.closed = True


class FakeData:
    def __init__(self):
        self.frames, self.requests = queue.Queue(), []
        self.closed = False
        self.api = Mock()
        self.api.WaitForSingleObject.return_value = 0

    def verified_process(self, pid, executable):
        if pid != 200:
            raise host.HostUnavailable('wrong child')
        return 1

    def write(self, text):
        req = json.loads(text)
        self.requests.append(req)
        method = req['method']
        if method == 'initialize':
            result = {'protocolVersion': 1}
        elif method == 'session/new':
            result = {'sessionId': 'own-acp-session', 'models': {
                'currentModelId': 'deepseek-v4.1-flash',
                'availableModels': [{'modelId': 'deepseek-v4.1-flash'}]}}
        else:
            raise AssertionError('preflight must not send this method: ' + method)
        self.frames.put(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}) + '\n')

    def flush(self):
        pass

    def __iter__(self):
        while not self.closed:
            try:
                line = self.frames.get(timeout=0.02)
            except queue.Empty:
                continue
            if line:
                yield line

    def close(self):
        self.closed = True


class HostBridgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.info = {'product_name': 'WorkBuddy', 'version': wb.SUPPORTED_VERSION,
                     'executable': str(self.root / 'WorkBuddy.exe'), 'cli': str(self.root / 'cli')}
        self.channel = host.profile_prefix(self.root) + 'control-a1234567'

    def client(self, scenario='ok'):
        control, data = FakeControl(self.root, scenario), FakeData()
        def factory(path, executable, expected_pid=None):
            self.assertEqual(executable, self.info['executable'])
            if 'control-' in path:
                return control
            self.assertEqual(expected_pid, control.server_pid)
            return data
        c = host.create_client(self.info, self.root, self.root, 'deepseek-v4.1-flash',
                               names=[self.channel], pipe_factory=factory)
        self.addCleanup(c.close)
        return c, control, data

    def test_discovery_is_profile_bound_and_refuses_ambiguity(self):
        self.assertTrue(host.select_channel(self.root, [self.channel, 'unrelated']).endswith(self.channel))
        for names in [[], ['other-profile-control-a1234567'], [self.channel, self.channel[:-1] + '8']]:
            with self.subTest(names=names), self.assertRaises(host.HostUnavailable):
                host.select_channel(self.root, names)

    def test_discovery_does_not_guess_profile_or_process(self):
        with self.assertRaises(ValueError):
            host.profile_prefix('relative')
        wrong = {**self.info, 'version': 'unknown'}
        with self.assertRaises(host.HostUnavailable):
            host.create_client(wrong, self.root, self.root, 'deepseek-v4.1-flash',
                               names=[self.channel], pipe_factory=lambda *a, **k: self.fail('opened'))

    def test_no_ping_userinfo_list_bootstrap_or_restart_requests(self):
        pipe = FakeControl(self.root)
        control = host.LeaseControl(pipe, 'modeldesk-wb-' + '1' * 32)
        for method in ['sidecar.ping', 'session.list', 'session.reconnect', 'sidecar.shutdown',
                       'sidecar.credentialProtection.bootstrap', 'getUserInfo']:
            with self.subTest(method=method), self.assertRaises(ValueError):
                control.request(method, {'sessionId': control.session_id})
        self.assertEqual(pipe.requests, [])

    def test_native_preflight_creates_one_owned_no_tools_session_without_prompt(self):
        client, control, data = self.client()
        result = client.connect()
        self.assertEqual(result['current_model_id'], 'deepseek-v4.1-flash')
        self.assertFalse(result['account_verified'])
        self.assertFalse(client.prompt_sent)
        self.assertEqual([x['method'] for x in data.requests], ['initialize', 'session/new'])
        self.assertEqual([x['method'] for x in control.requests], ['session.create'])
        params = control.requests[0]['params']
        self.assertEqual(params['command'], self.info['executable'])
        self.assertEqual(params['cwd'], str(self.root.resolve()))
        self.assertEqual(params['port'], 0)
        self.assertEqual(params['args'][params['args'].index('--tools') + 1], '')
        self.assertEqual(params['env']['CODEBUDDY_HOST'], 'workbuddy-desktop')
        self.assertNotIn('CODEBUDDY_DISABLE_REQUEST_VALIDATION', params['env'])

    def test_each_native_lease_gets_a_fresh_session(self):
        _, first, _ = self.client()
        _, second, _ = self.client()
        self.assertNotEqual(first.requests[0]['params']['sessionId'], second.requests[0]['params']['sessionId'])

    def test_reviewed_583_uses_the_same_owned_lease_and_product_identity(self):
        self.info['version'] = '5.8.3'
        client, control, data = self.client()
        self.assertEqual(client.connect()['current_model_id'], 'deepseek-v4.1-flash')
        self.assertEqual(control.requests[0]['params']['env']['CLIENT_INFO_PRODUCT_VERSION'], '5.8.3')
        self.assertEqual([x['method'] for x in data.requests], ['initialize', 'session/new'])

    def test_local_only_still_blocks_before_any_prompt(self):
        client, _, data = self.client()
        client.connect()
        with self.assertRaises(ValueError):
            client.chat('deepseek-v4.1-flash', 'test', {'paid_allowed': True, 'model_scope': 'local_only'})
        self.assertEqual([x['method'] for x in data.requests], ['initialize', 'session/new'])

    def test_unknown_native_create_is_not_retried(self):
        pipe = FakeControl(self.root, 'unknown')
        control = host.LeaseControl(pipe, 'modeldesk-wb-' + '1' * 32)
        params = {'sessionId': control.session_id}
        for _ in range(2):
            with self.assertRaises(wb.OutcomeUnknown):
                control.request('session.create', params)
        self.assertEqual(len(pipe.requests), 1)

    def test_foreign_session_is_not_attached_or_killed(self):
        with self.assertRaises(wb.OutcomeUnknown):
            self.client('foreign')

    def test_native_error_does_not_expose_private_message(self):
        with self.assertRaises(host.HostUnavailable) as error:
            self.client('rejected')
        self.assertNotIn('private-error', str(error.exception))

    def test_cleanup_requires_exact_session_and_runtime(self):
        client, control, _ = self.client()
        client.process.terminate()
        request = control.requests[-1]
        self.assertEqual(request['method'], 'session.kill')
        self.assertEqual(request['params']['sessionId'], client.process.control.session_id)
        self.assertEqual(request['params']['expectedRuntimeId'], client.process.control.runtime_id)
        with self.assertRaises(ValueError):
            client.process.terminate()

    def test_foreign_cleanup_is_rejected_before_writing(self):
        pipe = FakeControl(self.root)
        control = host.LeaseControl(pipe, 'modeldesk-wb-' + '1' * 32)
        control.runtime_id = '11111111-1111-4111-8111-111111111111'
        for params in [{'sessionId': 'modeldesk-wb-' + '2' * 32, 'expectedRuntimeId': control.runtime_id},
                       {'sessionId': control.session_id, 'expectedRuntimeId': 'another-runtime'}]:
            with self.subTest(params=params), self.assertRaises(ValueError):
                control.request('session.kill', params)
        self.assertEqual(pipe.requests, [])

    def test_unknown_cleanup_releases_connections_without_retry(self):
        client, control, data = self.client()
        data.api.WaitForSingleObject.return_value = 258
        control.scenario = 'unknown'
        with self.assertRaises(wb.OutcomeUnknown):
            client.close()
        self.assertEqual([x['method'] for x in control.requests], ['session.create', 'session.kill'])
        self.assertTrue(control.closed)
        self.assertTrue(data.closed)
        self.assertIsNone(client.process.child_handle)


if __name__ == '__main__':
    unittest.main()
