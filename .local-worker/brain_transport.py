"""Bounded loopback client for the panel-side main-brain broker.

Design rules:

* Only an explicit ``connect()`` performs network I/O.  Importing this module,
  creating a transport, listing MCP tools or reading the launch context never
  opens a socket and never starts the workbench.
* The controller capability (and the transient panel session token used only to
  register it) stays inside this process object.  It is never part of a task,
  prompt, worker environment, log line, tool result or exception message.
* The base URL is a trusted launcher setting, restricted to a loopback HTTP
  origin.  Proxy environment variables and redirects are ignored.
* Mutations are never retried.  A lost response is an explicit unknown outcome;
  a capability rejected after a backend restart requires an explicit reconnect.
"""
from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DEFAULT_BASE_URL = 'http://127.0.0.1:8765'
CAPABILITY_HEADER = 'X-Brain-Capability'
LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}
READ_OPERATIONS = frozenset({'describe', 'candidates', 'route_preview', 'status', 'delivery', 'request_status', 'project_map'})
MUTATING_OPERATIONS = frozenset({'dispatch', 'review', 'rework'})
MAX_BODY_BYTES = 5_000_000
MAX_RESPONSE_BYTES = 5_000_000


class TransportError(ValueError):
    """Safe transport failure that never carries a credential."""


class Unavailable(TransportError):
    """The explicit connection target is not running/reachable."""


class NotConnected(TransportError):
    """No capability is held; an explicit connect is required."""


class CallRejected(TransportError):
    """The controller explicitly refused the call (4xx); no effect was replayed."""

    def __init__(self, message, status=0, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {}


class OutcomeUnknown(TransportError):
    """A mutation may have taken effect; it was not retried."""

    def __init__(self, message, operation='', reference=None):
        super().__init__(message)
        self.operation = operation
        self.reference = reference


def normalize_base_url(value):
    """Accept only an explicit loopback HTTP origin with no path/credentials."""
    if not isinstance(value, str) or not value.strip():
        raise TransportError('主脑服务地址需要是本机回环 HTTP 地址')
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme != 'http':
        raise TransportError('主脑服务地址只允许 http:// 回环地址')
    if parsed.username or parsed.password:
        raise TransportError('主脑服务地址不能包含账号或密码')
    host = (parsed.hostname or '').lower()
    if host not in LOOPBACK_HOSTS:
        raise TransportError('主脑服务地址只允许 127.0.0.1 / localhost / ::1')
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise TransportError('主脑服务地址只能是主机与端口，不能带路径、查询或片段')
    try:
        port = parsed.port or 80
    except ValueError:
        raise TransportError('主脑服务端口不正确') from None
    if not 1 <= port <= 65535:
        raise TransportError('主脑服务端口不正确')
    display_host = '[%s]' % host if ':' in host else host
    return 'http://%s:%d' % (display_host, port)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: A002
        return None


def _decode(raw):
    try:
        data = json.loads(raw.decode('utf-8', 'replace'))
    except ValueError:
        return {'error': '主脑控制器返回了无法解析的响应'}
    return data if isinstance(data, dict) else {'error': '主脑控制器返回了意外的响应结构'}


def _safe_reason(payload, fallback):
    if isinstance(payload, dict):
        for key in ('error', 'detail', 'message'):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:1200]
    return fallback


def _scrub(value, *credentials):
    """Redact known connection credentials even if an upstream error echoes them."""
    if isinstance(value, str):
        for credential in credentials:
            if credential:
                value = value.replace(credential, '[credential redacted]')
        return value
    if isinstance(value, dict):
        return {_scrub(key, *credentials): _scrub(item, *credentials)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, *credentials) for item in value]
    return value


def _valid_result(operation, data):
    if not isinstance(data, dict):
        return False
    if operation == 'status':
        # Status exposes the executor's failure as data.error. It is not an
        # RPC error envelope; HTTP/auth/unknown-outcome checks still run first.
        return (isinstance(data.get('task_id'), str) and bool(data['task_id'])
                and isinstance(data.get('status'), str) and bool(data['status'])
                and (data.get('error') is None or isinstance(data.get('error'), str))
                and not data.get('outcome'))
    if data.get('error'):
        return False
    if operation == 'project_map':
        return (data.get('schema') == 1 and isinstance(data.get('project'), str)
                and isinstance(data.get('nodes'), list) and isinstance(data.get('edges'), list)
                and data.get('read_only') is True)
    if operation == 'request_status':
        return (isinstance(data.get('request_id'), str) and bool(data['request_id'])
                and data.get('request_state') in {'completed', 'rejected', 'reserved',
                                                  'unknown', 'not_found', 'task_found'}
                and data.get('read_only') is True and data.get('automatic_retry') is False)
    if operation in ('dispatch', 'rework'):
        return isinstance(data.get('task'), dict) and bool(data['task'].get('task_id'))
    if operation == 'review':
        return isinstance(data.get('receipt'), dict) and isinstance(data['receipt'].get('review'), dict)
    if operation == 'delivery':
        return bool(data.get('task_id')) and 'status' in data
    if operation == 'candidates':
        return isinstance(data.get('models'), list)
    if operation == 'route_preview':
        return isinstance(data.get('preview'), dict)
    return isinstance(data.get('context'), dict)


class BrainTransport:
    """One in-memory controller capability plus a bounded loopback HTTP client."""

    def __init__(self, base_url=DEFAULT_BASE_URL, timeout=15):
        self._base_url = normalize_base_url(base_url)
        self._timeout = max(1, min(int(timeout), 120)) if isinstance(timeout, int) else 15
        self._capability = None
        self._context = None
        self._lock = threading.Lock()
        self._opener = build_opener(ProxyHandler({}), _NoRedirect)

    @property
    def base_url(self):
        return self._base_url

    @property
    def connected(self):
        with self._lock:
            return bool(self._capability)

    def context_public(self):
        with self._lock:
            return dict(self._context['context']) if self._context else None

    def __repr__(self):
        return 'BrainTransport(base_url=%r, connected=%s)' % (self._base_url, self.connected)

    # ------------------------------------------------------------------ internals
    def _request(self, method, path, body=None, headers=None, mutating=False):
        payload = None
        merged = dict(headers or {})
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
            if len(payload) > MAX_BODY_BYTES:
                raise TransportError('请求体过大，已拒绝')
            merged['Content-Type'] = 'application/json; charset=utf-8'
        request = Request(self._base_url + path, data=payload, headers=merged, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise TransportError('主脑控制器响应过大，已拒绝')
                return response.status, _decode(raw)
        except HTTPError as error:
            try:
                raw = error.read(65536)
            except Exception:
                raw = b''
            return error.code, _decode(raw)
        except (OSError, ValueError):
            if mutating:
                raise OutcomeUnknown(
                    '请求已发往主脑控制器但响应丢失；未自动重试，结果可能未知。') from None
            raise Unavailable('主脑控制器不可用（未自动启动服务）；请先显式打开模型工作台。') from None

    # -------------------------------------------------------------------- public
    def connect(self, declaration):
        """Explicitly register this launch context with a *running* controller.

        The panel session token obtained from ``/api/session`` is used only for
        this registration and is not retained.  If the service is not already
        running a clear unavailable error is returned; this client never starts
        or restarts it.
        """
        if not isinstance(declaration, dict):
            raise TransportError('连接声明需要是对象')
        status, data = self._request('GET', '/api/session')
        token = data.get('token') if isinstance(data, dict) else None
        if status != 200 or not isinstance(token, str) or not token:
            raise Unavailable('模型工作台未运行或未返回会话凭据；请先显式打开模型工作台（启动脚本或 open_model_desk），'
                              '本接口不会自动启动服务。')
        status, data = self._request(
            'POST', '/api/brain/connect', declaration,
            headers={'X-Panel-Token': token}, mutating=False)
        capability = data.get('capability')
        public_data = _scrub({key: value for key, value in data.items() if key != 'capability'},
                             token, capability if isinstance(capability, str) else None)
        del token
        if status != 201 and status != 200:
            raise CallRejected(_safe_reason(public_data, '控制器拒绝了主脑连接注册'), status, public_data)
        context = public_data.get('context')
        if not isinstance(capability, str) or not capability or not isinstance(context, dict):
            raise TransportError('控制器没有返回有效的连接能力；请检查启动配置')
        with self._lock:
            self._capability = capability
            self._context = {'context': context}
        return {'connected': True, 'context': context, 'base_url': self._base_url}

    def disconnect(self):
        with self._lock:
            self._capability = None
            self._context = None

    def call(self, operation, payload=None):
        """Call one fixed broker operation.  Never retries a mutation."""
        if not isinstance(operation, str) or operation not in (READ_OPERATIONS | MUTATING_OPERATIONS):
            raise TransportError('未知的主脑操作')
        body = payload if payload is not None else {}
        if not isinstance(body, dict):
            raise TransportError('操作参数需要是对象')
        with self._lock:
            capability = self._capability
        if not capability:
            raise NotConnected('尚未显式连接主脑控制器；请先调用 brain_connect。')
        mutating = operation in MUTATING_OPERATIONS
        status, data = self._request(
            'POST', '/api/brain/call', {'operation': operation, 'payload': body},
            headers={CAPABILITY_HEADER: capability}, mutating=mutating)
        data = _scrub(data, capability)
        if status in (401, 403):
            with self._lock:
                self._capability = None
                self._context = None
            raise NotConnected('连接能力已失效（控制器可能已重启）；请显式重新调用 brain_connect。')
        if status >= 500:
            if mutating:
                raise OutcomeUnknown('控制器在处理变更时返回服务端错误；未自动重试，结果可能未知。', operation)
            raise Unavailable(_safe_reason(data, '主脑控制器暂时不可用；未自动重试'))
        if status == 409 and isinstance(data, dict) and data.get('outcome') == 'unknown':
            raise OutcomeUnknown(str(data.get('error') or '原请求结果未知；未自动重试'),
                                 operation, reference=data.get('request_reference'))
        if status < 200 or status >= 300:
            raise CallRejected(_safe_reason(data, '主脑控制器拒绝了该操作'), status, data)
        if isinstance(data, dict) and data.get('outcome') == 'unknown':
            raise OutcomeUnknown(str(data.get('error') or '原请求结果未知；未自动重试'),
                                 operation, reference=data.get('request_reference'))
        if not _valid_result(operation, data):
            if mutating:
                raise OutcomeUnknown('变更响应结构无法确认；未自动重试，请核查原请求。', operation,
                                     reference=body.get('request_id'))
            raise TransportError('主脑控制器返回了意外的响应结构')
        return data
