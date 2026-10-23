"""Real JSON-RPC stdio MCP handshake tests plus a full mock protocol loop.

The MCP server runs in a separate process over real stdio.  In the loop test the
brain MCP talks real HTTP to the real panel Handler on an ephemeral loopback
port with a temporary project registry.  Only executor submission is mocked; no
provider, desktop message, account or native host is touched.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import brain_gateway
import brain_requests
import brain_mcp_server as brain
import delegation_policy as policy
import model_registry as registry
import panel_server as panel

SERVER = str(HERE / 'brain_mcp_server.py')


class CountingHandler(BaseHTTPRequestHandler):
    hits = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        type(self).hits.append(('GET', self.path))
        self._json(503, {'error': 'not used by listing tests'})

    def do_POST(self):
        type(self).hits.append(('POST', self.path))
        self._json(503, {'error': 'not used by listing tests'})

    def _json(self, code, data):
        raw = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class StdioClient:
    """Minimal real JSON-RPC 2.0 client over the MCP process's stdio."""

    def __init__(self, args, timeout=15, *, cwd=None, env=None, registry_home=None):
        command=[sys.executable,SERVER,*args]
        if registry_home is not None:
            # Test-only bootstrap redirects local switch reads into the same
            # temporary fixture as the mock broker, never the live settings.
            code=('import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
                  'import model_registry as r;r.HERE=Path(sys.argv[2]);'
                  'r.REGISTRY=r.HERE/"models.json";r.RUNS=r.HERE/"runs";'
                  'import brain_mcp_server as b;b.main(sys.argv[3:])')
            command=[sys.executable,'-c',code,str(HERE),str(registry_home),*args]
        self.proc = subprocess.Popen(
            command, cwd=str(cwd or HERE), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', bufsize=1)
        self.messages = queue.Queue()
        self.stderr = []
        self.timeout = timeout
        self.next_id = 1
        self.tool_responses = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put(None)

    def _read_stderr(self):
        try:
            for line in self.proc.stderr:
                self.stderr.append(line.rstrip())
        except (OSError, ValueError):
            pass

    def send(self, message):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        message = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            message['params'] = params
        self.send(message)

    def request(self, method, params=None):
        request_id = self.next_id
        self.next_id += 1
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method,
                   'params': params if params is not None else {}})
        while True:
            try:
                message = self.messages.get(timeout=self.timeout)
            except queue.Empty:
                raise AssertionError('MCP stdio timeout; stderr=%r' % self.stderr[-5:])
            if message is None:
                raise AssertionError('MCP server exited; stderr=%r' % self.stderr[-10:])
            if message.get('id') == request_id:
                return message

    def initialize(self):
        result = self.request('initialize', {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'brain-stdio-test', 'version': '1'},
        })
        self.notify('notifications/initialized')
        return result

    def list_tools(self):
        return self.request('tools/list')['result']['tools']

    def call(self, name, arguments=None):
        response = self.request('tools/call', {'name': name, 'arguments': arguments or {}})
        if 'error' in response:
            raise AssertionError('JSON-RPC error: %r' % response['error'])
        result = response['result']
        # SDK input-schema validation can reject a call before our handler and
        # returns its own plain-text error. Our results always have public data.
        if 'structuredContent' in result or not result.get('isError'):
            visible_payload(result)
        self.tool_responses.append({'name': name, 'result': result})
        return result

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=4)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


def payload(result):
    return result.get('structuredContent') or {}


def text(result):
    return ' '.join(part.get('text', '') for part in result.get('content') or [])


def visible_payload(result):
    """Require complete JSON in the text path, not just structuredContent."""
    blocks = result.get('content') or []
    if len(blocks) != 2 or blocks[-1].get('type') != 'text':
        raise AssertionError('Expected summary plus public JSON text block')
    data = json.loads(blocks[-1]['text'])
    if data != payload(result):
        raise AssertionError('Model-visible text differs from structured content')
    return data


def assert_native_projection(case, responses):
    """Opt-in installed DSH client renderer check; no profile or model calls."""
    executable = os.environ.get('MODEL_DESK_TEST_DSH_EXE')
    if not executable:
        return
    archive_entry = (Path(executable).resolve().parent / 'resources' / 'app.asar' /
                     'dsh/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js')
    script = Path(__file__).with_name('test_brain_text_projection.cjs')
    with tempfile.TemporaryDirectory() as folder:
        fixture = Path(folder) / 'responses.json'
        fixture.write_text(json.dumps(responses, ensure_ascii=False), encoding='utf-8')
        result = subprocess.run([executable, '--expose-internals', str(script),
                                 archive_entry.as_uri(), str(fixture)],
            env=dict(os.environ, ELECTRON_RUN_AS_NODE='1'),
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        case.assertEqual(result.returncode, 0,
                         'Installed DSH projection check failed: ' + result.stderr.decode('utf-8', 'replace'))
        proof = json.loads(result.stdout)
        case.assertEqual(proof['verified'], len(responses))
        case.assertFalse(proof['model_called'])


class ResultTextTests(unittest.TestCase):
    def test_complete_compact_json_and_structured_content_are_equal(self):
        data = {'context': {'project': 'E:/测试/项目'}, 'task': {'accepted': False},
                'answer': '第一行\n第二行\t"JSON"', 'empty': None}
        result = brain._ok(data, '摘要').model_dump(mode='json', exclude_none=True)
        self.assertEqual(visible_payload(result), data)
        self.assertEqual(result['content'][1]['text'],
                         json.dumps(data, ensure_ascii=False, separators=(',', ':')))

    def test_unknown_outcome_reference_survives_text_only_clients(self):
        result = brain._error('结果未知', outcome='unknown',
                              request_reference={'request_id': 'fixture-id'}).model_dump(mode='json')
        self.assertTrue(result['isError'])
        self.assertEqual(visible_payload(result)['outcome'], 'unknown')
        self.assertIn('fixture-id', text(result))

    def test_delivery_answer_is_not_silently_cut_by_the_text_mirror(self):
        data = {'answer': '证据' * 10000, 'answer_truncated': True,
                'accepted': False, 'delivery_state': 'executor_completed_unaccepted'}
        result = brain._ok(data, '交付').model_dump(mode='json')
        self.assertEqual(visible_payload(result), data)


class PreflightTests(unittest.TestCase):
    def test_readiness_signal_needs_switch_binding_and_consistent_dispatch_candidate(self):
        with tempfile.TemporaryDirectory() as folder:
            context = brain.brain_context.build_context(folder, 'dsh', 'fixture',
                operator_session='fixture-chat', session_binding='user_bound')
            workflow = {'readable': True, 'enabled': True, 'model_scope': 'local_only'}
            candidates = {'dispatch_ready_model_ids': ['home'], 'models': [
                {'model_id': 'home', 'name': 'Qwen\n不应变成新行', 'dispatch_ready': True}]}
            signal = brain._preflight_signal(context, workflow, candidates)
            self.assertEqual(signal['state'], 'ready')
            self.assertTrue(signal['text'].startswith('🟢 多 Agent 协作已就绪'))
            self.assertNotIn('\n', signal['text'])
            self.assertIn(context.project, signal['text'])
            self.assertEqual(brain._preflight_signal(context, {**workflow, 'readable': False}, candidates)['state'], 'unavailable')
            self.assertEqual(brain._preflight_signal(context, {**workflow, 'enabled': False}, candidates)['state'], 'disabled')
            self.assertEqual(brain._preflight_signal(context, workflow, {'models': candidates['models']})['state'], 'no_executor')
            unknown = brain.brain_context.build_context(folder, 'dsh', 'fixture')
            self.assertEqual(brain._preflight_signal(unknown, workflow, candidates)['state'], 'unavailable')
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_explicit_native_preflight_can_bootstrap_and_return_disabled_switch_honestly(self):
        with tempfile.TemporaryDirectory() as folder:
            context = brain.brain_context.build_context(folder, 'dsh', 'fixture',
                operator_session='fixture-chat', session_binding='user_bound')
            candidates = {'workflow': {'readable': True, 'enabled': False}, 'models': []}
            with mock.patch.object(brain, '_CONTEXT', context), \
                 mock.patch.object(brain, '_ALLOW_SERVICE_START', True), \
                 mock.patch.object(brain, '_connected', return_value=False), \
                 mock.patch.object(brain, '_connect') as connect, \
                 mock.patch.object(brain, '_call', return_value=candidates), \
                 mock.patch('panel_service.ensure_service') as start:
                result = brain._run('brain_preflight', {})
            data = result.structuredContent
            self.assertFalse(data['dispatch_allowed_by_switch'])
            self.assertFalse(data['task_dispatched'])
            self.assertFalse(data['model_called'])
            self.assertEqual(data['user_signal']['state'], 'disabled')
            self.assertIn('协作已关闭', data['user_signal']['text'])
            self.assertEqual(list(Path(folder).iterdir()), [])
            start.assert_called_once()
            connect.assert_called_once()

    def test_ordinary_preflight_does_not_start_service(self):
        with tempfile.TemporaryDirectory() as folder:
            context = brain.brain_context.build_context(folder, 'other-host', 'fixture')
            with mock.patch.object(brain, '_CONTEXT', context), \
                 mock.patch.object(brain, '_ALLOW_SERVICE_START', False), \
                 mock.patch.object(brain, '_connected', return_value=False), \
                 mock.patch.object(brain, '_connect'), \
                 mock.patch.object(brain, '_call', return_value={'workflow': {'enabled': True, 'readable': True}}), \
                 mock.patch('panel_service.ensure_service') as start:
                brain._run('brain_preflight', {})
            start.assert_not_called()

    def test_old_backend_without_workflow_snapshot_is_not_inferred_enabled(self):
        with mock.patch.object(brain, '_connected', return_value=True), \
             mock.patch.object(brain, '_call', return_value={'models': []}), \
             self.assertRaisesRegex(ValueError, '缺少预检状态'):
            brain._run('brain_preflight', {})


class StdioProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.tmp.cleanup()

    def start(self, panel_url, extra=()):
        args = ['--project', str(self.project), '--host', 'test-host',
                '--owner', 'main-a', '--role', 'brain',
                '--session-binding', 'user_bound', '--session-id', 's-a',
                '--panel-url', panel_url, *extra]
        client = StdioClient(args)
        self.clients.append(client)
        return client

    def test_initialize_and_listing_do_no_io_and_expose_fixed_tools(self):
        CountingHandler.hits = []
        server = ThreadingHTTPServer(('127.0.0.1', 0), CountingHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = self.start('http://127.0.0.1:%d' % server.server_address[1])
            initialized = client.initialize()
            self.assertEqual(initialized['result']['serverInfo']['name'], 'model-desk-brain')
            tools = {tool['name']: tool for tool in client.list_tools()}
            self.assertEqual(set(tools), set(brain.TOOLS))
            self.assertEqual(len(tools), 11)
            self.assertTrue(tools['brain_request_status']['annotations']['readOnlyHint'])
            for name, tool in tools.items():
                self.assertFalse(tool['inputSchema'].get('additionalProperties', True),
                                 name)
                for forbidden in ('project', 'owner', 'role', 'actor', 'engine', 'session'):
                    self.assertNotIn(forbidden, tool['inputSchema']['properties'], (name, forbidden))
            self.assertEqual(CountingHandler.hits, [])
            described = client.call('brain_describe')
            self.assertFalse(payload(described)['connected'])
            self.assertEqual(payload(described)['context']['owner']['id'], 'main-a')
            self.assertEqual(CountingHandler.hits, [])
            refused = client.call('brain_candidates')
            self.assertTrue(refused['isError'])
            self.assertIn('brain_connect', text(refused))
            self.assertEqual(CountingHandler.hits, [])
            extra = client.call('brain_dispatch', {
                'request_id': 'x', 'prompt': 'p', 'acceptance': ['a'], 'project': '/tmp/x'})
            self.assertTrue(extra['isError'])
            self.assertIn('project', text(extra))
            self.assertEqual(CountingHandler.hits, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_connect_to_missing_service_is_clear_and_never_starts_one(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        client = self.start('http://127.0.0.1:%d' % port)
        client.initialize()
        result = client.call('brain_connect')
        self.assertTrue(result['isError'])
        self.assertIn('未自动启动服务', text(result))

    def test_invalid_launch_context_fails_closed_without_protocol_output(self):
        for args in (
            ['--project', 'relative', '--host', 'h', '--owner', 'o', '--role', 'brain'],
            ['--project', str(self.project), '--host', 'h', '--owner', 'o', '--role', 'brain',
             '--session-binding', 'host_verified'],
        ):
            process = subprocess.run([sys.executable, SERVER, *args], cwd=str(HERE),
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                     encoding='utf-8', errors='replace', timeout=30)
            self.assertNotEqual(process.returncode, 0)
            self.assertTrue(process.stderr.strip())
            self.assertEqual(process.stdout.strip(), '')


class EndToEndLoopTests(unittest.TestCase):
    def test_request_lookup_over_stdio_http_for_both_hosts_after_receipt_loss(self):
        responses = []
        for host in ('codex', 'dsh'):
            with self.subTest(host=host):
                client = StdioClient([
                    '--project',str(self.project),'--host',host,'--owner','main-a',
                    '--role','brain','--session-binding','user_bound','--session-id','s-a',
                    '--panel-url','http://127.0.0.1:%d'%self.server.server_address[1]],
                    registry_home=registry.HERE)
                try:
                    client.initialize()
                    self.assertFalse(client.call('brain_connect')['isError'])
                    req = 'stdio-lost-' + host
                    with mock.patch.object(brain_requests, 'complete', side_effect=OSError('fixture disk full')):
                        lost = client.call('brain_dispatch', {'request_id':req,'prompt':'Mock only',
                            'kind':'edit','files':['a.py'],'acceptance':['criterion']})
                    self.assertTrue(lost['isError'])
                    self.assertEqual(payload(lost)['outcome'], 'unknown')
                    submitted = list(self.submitted)
                    found = client.call('brain_request_status', {'request_id':req})
                    data = visible_payload(found)
                    self.assertFalse(found['isError'])
                    self.assertEqual(data['request_state'], 'task_found')
                    self.assertEqual(data['task']['task_id'], submitted[-1])
                    self.assertEqual(data['journal_state'], 'unknown')
                    self.assertEqual(self.submitted, submitted)
                    self.assertFalse(data['safe_to_redispatch'])
                    responses.append({'name':'brain_request_status','result':found})
                finally:
                    client.close()
        assert_native_projection(self, responses)

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
        self.client = StdioClient([
            '--project', str(self.project), '--host', 'test-host', '--owner', 'main-a',
            '--role', 'brain', '--session-binding', 'user_bound', '--session-id', 's-a',
            '--panel-url', 'http://127.0.0.1:%d' % self.server.server_address[1],
        ])
        self.client.initialize()

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for patch in reversed(self.patches):
            patch.stop()
        brain_gateway._CAPABILITIES.clear()
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.tmp.cleanup()

    def complete(self, task_id, answer='mock deliverable'):
        run = registry.task_dir(task_id, str(self.project))
        registry.patch_task(run, status='completed', phase='已完成', ended_at=registry.now())
        (run / 'answer.txt').write_text(answer, encoding='utf-8')
        policy.record_executor(run, {'completed': True, 'changed_files': ['a.py'],
                                     'test': {'exit_code': 0, 'output': 'ok'}})
        return run

    def evidence(self):
        return [{'id': 'c1', 'status': 'pass',
                 'evidence': [{'source': 'reviewer_test', 'detail': 'reran the test',
                               'exit_code': 0}]}]

    def test_new_chat_can_read_history_but_cannot_review_or_rework(self):
        self.client.call('brain_connect')
        dispatched=self.client.call('brain_dispatch',{
            'request_id':'history-1','prompt':'Fixture','kind':'edit',
            'model_id':'mock','files':['a.py'],'acceptance':['criterion one']})
        task_id=payload(dispatched)['task']['task_id']
        run=self.complete(task_id)
        before={name:(run/name).read_bytes() for name in ('task.json','review.json')}
        other=StdioClient(['--project',str(self.project),'--host','different-host','--owner','new-brain',
                          '--role','brain','--session-binding','user_bound','--session-id','new-chat',
                          '--panel-url','http://127.0.0.1:%d'%self.server.server_address[1]])
        try:
            other.initialize()
            self.assertFalse(other.call('brain_connect')['isError'])
            for name in ('brain_status','brain_delivery'):
                result=other.call(name,{'task_id':task_id})
                self.assertFalse(result['isError'])
                self.assertEqual(visible_payload(result)['access']['mode'],'project_read_only')
                self.assertEqual(payload(result)['owner'],'main-a')
            self.assertTrue(other.call('brain_review',{'task_id':task_id,'request_id':'foreign-review',
                                                     'action':'block','notes':'must reject'})['isError'])
            self.assertTrue(other.call('brain_rework',{'parent_task_id':task_id,'request_id':'foreign-rework'})['isError'])
        finally:
            other.close()
        self.assertEqual(self.submitted,[task_id])
        for name,data in before.items():
            self.assertEqual((run/name).read_bytes(),data)

    def test_attention_task_status_error_is_visible_without_dispatch_or_review(self):
        self.client.call('brain_connect')
        dispatched = self.client.call('brain_dispatch', {
            'request_id': 'attention-status', 'prompt': 'Fixture only', 'kind': 'edit',
            'model_id': 'mock', 'files': ['a.py'], 'acceptance': ['criterion one']})
        task_id = payload(dispatched)['task']['task_id']
        run = registry.task_dir(task_id, str(self.project))
        for state in ('failed', 'cancelled', 'recovery_pending'):
            error = 'fixture error: ' + state
            registry.patch_task(run, status=state, error=error)
            before = (run / 'task.json').read_bytes()
            for tool in ('brain_status', 'brain_delivery'):
                result = self.client.call(tool, {'task_id': task_id})
                self.assertFalse(result['isError'], text(result))
                self.assertEqual(visible_payload(result)['execution_error'], error)
                self.assertEqual(visible_payload(result), payload(result))
                if tool == 'brain_status':
                    self.assertEqual(payload(result)['error'], error)
                else:
                    self.assertFalse(payload(result)['accepted'])
            self.assertEqual((run / 'task.json').read_bytes(), before)
        self.assertEqual(self.submitted, [task_id])
        assert_native_projection(self, self.client.tool_responses)

    def test_full_mock_loop_connect_preview_dispatch_deliver_return_rework_accept(self):
        described = self.client.call('brain_describe')
        self.assertEqual(visible_payload(described)['context']['role'], 'brain')
        connected = self.client.call('brain_connect')
        self.assertFalse(connected['isError'])
        self.assertTrue(payload(connected)['connected'])
        preflight = visible_payload(self.client.call('brain_preflight'))
        self.assertTrue(preflight['workflow']['enabled'])
        self.assertEqual(preflight['context']['project'], str(self.project))
        self.assertEqual(self.submitted, [])
        self.assertNotIn('capability', json.dumps(payload(connected)).lower())

        candidates = payload(self.client.call('brain_candidates'))
        self.assertEqual(candidates['models'][0]['model_id'], 'mock')
        self.assertEqual(candidates['models'][0]['engine'], 'dsh')

        preview = payload(self.client.call('brain_route_preview', {
            'model_id': 'mock', 'prompt': 'work', 'kind': 'edit',
            'files': ['a.py'], 'acceptance': ['criterion one']}))
        self.assertEqual(preview['preview']['engine'], 'dsh')
        self.assertEqual(self.submitted, [])

        dispatched = self.client.call('brain_dispatch', {
            'request_id': 'loop-dispatch', 'prompt': 'Do the work', 'kind': 'edit',
            'model_id': 'mock', 'files': ['a.py'], 'acceptance': ['criterion one']})
        self.assertFalse(dispatched['isError'])
        task_id = payload(dispatched)['task']['task_id']
        self.assertEqual(self.submitted, [task_id])
        duplicate = self.client.call('brain_dispatch', {
            'request_id': 'loop-dispatch', 'prompt': 'Do the work', 'kind': 'edit',
            'model_id': 'mock', 'files': ['a.py'], 'acceptance': ['criterion one']})
        self.assertEqual(payload(duplicate)['task']['task_id'], task_id)
        self.assertEqual(self.submitted, [task_id])

        request = visible_payload(self.client.call('brain_request_status', {'request_id':'loop-dispatch'}))
        self.assertEqual(request['request_state'], 'completed')
        self.assertEqual(request['task']['task_id'], task_id)
        self.assertEqual(self.submitted, [task_id])

        status = payload(self.client.call('brain_status', {'task_id': task_id}))
        self.assertEqual(status['status'], 'queued')
        self.assertEqual(self.submitted, [task_id])
        registry.patch_task(registry.task_dir(task_id, str(self.project)), status='running',
                            phase='执行中', started_at=registry.now())
        running = payload(self.client.call('brain_status', {'task_id': task_id}))
        self.assertEqual(running['status'], 'running')
        self.assertEqual(self.submitted, [task_id])

        self.complete(task_id, 'defective deliverable')
        delivery = payload(self.client.call('brain_delivery', {'task_id': task_id}))
        self.assertEqual(delivery['delivery_state'], 'executor_completed_unaccepted')
        self.assertFalse(delivery['accepted'])
        self.assertIn('defective deliverable', delivery['answer'])

        returned = self.client.call('brain_review', {
            'request_id': 'loop-return', 'task_id': task_id, 'action': 'return',
            'failed_criteria': ['c1'], 'problems': 'observed defective result',
            'boundaries': 'a.py only'})
        self.assertFalse(returned['isError'], text(returned))
        self.assertEqual(payload(returned)['receipt']['summary']['state'], policy.RETURNED)

        rework = self.client.call('brain_rework', {
            'request_id': 'loop-rework', 'parent_task_id': task_id})
        self.assertFalse(rework['isError'], text(rework))
        child_id = payload(rework)['task']['task_id']
        self.assertNotEqual(child_id, task_id)
        self.assertEqual(self.submitted, [task_id, child_id])
        same = self.client.call('brain_rework', {
            'request_id': 'loop-rework', 'parent_task_id': task_id})
        self.assertEqual(payload(same)['task']['task_id'], child_id)
        self.assertEqual(self.submitted, [task_id, child_id])
        second_slot = self.client.call('brain_rework', {
            'request_id': 'loop-rework-2', 'parent_task_id': task_id})
        self.assertTrue(second_slot['isError'])
        self.assertEqual(self.submitted, [task_id, child_id])

        self.complete(child_id, 'corrected deliverable')
        accepted = self.client.call('brain_review', {
            'request_id': 'loop-accept', 'task_id': child_id, 'action': 'accept',
            'criteria': self.evidence()})
        self.assertFalse(accepted['isError'], text(accepted))
        self.assertEqual(payload(accepted)['receipt']['summary']['state'], policy.ACCEPTED)
        repeat = self.client.call('brain_review', {
            'request_id': 'loop-accept', 'task_id': child_id, 'action': 'accept',
            'criteria': self.evidence()})
        self.assertTrue(payload(repeat)['reused'])
        final = payload(self.client.call('brain_delivery', {'task_id': child_id}))
        self.assertTrue(final['accepted'])
        self.assertEqual(final['delivery_state'], 'accepted')
        # No dispatch happened on status/delivery/review reads beyond the two rounds.
        self.assertEqual(len(self.submitted), 2)
        # Controller capability/panel token never leak into the protocol payload.
        everything = json.dumps([payload(dispatch) for dispatch in
                                 (connected, preview, dispatched, status, delivery, returned,
                                  rework, accepted, final)], ensure_ascii=False)
        self.assertNotIn('X-Brain-Capability', everything)
        self.assertNotIn('panel_token', everything.lower())
        # Check BOTH representations against the private in-memory capability.
        # The actual DSH renderer is exercised when explicitly configured.
        results = json.dumps(self.client.tool_responses, ensure_ascii=False)
        for capability in brain_gateway._CAPABILITIES:
            self.assertNotIn(capability, results)
        self.assertEqual({entry['name'] for entry in self.client.tool_responses}, set(brain.TOOLS))
        assert_native_projection(self, self.client.tool_responses)


if __name__ == '__main__':
    unittest.main()
