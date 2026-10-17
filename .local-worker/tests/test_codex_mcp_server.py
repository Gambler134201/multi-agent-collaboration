"""Mock-only tests for the Model Desk MCP backend.

No real panel restart, no model or paid generation, no user setting changes.
"""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import codex_mcp_server as desk
from mcp import types as mcp_types


def fingerprint(paths):
    rows = {}
    for path in paths:
        if not path.exists():
            rows[str(path)] = None
            continue
        raw = path.read_bytes()
        rows[str(path)] = (hashlib.sha256(raw).hexdigest()[:16], len(raw), path.stat().st_mtime_ns)
    return rows


class MetadataTests(unittest.TestCase):
    def dump(self, item):
        return item.model_dump(by_alias=True, mode="json", exclude_none=True)

    def test_tool_definitions_and_entrypoints(self):
        tools = {tool.name: self.dump(tool) for tool in desk.tool_definitions()}
        self.assertEqual(set(tools), {desk.TOOL_OPEN, desk.TOOL_PANEL})
        opened = tools[desk.TOOL_OPEN]
        meta = opened["_meta"]
        self.assertEqual(meta["ui"]["resourceUri"], desk.RESOURCE_URI)
        self.assertEqual(meta["ui"]["visibility"], ["model", "app"])
        self.assertEqual(meta["openai/ui"]["entrypoints"], [{"type": "global"}, {"type": "thread"}])
        self.assertEqual(meta["openai/outputTemplate"], desk.RESOURCE_URI)
        self.assertFalse(opened['inputSchema']['additionalProperties'])
        self.assertEqual(opened['inputSchema']['properties']['project']['type'],'string')
        self.assertNotIn('required',opened['inputSchema'])
        self.assertFalse(opened["annotations"]["destructiveHint"])
        self.assertTrue(opened["annotations"]["idempotentHint"])

    def test_panel_tool_is_app_only_without_entrypoint(self):
        panel = self.dump(desk.tool_definitions()[1])
        self.assertEqual(panel["_meta"], {"ui": {"visibility": ["app"]}})
        self.assertNotIn("openai/ui", panel["_meta"])
        self.assertNotIn("openai/outputTemplate", panel["_meta"])
        self.assertEqual(panel["inputSchema"]["required"], ["path", "method"])

    def test_resource_definition_metadata(self):
        resource = self.dump(desk.resource_definitions()[0])
        self.assertEqual(resource["uri"], desk.RESOURCE_URI)
        self.assertEqual(resource["mimeType"], desk.UI_MIME)
        self.assertFalse(resource["_meta"]["ui"]["prefersBorder"])
        self.assertEqual(resource["_meta"]["ui"]["csp"]["connectDomains"], [])
        self.assertEqual(resource["_meta"]["ui"]["csp"]["resourceDomains"], [])
        self.assertEqual(resource["_meta"]["ui"]["csp"]["frameDomains"], [])


class RouteTests(unittest.TestCase):
    def test_allowed_routes(self):
        for path in ("/api/state", "/api/workflow", "/api/codex-usage", "/api/session",
                     "/api/tasks/task-1", "/api/tasks/task_1/log"):
            self.assertEqual(desk.validate_request(path, "GET"), path)
        for path in ("/api/workflow", "/api/codex-usage/refresh", "/api/route-preview", "/api/models",
                     "/api/discover", "/api/tasks", "/api/models/m1/save", "/api/models/m1/delete",
                     "/api/models/m1/test", "/api/models/m1/default", "/api/models/m1/balance",
                     "/api/models/m1/thinking", "/api/tasks/t1/cancel"):
            self.assertEqual(desk.validate_request(path, "POST"), path)

    def test_rejected_routes(self):
        for method, path in (("GET", "/api/state?x=1"), ("GET", "/api/state#frag"), ("GET", "/api/%73tate"),
                             ("GET", "http://127.0.0.1:8765/api/state"), ("GET", "//127.0.0.1/api/state"),
                             ("GET", "/api/../secret"), ("GET", "/api/state/"), ("GET", "/api/models"),
                             ("GET", "/api/tasks/a/cancel"), ("GET", "/etc/passwd"), ("GET", "/api/unknown"),
                             ("POST", "/api/session"), ("POST", "/api/state"), ("POST", "/api/models/m1/log"),
                             ("POST", "/api/tasks/a/log"), ("DELETE", "/api/state"), ("GET", "/api\\state"),
                             ("GET", "/api/tasks/a b"), ("POST", "/api/models/m1/../delete")):
            with self.subTest(method=method, path=path):
                with self.assertRaises(desk.PanelError):
                    desk.validate_request(path, method)

    def test_stage_goal_and_handover_routes_are_allowed(self):
        for path in ("/api/goals", "/api/stages", "/api/recovery", "/api/goals/goal-1",
                     "/api/stages/stage-1", "/api/tasks/t1/handover"):
            self.assertEqual(desk.validate_request(path, "GET"), path)
        for path in ("/api/goals", "/api/stages", "/api/goals/goal-1/status",
                     "/api/stages/stage-1/dispatch", "/api/stages/stage-1/cancel",
                     "/api/stages/stage-1/requeue", "/api/stages/stage-1/sync",
                     "/api/tasks/t1/handover", "/api/tasks/t1/reference"):
            self.assertEqual(desk.validate_request(path, "POST"), path)

    def test_stage_routes_stay_concrete_not_a_generic_proxy(self):
        for method, path in (("GET", "/api/goals/goal-1/status"), ("GET", "/api/stages/stage-1/dispatch"),
                             ("POST", "/api/goals/goal-1"), ("POST", "/api/stages/stage-1/delete"),
                             ("POST", "/api/stages/stage-1"), ("POST", "/api/tasks/t1/handover/extra"),
                             ("GET", "/api/recovery/extra"), ("POST", "/api/recovery")):
            with self.subTest(method=method, path=path):
                with self.assertRaises(desk.PanelError):
                    desk.validate_request(path, method)

    def test_method_and_body_type_are_validated(self):
        with self.assertRaises(desk.PanelError):
            desk.validate_request("/api/state", None)
        with self.assertRaises(desk.PanelError):
            desk.panel_call("/api/models", "POST", ["not", "an", "object"])

    def test_body_size_limit(self):
        with mock.patch.object(desk, "panel_token", return_value="t"):
            with mock.patch.object(desk, "http_call") as call:
                with self.assertRaises(desk.PanelError):
                    desk.panel_call("/api/workflow", "POST", {"blob": "x" * (desk.MAX_BODY_BYTES + 1)})
        call.assert_not_called()


class ProxyTests(unittest.TestCase):
    def setUp(self):
        desk._TOKEN["value"] = None

    def test_endpoint_is_fixed_loopback(self):
        self.assertEqual(desk.API_BASE, "http://127.0.0.1:8765")

    def test_redirects_are_never_followed(self):
        handler = desk._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "http://example.com/"))

    def test_session_returns_dummy_token_without_http(self):
        with mock.patch.object(desk, "ensure_service", return_value=True), \
             mock.patch.object(desk, "http_call", side_effect=AssertionError("no direct call")):
            result = desk.panel_call("/api/session", "GET")
        self.assertEqual(result, {"status": 200, "data": {"token": "mcp-bridge"}})

    def test_restart_refreshes_credential_once_without_duplicate_mutation(self):
        for refusal in ({'error': '请刷新面板后重试'},
                        {'error': '请刷新面板后重试', 'code': 'panel_session_expired'}):
            with self.subTest(refusal=refusal):
                desk._TOKEN['value'] = 'old-fixture-credential'
                calls, changes = [], []
                view = {'mode': 'unbound', 'project': None}
                payload = {'execution_agent': 'pi'}
                def fake(method, path, body=None, token=None, **kwargs):
                    calls.append((method, path, body, token, kwargs.get('view')))
                    if method == 'GET': return 200, {'token': 'new-fixture-credential'}
                    if token == 'old-fixture-credential': return 403, refusal
                    changes.append(body)
                    return 200, {'ok': True}
                with mock.patch.object(desk, 'http_call', side_effect=fake):
                    result = desk.panel_call('/api/workflow', 'POST', payload, view)
                self.assertEqual(result, {'status': 200, 'data': {'ok': True}})
                self.assertEqual(changes, [payload])
                self.assertEqual([x[:2] for x in calls], [('POST','/api/workflow'),('GET','/api/session'),('POST','/api/workflow')])
                self.assertEqual([x[4] for x in calls if x[0] == 'POST'], [view, view])
                self.assertNotIn('credential', json.dumps(result))

    def test_only_one_auth_retry_and_no_retry_for_other_failures(self):
        for status, data in [(403, {'error':'请求来源不正确'}),
                             (403, {'error':'项目无权限'}),
                             (400, {'error':'请刷新面板后重试'}),
                             (500, {'code':'panel_session_expired'})]:
            desk._TOKEN['value'] = 'fixture'
            with mock.patch.object(desk,'http_call', return_value=(status, data)) as call:
                self.assertEqual(desk.panel_call('/api/tasks','POST',{}), {'status':status,'data':data})
            self.assertEqual(call.call_count, 1)
        desk._TOKEN['value'] = 'fixture'
        expired = (403, {'code':'panel_session_expired','error':'fixture refusal'})
        with mock.patch.object(desk,'http_call',side_effect=[expired,(200,{'token':'another'}),expired]) as call:
            self.assertEqual(desk.panel_call('/api/tasks','POST',{})['status'],403)
        self.assertEqual(call.call_count,3)

    def test_transport_failure_never_replays_a_mutation(self):
        desk._TOKEN['value'] = 'fixture'
        with mock.patch.object(desk,'http_call',side_effect=desk.PanelError('connection lost')) as call:
            self.assertEqual(desk.panel_call('/api/tasks','POST',{})['status'],503)
        self.assertEqual(call.call_count,1)

    def test_panel_refresh_invalidates_cached_real_credential(self):
        desk._TOKEN['value'] = 'old-fixture'
        with mock.patch.object(desk,'ensure_service',return_value=True):
            result = desk.panel_call('/api/session','GET')
        self.assertEqual(result['data'],{'token':'mcp-bridge'})
        self.assertIsNone(desk._TOKEN['value'])

    def test_concurrent_rejections_share_one_credential_refresh(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        count = 5
        barrier = threading.Barrier(count)
        reads, changes = [], []
        desk._TOKEN['value'] = 'old-fixture'
        def fake(method, path, body=None, token=None, **kwargs):
            if method == 'GET':
                reads.append(path)
                return 200, {'token': 'new-fixture'}
            if token == 'old-fixture':
                barrier.wait(timeout=5)
                return 403, {'code':'panel_session_expired'}
            changes.append(body['id'])
            return 200, {'ok': True}
        with mock.patch.object(desk,'http_call',side_effect=fake), ThreadPoolExecutor(max_workers=count) as pool:
            results=list(pool.map(lambda i:desk.panel_call('/api/tasks','POST',{'id':i}),range(count)))
        self.assertTrue(all(x['status'] == 200 for x in results))
        self.assertEqual(reads,['/api/session'])
        self.assertEqual(sorted(changes),list(range(count)))

    def test_real_token_stays_internal(self):
        real = "REAL-PANEL-TOKEN"
        seen = []

        def fake(method, path, body=None, token=None, timeout=desk.HTTP_TIMEOUT):
            seen.append((method, path, token))
            if path == "/api/session":
                return 200, {"token": real}
            return 200, {"ok": True}

        with mock.patch.object(desk, "http_call", side_effect=fake):
            result = desk.panel_call("/api/workflow", "POST", {"enabled": True})
            importlib.reload(desk)
        self.assertEqual(result, {"status": 200, "data": {"ok": True}})
        self.assertIn(("POST", "/api/workflow", real), seen)
        self.assertNotIn(real, json.dumps(result))

    def test_non_success_response_is_preserved(self):
        with mock.patch.object(desk, "http_call", return_value=(404, {"error": "操作不存在"})):
            self.assertEqual(desk.panel_call("/api/tasks/missing", "GET"),
                             {"status": 404, "data": {"error": "操作不存在"}})

    def test_unreachable_service_returns_503_for_reads(self):
        offline = "无法连接模型工作台服务，请先打开模型工作台。"
        with mock.patch.object(desk, "http_call", side_effect=desk.PanelError(offline)):
            result = desk.panel_call("/api/state", "GET")
        self.assertEqual(result["status"], 503)
        self.assertEqual(result["data"]["error"], offline)

    def test_import_and_listing_touch_nothing(self):
        watched = [desk.HERE / name for name in ("models.json", "settings.json", "workflow.json")]
        before = fingerprint(watched)
        with mock.patch.object(desk, "http_call", side_effect=AssertionError("network")), \
             mock.patch.object(desk, "find_pwsh", side_effect=AssertionError("launcher")), \
             mock.patch.object(desk.subprocess, "run", side_effect=AssertionError("process")):
            importlib.reload(desk)
            desk.tool_definitions()
            desk.resource_definitions()
            desk.validate_request("/api/state", "GET")
        self.assertEqual(before, fingerprint(watched))

    def test_reads_and_mutations_never_start_the_service(self):
        started = []
        offline = desk.PanelError("无法连接模型工作台服务，请先打开模型工作台。")
        token_error = desk.PanelError("模型工作台服务未就绪，请先打开模型工作台。")
        with mock.patch.object(desk, "http_call", side_effect=offline), \
             mock.patch.object(desk, "_start_service", side_effect=lambda: started.append(1)), \
             mock.patch.object(desk, "panel_token", side_effect=token_error):
            self.assertEqual(desk.panel_call("/api/state", "GET")["status"], 503)
            self.assertEqual(desk.panel_call("/api/tasks/t1", "GET")["status"], 503)
            self.assertEqual(desk.panel_call("/api/workflow", "POST", {"enabled": True})["status"], 503)
        self.assertEqual(started, [])

    def test_session_starts_the_service_exactly_once(self):
        state = {"started": 0}

        def start():
            state["started"] += 1

        def workspace(timeout=3):
            return str(desk.ROOT) if state["started"] else "D:\\other"

        with mock.patch.object(desk, "_start_service", side_effect=start), \
             mock.patch.object(desk, "service_workspace", side_effect=workspace):
            result = desk.panel_call("/api/session", "GET")
        self.assertEqual(result["data"]["token"], "mcp-bridge")
        self.assertEqual(state["started"], 1)

    def test_running_service_is_reused_without_restart(self):
        started = []
        with mock.patch.object(desk, "http_call", return_value=(200, {"workspace": str(desk.ROOT)})), \
             mock.patch.object(desk, "_start_service", side_effect=lambda: started.append(1)):
            self.assertTrue(desk.ensure_service())
        self.assertEqual(started, [])

    def test_foreign_service_on_the_port_is_rejected(self):
        with mock.patch.object(desk, "http_call", return_value=(200, {"workspace": "D:\\other"})), \
             mock.patch.object(desk, "_start_service", return_value=None):
            self.assertFalse(desk.ensure_service(timeout=0))


class UiBundleTests(unittest.TestCase):
    def test_bundle_inlines_assets_from_a_temp_fixture(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "index.html").write_text(
                '<html><head><link rel="stylesheet" href="/style.css"></head>'
                '<body><script src="/app.js"></script></body></html>', encoding="utf-8")
            (base / "style.css").write_text("body{color:#123}", encoding="utf-8")
            (base / "app.js").write_text('const t="</script>-safe";', encoding="utf-8")
            (base / "codex-bridge.js").write_text("window.modelDeskHost=true;", encoding="utf-8")
            html = desk.build_workbench_html(base)
        self.assertIn("body{color:#123}", html)
        self.assertIn("window.modelDeskHost=true", html)
        self.assertNotIn('src="/app.js"', html)
        self.assertNotIn('href="/style.css"', html)
        self.assertIn("<\\/script>-safe", html)
        self.assertNotIn("</script>-safe", html)

    def test_missing_bridge_is_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "index.html").write_text('<link rel="stylesheet" href="/style.css"><script src="/app.js"></script>', encoding="utf-8")
            (base / "style.css").write_text("", encoding="utf-8")
            (base / "app.js").write_text("", encoding="utf-8")
            with self.assertRaises(desk.PanelError):
                desk.build_workbench_html(base)

    def test_real_panel_bundle_is_self_contained(self):
        items = desk.read_workbench()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].mime_type, desk.UI_MIME)
        self.assertFalse(items[0].meta["ui"]["prefersBorder"])
        html = items[0].content
        self.assertNotIn('<script src=', html)
        self.assertNotIn('<link rel="stylesheet"', html)
        self.assertIn("window.modelDeskHost", html)
        self.assertIn("model-desk.theme", html)


class ServerHandlerTests(unittest.TestCase):
    def setUp(self):
        self.server = desk.build_server()

    def call(self, request_type, request):
        import anyio
        return anyio.run(self.server.request_handlers[request_type], request)

    def test_handlers_are_registered(self):
        for request_type in (mcp_types.ListToolsRequest, mcp_types.ListResourcesRequest,
                             mcp_types.ReadResourceRequest, mcp_types.CallToolRequest):
            self.assertIn(request_type, self.server.request_handlers)

    def test_list_tools_over_the_registered_handler(self):
        result = self.call(mcp_types.ListToolsRequest, mcp_types.ListToolsRequest(method="tools/list"))
        self.assertEqual([tool.name for tool in result.root.tools], [desk.TOOL_OPEN, desk.TOOL_PANEL])

    def test_read_resource_over_the_registered_handler(self):
        request = mcp_types.ReadResourceRequest(
            method="resources/read", params=mcp_types.ReadResourceRequestParams(uri=desk.RESOURCE_URI))
        with mock.patch.object(desk, "http_call", side_effect=AssertionError("network")):
            result = self.call(mcp_types.ReadResourceRequest, request)
        contents = result.root.contents[0]
        self.assertEqual(contents.mimeType, desk.UI_MIME)
        self.assertIn("model-desk.theme", contents.text)

    def test_unknown_resource_is_rejected(self):
        request = mcp_types.ReadResourceRequest(
            method="resources/read", params=mcp_types.ReadResourceRequestParams(uri="ui://model-desk/other"))
        with self.assertRaises(ValueError):
            self.call(mcp_types.ReadResourceRequest, request)

    def test_panel_request_proxies_through_registered_handler(self):
        request = mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
            name=desk.TOOL_PANEL, arguments={"path": "/api/state", "method": "GET"}))
        with mock.patch.object(desk, "http_call", return_value=(200, {"workspace": str(desk.ROOT)})):
            result = self.call(mcp_types.CallToolRequest, request)
        self.assertEqual(result.root.structuredContent, {"status": 200, "data": {"workspace": str(desk.ROOT)}})
        self.assertFalse(result.root.isError)

    def test_panel_payload_is_not_duplicated_as_text_by_the_sdk(self):
        request = mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
            name=desk.TOOL_PANEL, arguments={"path": "/api/state", "method": "GET"}))
        data = {"tasks": [{"id": "one", "answer": "answer-marker" * 10000}]}
        with mock.patch.object(desk, "http_call", return_value=(200, data)):
            result = self.call(mcp_types.CallToolRequest, request).root
        self.assertEqual(result.structuredContent, {"status": 200, "data": data})
        self.assertLess(sum(len(item.text) for item in result.content), 100)
        self.assertNotIn("answer-marker", result.content[0].text)
        self.assertFalse(result.isError)

    def test_panel_request_rejects_out_of_scope_path(self):
        request = mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
            name=desk.TOOL_PANEL, arguments={"path": "/api/../secret", "method": "GET"}))
        result = self.call(mcp_types.CallToolRequest, request)
        self.assertTrue(result.root.isError)

    def test_open_tool_reports_a_safe_error(self):
        request = mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
            name=desk.TOOL_OPEN, arguments={}))
        with mock.patch.object(desk, "open_desk", side_effect=desk.PanelError("模型工作台未能启动，请手动运行启动脚本检查。")):
            result = self.call(mcp_types.CallToolRequest, request)
        self.assertTrue(result.root.isError)
        self.assertIn("请手动运行启动脚本", result.root.content[0].text)

    def test_open_tool_returns_public_payload(self):
        request = mcp_types.CallToolRequest(method="tools/call", params=mcp_types.CallToolRequestParams(
            name=desk.TOOL_OPEN, arguments={}))
        with mock.patch.object(desk, "ensure_service", return_value=True):
            result = self.call(mcp_types.CallToolRequest, request)
        payload = result.root.structuredContent
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["url"], "http://127.0.0.1:8765")


if __name__ == "__main__":
    unittest.main(verbosity=2)
