import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import model_registry as registry
import panel_server as panel


class MockModel(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "test-model"}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for part in ("测试", "回复"):
                self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": part}}]}) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(.02)
            self.wfile.write(b'data: {"choices":[],"usage":{"total_tokens":12}}\n\ndata: [DONE]\n\n')
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"OK"}}]}')


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="model-panel-test-")
        cls.saved_paths = (registry.HERE, registry.REGISTRY, registry.RUNS)
        registry.HERE = Path(cls.temp.name)
        registry.REGISTRY = registry.HERE / "models.json"
        registry.RUNS = registry.HERE / "runs"
        cls.mock = ThreadingHTTPServer(("127.0.0.1", 0), MockModel)
        threading.Thread(target=cls.mock.serve_forever, daemon=True).start()
        cls.model_base = f"http://127.0.0.1:{cls.mock.server_port}/v1"
        registry.write_json(registry.HERE / "settings.json", {
            "model": "test-model", "api_base": cls.model_base, "api_key_env": "",
            "max_input_tokens": 32768, "max_output_tokens": 4096})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.opener = build_opener(ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.mock.shutdown()
        registry.HERE, registry.REGISTRY, registry.RUNS = cls.saved_paths
        cls.temp.cleanup()

    def request(self, path, body=None, token=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Panel-Token"] = panel.TOKEN
        if origin:
            headers["Origin"] = origin
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)

    def test_cross_origin_and_missing_token(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/models", {}, token=False)
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/state", origin="https://example.org")
        self.assertEqual(error.exception.code, 403)

    def test_model_crud_encrypted_key_and_default(self):
        model = self.request("/api/models", {"name": "test-provider", "provider": "custom",
            "model": "test-model", "api_base": self.model_base, "api_key": "test-only-secret", "enabled": True,
            "bypass_proxy": True})
        self.assertNotIn("secret", model)
        self.assertNotIn("api_key", model)
        self.assertTrue(model["api_key_set"])
        self.assertNotIn("test-only-secret", registry.REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(registry.get_model(model["id"])["api_key"], "test-only-secret")
        self.request(f"/api/models/{model['id']}/default", {})
        self.assertEqual(self.request("/api/state")["default_model"], model["id"])
        check = self.request(f"/api/models/{model['id']}/test", {})
        self.assertTrue(check["ok"])
        self.request(f"/api/models/{model['id']}/delete", {})
        self.assertEqual(self.request("/api/state")["default_model"], "home")

    def test_streaming_task_records_answer_and_usage(self):
        run = registry.task_dir("stream-test")
        run.mkdir(parents=True)
        (run / "prompt.txt").write_text("test", encoding="utf-8")
        task = registry.patch_task(run, id="stream-test", title="test", kind="chat", model_id="home", timeout=30)
        panel.run_chat(run, task, registry.get_model("home"))
        detail = self.request(f"/api/tasks/{task['id']}")
        self.assertEqual(detail["task"]["status"], "completed")
        self.assertEqual(detail["task"]["answer"], "测试回复")
        self.assertEqual(detail["task"]["usage"]["total_tokens"], 12)

    def test_disabled_model_and_file_escape_rejected(self):
        model = self.request("/api/models", {"name": "disabled", "provider": "local", "model": "test-model",
            "api_base": self.model_base, "enabled": False})
        with self.assertRaises(HTTPError):
            self.request("/api/tasks", {"model_id": model["id"], "prompt": "test"})
        self.request(f"/api/models/{model['id']}/delete", {})
        with self.assertRaises(HTTPError):
            self.request("/api/tasks", {"model_id": "home", "kind": "edit", "prompt": "test",
                 "project": str(registry.HERE), "files": ["../outside.py"]})

    def test_cancel_queued_task_preserves_record(self):
        run = registry.task_dir("queued-test")
        run.mkdir(parents=True)
        registry.patch_task(run, id="queued-test", status="queued", kind="chat", model_id="home")
        self.request("/api/tasks/queued-test/cancel", {})
        task = self.request("/api/tasks/queued-test")["task"]
        self.assertEqual(task["status"], "cancelled")
        self.assertTrue((run / "cancelled.flag").exists())

    def test_task_order_handles_mixed_timezones(self):
        for task_id, created in (("timezone-a", "2026-10-13T14:00:00+08:00"),
                                 ("timezone-b", "2026-10-13T07:00:00+00:00")):
            registry.patch_task(registry.task_dir(task_id), id=task_id, kind="chat", status="completed", created_at=created)
        rows = [row["id"] for row in self.request("/api/state")["tasks"] if row["id"].startswith("timezone-")]
        self.assertEqual(rows, ["timezone-b", "timezone-a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
