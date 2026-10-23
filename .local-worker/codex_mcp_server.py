"""Model Desk MCP Apps backend.

Exposes the local Model Desk workbench to any local Codex project through the
official MCP Apps contract (stdio transport, official Python mcp SDK):

* ``open_model_desk`` - the model-visible workbench entry. It starts the existing
  loopback panel on explicit request and renders the workbench UI.
* ``panel_request`` - a UI-only proxy for a small whitelist of panel API calls.
  The internal panel session token never leaves this process.
* ``query_project_map`` - a model-visible, offline read-only code navigation
  query. It never starts the panel and uses an explicit/host-bound project.
* ``ui://model-desk/workbench-v1`` - the workbench UI, bundled from the panel's
  existing index.html / style.css / app.js plus the MCP host bridge.

Importing this module, initializing the transport, listing tools or reading the
resource NEVER starts the panel, dispatches workers, reads stored model keys or
changes settings. Only an explicit ``open_model_desk`` call, or the UI's first
``/api/session`` request, starts the service; a running service is reused.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.request import url2pathname
from urllib.parse import urlparse, unquote

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
import project_scope
from codex_usage import find_codex

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PANEL_DIR = HERE / "panel"
LAUNCHER = ROOT / "打开模型工作台.ps1"
BRIDGE_SCRIPT = "codex-bridge.js"

API_HOST = "127.0.0.1"
API_PORT = 8765
API_BASE = f"http://{API_HOST}:{API_PORT}"

RESOURCE_URI = "ui://model-desk/workbench-v2"
UI_MIME = "text/html;profile=mcp-app"

TOOL_OPEN = "open_model_desk"
TOOL_PANEL = "panel_request"
TOOL_MAP = "query_project_map"

MAX_BODY_BYTES = 200_000
HTTP_TIMEOUT = 15
STARTUP_TIMEOUT = 25
DEFAULT_DUMMY_TOKEN = "mcp-bridge"


class PanelError(Exception):
    """A safe, user-facing failure that never carries secrets or raw dumps."""


# --------------------------------------------------------------------------- paths

def find_pwsh() -> str:
    """Absolute native PowerShell used to start the existing launcher."""
    from powershell_runtime import find_pwsh as resolve
    try:
        return resolve()
    except ValueError as error:
        raise PanelError(str(error)) from None


# --------------------------------------------------------------------- validation

_SAFE_SEGMENT = r"[A-Za-z0-9_-]+"
_SAFE_PATH = re.compile(r"/api/[A-Za-z0-9_./-]+")
_GET_EXACT = frozenset({"/api/state", "/api/workflow", "/api/codex-usage", "/api/session", "/api/reviews",
                        "/api/goals", "/api/stages", "/api/recovery", "/api/executor/capabilities",
                        "/api/tree", "/api/observability", "/api/projects", "/api/task-records",
                        "/api/db/space"})
_GET_GOAL = re.compile(rf"^/api/goals/{_SAFE_SEGMENT}$")
_GET_STAGE = re.compile(rf"^/api/stages/{_SAFE_SEGMENT}$")
_GET_TASK = re.compile(rf"^/api/tasks/{_SAFE_SEGMENT}(/log|/review|/handover|/observability)?$")
_GET_PICK = re.compile(r'^/api/projects/pick/[0-9a-f]{32}$')
_POST_EXACT = frozenset({"/api/workflow", "/api/codex-usage/refresh", "/api/route-preview",
                         "/api/models", "/api/discover", "/api/tasks", "/api/goals", "/api/stages", "/api/projects/pick",
                         "/api/task-records/preview", "/api/task-records/delete", "/api/projects/remember",
                         "/api/db/compact", "/api/project-map"})
_POST_MODEL = re.compile(rf"^/api/models/{_SAFE_SEGMENT}/(save|delete|test|default|balance|thinking)$")
_POST_ENDPOINT = re.compile(r'^/api/execution-endpoints/(pi|dsh|workbuddy)$')
_POST_CANCEL = re.compile(rf"^/api/tasks/{_SAFE_SEGMENT}/cancel$")
_POST_RECOVER = re.compile(rf"^/api/tasks/{_SAFE_SEGMENT}/recover$")
_POST_REVIEW = re.compile(rf"^/api/tasks/{_SAFE_SEGMENT}/review$")
_POST_GOAL = re.compile(rf"^/api/goals/{_SAFE_SEGMENT}/status$")
_POST_STAGE = re.compile(rf"^/api/stages/{_SAFE_SEGMENT}/(dispatch|cancel|requeue|sync)$")
_POST_TASK_EXTRA = re.compile(rf"^/api/tasks/{_SAFE_SEGMENT}/(handover|reference)$")


def validate_request(path, method) -> str:
    """Return a whitelisted loopback path, or raise for anything else.

    Rejects query strings, fragments, percent encoding, absolute URLs,
    traversal, duplicated separators and unsupported methods.
    """
    if not isinstance(method, str) or method.upper() not in ("GET", "POST"):
        raise PanelError("只允许 GET 或 POST 请求。")
    if not isinstance(path, str) or not path:
        raise PanelError("接口路径格式不正确。")
    if any(token in path for token in ("?", "#", "%", "\\", " ", "\t", "\n", "\r")):
        raise PanelError("接口路径不允许查询、片段或转义字符。")
    if not path.startswith("/api/") or "//" in path:
        raise PanelError("只支持工作台内部的相对接口路径。")
    if not _SAFE_PATH.fullmatch(path):
        raise PanelError("接口路径包含不支持的字符。")
    if ".." in path.split("/"):
        raise PanelError("接口路径不允许上级跳转。")
    if method.upper() == "GET":
        allowed = (path in _GET_EXACT or bool(_GET_TASK.fullmatch(path))
                   or bool(_GET_GOAL.fullmatch(path)) or bool(_GET_STAGE.fullmatch(path))
                   or bool(_GET_PICK.fullmatch(path)))
    else:
        allowed = (path in _POST_EXACT
                   or bool(_POST_ENDPOINT.fullmatch(path))
                   or bool(_POST_MODEL.fullmatch(path))
                   or bool(_POST_CANCEL.fullmatch(path))
                   or bool(_POST_RECOVER.fullmatch(path))
                   or bool(_POST_REVIEW.fullmatch(path))
                   or bool(_POST_GOAL.fullmatch(path))
                   or bool(_POST_STAGE.fullmatch(path))
                   or bool(_POST_TASK_EXTRA.fullmatch(path)))
    if not allowed:
        raise PanelError("该接口不在工作台允许范围内。")
    return path


# ------------------------------------------------------------------- panel access

class _NoRedirect(HTTPRedirectHandler):
    """Never follow a redirect; the panel is a fixed loopback endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: A002
        return None


def _opener():
    return build_opener(ProxyHandler({}), _NoRedirect)


def _decode(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"error": "模型工作台返回了无法解析的响应。"}
    return data if isinstance(data, dict) else {"error": "模型工作台返回了意外的响应结构。"}


def http_call(method: str, path: str, body=None, token: str | None = None, timeout: int = HTTP_TIMEOUT, view=None):
    """Call the panel API on the fixed loopback endpoint. No redirect, no retry."""
    payload = None
    headers = {}
    if view is not None:
        headers['X-Model-Desk-View']=project_scope.encode_view_header(view)
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["X-Panel-Token"] = token
    request = Request(API_BASE + path, data=payload, headers=headers, method=method)
    try:
        with _opener().open(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except HTTPError as error:
        return error.code, _decode(error.read(65536))
    except (OSError, ValueError) as error:
        raise PanelError("无法连接模型工作台服务，请先打开模型工作台。") from error


_TOKEN_LOCK = threading.Lock()
_TOKEN: dict[str, str | None] = {"value": None}


def panel_token(force: bool = False, rejected_token: str | None = None) -> str:
    """Internal only: the real panel session token never reaches the UI."""
    with _TOKEN_LOCK:
        # Another concurrent request may already have replaced the rejected
        # credential. Reuse that replacement instead of refreshing once per tab.
        if _TOKEN["value"] and (not force or
                (rejected_token is not None and _TOKEN['value'] != rejected_token)):
            return _TOKEN["value"]
        try:
            status, data = http_call("GET", "/api/session")
        except PanelError:
            raise PanelError("模型工作台服务未就绪，请先打开模型工作台。") from None
        token = data.get("token") if isinstance(data, dict) else None
        if status != 200 or not isinstance(token, str) or not token:
            raise PanelError("模型工作台未返回会话凭据，请重新打开模型工作台。")
        _TOKEN["value"] = token
        return token


def service_workspace(timeout: int = 3):
    """Return the workspace reported by the loopback service, or None."""
    try:
        status, data = http_call("GET", "/api/state", timeout=timeout)
    except PanelError:
        return None
    if status == 200 and isinstance(data, dict):
        return data.get("workspace")
    return None


_STARTUP_LOCK = threading.Lock()


def _start_service() -> None:
    """Run the existing launcher once, without an external browser."""
    launcher = str(LAUNCHER)
    if not Path(launcher).is_file():
        raise PanelError("找不到模型工作台启动脚本。")
    command = [find_pwsh(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launcher, "-NoBrowser"]
    creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryFile() as sink:
        try:
            completed = subprocess.run(command, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                       stdout=sink, stderr=sink, timeout=STARTUP_TIMEOUT,
                                       creationflags=creation)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PanelError("模型工作台启动超时或失败，请手动运行启动脚本检查。") from error
        if completed.returncode == 0:
            return
        sink.seek(0)
        detail = sink.read(4096).decode("utf-8", "replace").strip().replace("\n", " ")
    raise PanelError(f"模型工作台启动脚本未成功：{detail[-300:] or '无输出'}")


def ensure_service(timeout: int | None = None) -> bool:
    """Reuse a running panel for this workspace, otherwise start it explicitly."""
    limit = STARTUP_TIMEOUT if timeout is None else timeout
    with _STARTUP_LOCK:
        if service_workspace() == str(ROOT):
            return True
        _start_service()
        deadline = time.monotonic() + limit
        while time.monotonic() <= deadline:
            if service_workspace() == str(ROOT):
                return True
            time.sleep(0.3)
        return False


def panel_call(path, method, body=None, view=None) -> dict:
    """Proxy a whitelisted call; refresh a rejected pre-handler credential once.

    Network errors, unknown outcomes and other permission refusals never replay
    mutations. The real credential remains inside this MCP process.
    """
    method = method.upper() if isinstance(method, str) else method
    path = validate_request(path, method)
    view_args = {} if view is None else {'view':project_scope.parse_view(view)}
    if method == "GET" and path == "/api/session":
        if not ensure_service():
            return {"status": 503, "data": {"error": "模型工作台服务未能启动，请手动运行启动脚本。"}}
        with _TOKEN_LOCK:
            _TOKEN['value'] = None
        return {"status": 200, "data": {"token": DEFAULT_DUMMY_TOKEN}}
    if method == "GET":
        try:
            status, data = http_call("GET", path, **view_args)
        except PanelError as error:
            return {"status": 503, "data": {"error": str(error)}}
        return {"status": status, "data": data}
    payload = {} if body is None else body
    if not isinstance(payload, dict):
        raise PanelError("请求体必须是 JSON 对象。")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_BODY_BYTES:
        raise PanelError("请求体过大，已拒绝。")
    try:
        token = panel_token()
        status, data = http_call("POST", path, body=payload, token=token, **view_args)
        if session_rejected(status, data):
            # The panel's authentication guard rejected the request before its
            # handler ran. This is the sole safe retry, with identical scope and
            # payload. Never replay an HTTP/transport/business failure.
            token = panel_token(force=True, rejected_token=token)
            status, data = http_call("POST", path, body=payload, token=token, **view_args)
    except PanelError as error:
        return {"status": 503, "data": {"error": str(error)}}
    return {"status": status, "data": data}


def session_rejected(status, data):
    """Recognise only the pre-handler session refusal (including older panels)."""
    if status != 403 or not isinstance(data, dict):
        return False
    return (data.get('code') == 'panel_session_expired' or
            data == {'error': '请刷新面板后重试'})


def open_desk(view=None) -> dict:
    """Start (or reuse) the workbench for this workspace."""
    if not ensure_service():
        raise PanelError("模型工作台未能启动，请手动运行启动脚本检查。")
    return {"ready": True, "url": API_BASE, "workspace": str(ROOT),
            'view':project_scope.parse_view(view), 'ui_version':2}


def single_root_view(roots):
    """The one valid local file root, or None for missing/ambiguous/remote input."""
    if len(roots) != 1:
        return None
    try:
        uri = urlparse(str(roots[0].uri))
        if uri.scheme != 'file' or uri.netloc not in ('', 'localhost'):
            return None
        return project_scope.parse_view({'mode': 'project', 'project': url2pathname(uri.path)})
    except ValueError:
        return None


_THREAD_ID = re.compile(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')


def thread_metadata_view(thread_id, thread):
    """Only metadata for the exact host-supplied local thread may supply cwd."""
    if not isinstance(thread, dict) or thread.get('id') != thread_id:
        return None
    environments = thread.get('environments')
    if environments is not None:
        if (not isinstance(environments, list) or len(environments) != 1
                or not isinstance(environments[0], dict)
                or environments[0].get('environmentId') != 'local'):
            return None
        directory = environments[0].get('cwd')
        if thread.get('cwd') is not None:
            try:
                if project_scope.project_key(directory) != project_scope.project_key(thread['cwd']):
                    return None
            except ValueError:
                return None
    else:
        directory = thread.get('cwd')
    try:
        return project_scope.parse_view({'mode': 'project', 'project': directory})
    except ValueError:
        return None


def read_thread_project(thread_id):
    """Read directory metadata only: no thread start/resume, turn or model request."""
    process = subprocess.Popen([find_codex(), 'app-server', '--stdio'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    messages = queue.Queue(maxsize=16)

    def read_stdout():
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                    if isinstance(message, dict) and message.get('id') in (1, 2):
                        messages.put_nowait(message)
                except (ValueError, queue.Full):
                    continue
        except (OSError, ValueError):
            pass
        finally:
            try:
                messages.put_nowait(None)
            except queue.Full:
                pass

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + 10

    def send(message):
        process.stdin.write(json.dumps(message) + '\n')
        process.stdin.flush()

    def receive(request_id):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Thread metadata lookup timed out')
            message = messages.get(timeout=remaining)
            if message is None:
                raise OSError('Thread metadata reader exited')
            if message.get('id') == request_id:
                if 'error' in message or not isinstance(message.get('result'), dict):
                    raise ValueError('Thread metadata unavailable')
                return message['result']

    try:
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'model_desk_project', 'version': '1'},
            'capabilities': {'experimentalApi': True}}})
        receive(1)
        send({'method': 'initialized', 'params': {}})
        send({'id': 2, 'method': 'thread/read', 'params': {
            'threadId': thread_id, 'includeTurns': False}})
        return thread_metadata_view(thread_id, receive(2).get('thread'))
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        reader.join(timeout=1)
        process.stdout.close()


async def opening_view(server, arguments):
    """Explicit directory, host thread metadata, then a unique advertised root."""
    if arguments.get('project') is not None:
        return project_scope.parse_view({'mode':'project','project':arguments['project']})
    try:
        meta = server.request_context.meta
        meta = meta if isinstance(meta, dict) else meta.model_dump()
    except (AttributeError, LookupError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    if 'threadId' in meta:
        thread_id = meta['threadId']
        if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
            return project_scope.parse_view(None)
        try:
            view = await anyio.to_thread.run_sync(lambda: read_thread_project(thread_id))
            return view if view is not None else project_scope.parse_view(None)
        except Exception:
            # An identified but unreadable thread must not inherit another root.
            return project_scope.parse_view(None)
    try:
        session=server.request_context.session
        if not session.check_client_capability(types.ClientCapabilities(roots={})):
            return project_scope.parse_view(None)
        with anyio.fail_after(2):
            roots=(await session.list_roots()).roots
    except Exception:
        return project_scope.parse_view(None)
    view = single_root_view(roots)
    return view if view is not None else project_scope.parse_view(None)


# ----------------------------------------------------------------------- UI bundle

def _escape_script(source: str) -> str:
    return re.sub(r"</(script)", r"<\\/\1", source, flags=re.IGNORECASE)


def build_workbench_html(panel_dir: Path | None = None) -> str:
    """Inline every local page asset on each resource read.

    The long-lived MCP process need not know asset names added after it was
    started. A newly opened app still gets the current checked-in page bundle.
    """
    folder = Path(panel_dir) if panel_dir is not None else PANEL_DIR
    index = (folder / "index.html").read_text(encoding="utf-8")
    bridge_file = folder / BRIDGE_SCRIPT
    if not bridge_file.is_file():
        raise PanelError("工作台界面桥接脚本缺失，请检查 .local-worker/panel/codex-bridge.js。")
    bridge = bridge_file.read_text(encoding="utf-8")
    link = re.compile(r'<link rel="stylesheet" href="/([a-z0-9-]+\.css)">')
    script = re.compile(r'<script src="/([a-z0-9-]+\.js)"></script>')
    styles = link.findall(index)
    scripts = script.findall(index)
    if styles.count('style.css') != 1 or scripts.count('app.js') != 1 or len(styles) != len(set(styles)) or len(scripts) != len(set(scripts)):
        raise PanelError("工作台页面结构已变化，无法安全打包界面。")
    bridge_tag = '<script>\n' + _escape_script(bridge) + '\n</script>\n'
    def body(name: str) -> str:
        try:
            return (folder / name).read_text(encoding='utf-8')
        except OSError:
            raise PanelError('工作台界面资源缺失：' + name) from None
    index = link.sub(lambda match: '<style>\n' + body(match.group(1)) + '\n</style>', index)
    first_script = True
    def inline_script(match):
        nonlocal first_script
        prefix = bridge_tag if first_script else ''
        first_script = False
        return prefix + '<script>\n' + _escape_script(body(match.group(1))) + '\n</script>'
    return script.sub(inline_script, index)


def read_workbench(panel_dir: Path | None = None) -> list[ReadResourceContents]:
    html = build_workbench_html(panel_dir)
    return [ReadResourceContents(content=html, mime_type=UI_MIME, meta=UI_META)]


UI_META = {"ui": {"prefersBorder": False,
                  "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []}}}

_OPEN_META = {
    "ui": {"resourceUri": RESOURCE_URI, "visibility": ["model", "app"]},
    "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
    "openai/outputTemplate": RESOURCE_URI,
}
_PANEL_META = {"ui": {"visibility": ["app"]}}


def tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name=TOOL_OPEN, title="模型工作台",
            description="在本机打开模型工作台：复用已运行的服务，必要时启动本地服务，随后显示工作台界面。",
            inputSchema={"type": "object", "properties": {'project':{'type':'string','description':'明确的本机项目绝对目录；未提供时优先使用宿主当前聊天的目录元数据，再尝试唯一 MCP root，均不可用时在面板选择'}}, "additionalProperties": False},
            annotations=types.ToolAnnotations(title="模型工作台", destructiveHint=False,
                                              idempotentHint=True, openWorldHint=False),
            _meta=_OPEN_META,
        ),
        types.Tool(
            name=TOOL_PANEL, title="工作台接口代理",
            description="仅供模型工作台界面调用：按白名单转发工作台本机接口请求，界面不直接访问本机端口。",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作台内部接口路径"},
                    "method": {"type": "string", "enum": ["GET", "POST"]},
                    "body": {"type": "object", "description": "POST 请求体"},
                    "view": {"type":"object","description":"此面板实例的项目视图，默认未绑定"},
                },
                "required": ["path", "method"],
                "additionalProperties": False,
            },
            annotations=types.ToolAnnotations(title="工作台接口代理", destructiveHint=False,
                                              idempotentHint=False, openWorldHint=False),
            _meta=_PANEL_META,
        ),
        types.Tool(
            name=TOOL_MAP, title='查询项目代码地图',
            description=('只读查询明确项目的文件、符号、静态 Python 导入和相关测试；'
                         '不启动工作台、不执行代码或模型。项目不明时拒绝，不使用其他标签的项目。'),
            inputSchema={'type':'object', 'properties': {
                'project': {'type':'string', 'description':'明确的本机项目绝对目录；省略时只解析当前聊天目录或唯一 MCP root'},
                'file': {'type':'string', 'description':'项目内相对代码文件，查看本文件及直接邻居'},
                'query': {'type':'string', 'maxLength':160},
                'refresh': {'type':'boolean'},
                'limit': {'type':'integer', 'minimum':1, 'maximum':300},
            }, 'additionalProperties':False},
            annotations=types.ToolAnnotations(title='查询项目代码地图', readOnlyHint=True,
                destructiveHint=False, idempotentHint=True, openWorldHint=False),
        ),
    ]


def resource_definitions() -> list[types.Resource]:
    return [types.Resource(name="model-desk-workbench", title="模型工作台", uri=RESOURCE_URI,
                           description="模型工作台界面，由本机面板资源打包。",
                           mimeType=UI_MIME, _meta=UI_META)]


# -------------------------------------------------------------------- MCP server

def read_project_map(view, arguments):
    view = project_scope.parse_view(view)
    if view['mode'] != 'project':
        raise ValueError('项目地图需要明确的当前项目目录；不会使用全局或其他标签的项目')
    import project_map
    return project_map.query(view['project'], **{key: value for key, value in arguments.items() if key != 'project'})


def build_server() -> Server:
    server = Server("model-desk")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return tool_definitions()

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:
        return resource_definitions()

    @server.read_resource()
    async def _read_resource(uri) -> list[ReadResourceContents]:
        if str(uri).rstrip("/") not in {RESOURCE_URI,'ui://model-desk/workbench-v1'}:
            raise ValueError("未知的工作台资源。")
        return await anyio.to_thread.run_sync(read_workbench)

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        arguments = arguments or {}
        if name == TOOL_MAP:
            try:
                if not isinstance(arguments, dict) or set(arguments) - {'project','file','query','refresh','limit'}:
                    raise ValueError('项目地图参数不正确')
                view = await opening_view(server, arguments)
                result = await anyio.to_thread.run_sync(lambda: read_project_map(view, arguments))
                return types.CallToolResult(content=[
                    types.TextContent(type='text', text='已返回项目代码地图；静态导航不代表测试通过。'),
                    types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False))],
                    structuredContent=result)
            except (PanelError, ValueError, OSError) as error:
                message = str(error) if isinstance(error, (PanelError, ValueError)) else '项目地图暂不可读'
                return types.CallToolResult(content=[types.TextContent(type='text', text=message)], isError=True)
        if name == TOOL_OPEN:
            try:
                view=await opening_view(server,arguments)
                return await anyio.to_thread.run_sync(lambda:open_desk(view))
            except (PanelError,ValueError) as error:
                return types.CallToolResult(content=[types.TextContent(type="text", text=str(error))],
                                            isError=True)
        if name == TOOL_PANEL:
            try:
                result = await anyio.to_thread.run_sync(
                    lambda: panel_call(arguments.get("path"), arguments.get("method"), arguments.get("body"),arguments.get('view')))
            except (PanelError,ValueError) as error:
                return types.CallToolResult(content=[types.TextContent(type="text", text=str(error))],
                                            isError=True)
            # This app-only tool supplies the payload in structuredContent.
            # Returning a dict makes the SDK serialize another full, indented
            # copy into TextContent on every poll, including historical tasks.
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="工作台接口响应。")],
                structuredContent=result)
        return types.CallToolResult(content=[types.TextContent(type="text", text="未知的工作台工具。")],
                                    isError=True)

    return server


async def serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    anyio.run(serve)


if __name__ == "__main__":
    main()
