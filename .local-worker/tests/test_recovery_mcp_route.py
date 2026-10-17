"""Recovery stays a concrete, app-only MCP proxy route with no URL escape."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_mcp_server as desk


class RecoveryProxyRouteTests(unittest.TestCase):
    def test_only_concrete_post_recovery_route_is_allowed(self):
        path = '/api/tasks/task-1_a/recover'
        self.assertEqual(path, desk.validate_request(path, 'POST'))
        for method, candidate in (
            ('GET', path), ('DELETE', path), ('POST', path + '?project=other'),
            ('POST', '/api/tasks/../recover'), ('POST', '/api/tasks/a/b/recover'),
            ('POST', 'http://localhost:8765' + path), ('POST', path + '/'),
        ):
            with self.subTest(method=method, path=candidate):
                with self.assertRaises(desk.PanelError):
                    desk.validate_request(candidate, method)

    def test_recovery_does_not_make_proxy_model_visible(self):
        tool = desk.tool_definitions()[1].model_dump(by_alias=True, mode='json')
        self.assertEqual(['app'], tool['_meta']['ui']['visibility'])
