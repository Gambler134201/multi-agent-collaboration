"""Real local HTTP guard + MCP proxy; isolated settings, no model calls."""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_mcp_server as desk
import model_registry as registry
import panel_server as panel
import workflow_settings as settings


class SessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='md-session-recovery-')
        self.saved = registry.HERE, registry.REGISTRY, registry.RUNS
        registry.HERE = Path(self.temp.name)
        registry.REGISTRY, registry.RUNS = registry.HERE / 'models.json', registry.HERE / 'runs'
        self.project = registry.HERE / 'project'
        self.project.mkdir()
        self.original = settings.save_settings({'enabled': False, 'execution_agent': 'dsh',
                         'paid_allowed': False, 'model_scope': 'local_only',
                         'enforce_paid_budget': False, 'enforce_model_call_limit': False})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.patch_url = patch.object(desk, 'API_BASE', self.url)
        self.patch_token = patch.object(panel, 'TOKEN', 'fixture-before-restart')
        self.patch_url.start(); self.patch_token.start()
        desk._TOKEN['value'] = None

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.patch_url.stop(); self.patch_token.stop()
        desk._TOKEN['value'] = None
        registry.HERE, registry.REGISTRY, registry.RUNS = self.saved
        self.temp.cleanup()

    def test_rotated_server_credential_recovers_without_duplicate_settings_write(self):
        view = {'mode': 'project', 'project': str(self.project)}
        self.assertEqual(desk.panel_call('/api/workflow', 'POST', {'execution_agent': 'dsh'}, view)['status'], 200)
        panel.TOKEN = 'fixture-after-restart'
        calls = []
        real_call, real_save = desk.http_call, settings.save_settings
        def tracking(method, path, **kwargs):
            status, data = real_call(method, path, **kwargs)
            calls.append((method, path, status, kwargs.get('view')))
            return status, data
        with patch.object(desk, 'http_call', side_effect=tracking), \
             patch.object(settings, 'save_settings', side_effect=real_save) as save:
            result = desk.panel_call('/api/workflow', 'POST', {'execution_agent': 'pi'}, view)
        self.assertEqual(result['status'], 200)
        save.assert_called_once_with({'execution_agent': 'pi'})
        self.assertEqual([c[:3] for c in calls], [('POST','/api/workflow',403),('GET','/api/session',200),('POST','/api/workflow',200)])
        self.assertEqual([c[3] for c in calls if c[0] == 'POST'], [view, view])
        current = settings.snapshot()
        self.assertEqual(current, {**self.original, 'execution_agent':'pi'})
        self.assertNotIn('fixture-after-restart', json.dumps(result))
        self.assertNotIn('fixture-before-restart', json.dumps(result))

    def test_expired_session_code_only_precedes_the_mutation_handler(self):
        real_save = settings.save_settings
        request = Request(self.url + '/api/workflow', data=b'{"execution_agent":"pi"}',
                          headers={'X-Panel-Token':'expired-fixture','Content-Type':'application/json'})
        with patch.object(settings,'save_settings',side_effect=real_save) as save:
            with self.assertRaises(HTTPError) as failure:
                build_opener(ProxyHandler({})).open(request)
        self.assertEqual(failure.exception.code,403)
        self.assertEqual(json.load(failure.exception)['code'],'panel_session_expired')
        save.assert_not_called()
        self.assertEqual(settings.snapshot(),self.original)

    def test_wrong_origin_is_not_a_refreshable_credential_failure(self):
        request = Request(self.url + '/api/workflow',data=b'{}',headers={
            'X-Panel-Token':panel.TOKEN,'Content-Type':'application/json','Origin':'https://untrusted.invalid'})
        with self.assertRaises(HTTPError) as failure:
            build_opener(ProxyHandler({})).open(request)
        self.assertEqual(failure.exception.code,403)
        self.assertNotIn('code',json.load(failure.exception))
        self.assertEqual(settings.snapshot(),self.original)


if __name__ == '__main__': unittest.main()
