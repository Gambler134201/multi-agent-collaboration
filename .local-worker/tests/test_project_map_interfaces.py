from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_context
import brain_gateway
import brain_transport
import codex_mcp_server as codex
import panel_server as panel
import project_map
import project_scope


class MapInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='map-interface-')
        self.a = Path(self.temp.name) / 'a'
        self.b = Path(self.temp.name) / 'b'
        self.a.mkdir(); self.b.mkdir()
        (self.a / 'a.py').write_text('def only_a(): pass')
        (self.b / 'b.py').write_text('def only_b(): pass')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        project_map._CACHE.clear()
        self.temp.cleanup()

    def post(self, view, body, with_token=True):
        headers = {'Content-Type':'application/json'}
        if with_token:
            headers['X-Panel-Token'] = panel.TOKEN  # fresh fixture process memory, never stored authentication
        if view is not None:
            headers['X-Model-Desk-View'] = project_scope.encode_view_header(view)
        request = Request(self.url + '/api/project-map', json.dumps(body).encode(), headers, method='POST')
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.code, json.load(response)

    def test_project_view_api_rejects_missing_all_or_project_override(self):
        for view in (None, {'mode':'unbound','project':None}, {'mode':'all','project':None}):
            status, _ = self.post(view, {})
            self.assertEqual(status, 400)
        view = {'mode':'project', 'project':str(self.a)}
        self.assertEqual(self.post(view, {'project':str(self.b)})[0], 400)
        self.assertEqual(self.post(view, {}, with_token=False)[0], 403)
        self.assertEqual(self.post(view, {'file':'../b/b.py'})[0], 400)

    def test_api_a_b_switch_stays_scoped_and_creates_no_records(self):
        for directory, expected in ((self.a, 'a.py'), (self.b, 'b.py'), (self.a, 'a.py')):
            status, data = self.post({'mode':'project','project':str(directory)}, {})
            self.assertEqual(status, 200)
            self.assertEqual(data['project'], str(directory))
            self.assertEqual([node['path'] for node in data['nodes']], [expected])
            self.assertTrue(data['read_only'])
            self.assertFalse((directory / '.model-desk').exists())

    def test_brain_hosts_use_bound_project_and_read_does_not_need_workflow_on(self):
        for host in ('codex', 'dsh'):
            context = brain_context.build_context(str(self.a), host, 'map-test-owner',
                role='brain', session_binding='user_bound', operator_session='map-test')
            connection = brain_gateway.register_connection(context.declaration())
            cap = connection['capability']
            try:
                with mock.patch('workflow_settings.snapshot', return_value={'enabled':False}), \
                     mock.patch.object(panel.EXECUTOR, 'submit', side_effect=AssertionError('No executor work')):
                    data = brain_gateway.handle('project_map', {'query':'only_a'}, cap)
                self.assertEqual(data['project'], str(self.a))
                self.assertEqual(data['nodes'][0]['path'], 'a.py')
                with self.assertRaises(ValueError):
                    brain_gateway.handle('project_map', {'project':str(self.b)}, cap)
                self.assertTrue(brain_transport._valid_result('project_map', data))
            finally:
                with brain_gateway._LOCK:
                    brain_gateway._CAPABILITIES.pop(cap, None)

    def test_codex_entry_is_offline_and_does_not_fall_back_to_last_project(self):
        with mock.patch.object(codex, 'ensure_service', side_effect=AssertionError('No service')):
            result = codex.read_project_map({'mode':'project','project':str(self.b)}, {'query':'only_b'})
        self.assertEqual(result['nodes'][0]['path'], 'b.py')
        for view in (None, {'mode':'all','project':None}, {'mode':'unbound','project':None}):
            with self.assertRaises(ValueError):
                codex.read_project_map(view, {})
        tool = next(tool for tool in codex.tool_definitions() if tool.name == codex.TOOL_MAP)
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertEqual(codex.validate_request('/api/project-map', 'POST'), '/api/project-map')

    def test_real_codex_stdio_tool_returns_visible_json_without_controller(self):
        import test_brain_mcp_server as protocol
        server = str(Path(codex.__file__))
        with mock.patch.object(protocol, 'SERVER', server):
            client = protocol.StdioClient([])
        try:
            client.initialize()
            tool = next(item for item in client.request('tools/list')['result']['tools'] if item['name'] == 'query_project_map')
            self.assertTrue(tool['annotations']['readOnlyHint'])
            result = client.call('query_project_map', {'project':str(self.a), 'query':'only_a'})
            self.assertFalse(result['isError'])
            visible = protocol.visible_payload(result)
            self.assertEqual(visible['nodes'][0]['path'], 'a.py')
            self.assertFalse((self.a / '.model-desk').exists())
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
