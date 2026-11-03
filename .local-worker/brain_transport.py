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
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DEFAULT_BASE_URL = 'http://127.0.0.1:8765'
CAPABILITY_HEADER = 'X-Brain-Capability'
LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1'}
READ_OPERATIONS = frozenset({'describe', 'candidates', 'route_preview', 'status', 'delivery', 'request_status', 'project_map','plan_status','plan_preview'})
MUTATING_OPERATIONS = frozenset({'dispatch', 'review', 'rework', 'integrate', 'accept_integration', 'finalize', 'reconcile_integration','plan_draft','plan_prepare','plan_resume','plan_handover'})
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
        valid=(isinstance(data.get('request_id'), str) and bool(data['request_id'])
                and data.get('request_state') in {'completed', 'rejected', 'reserved',
                                                  'unknown', 'not_found', 'task_found','plan_found'}
                and data.get('read_only') is True and data.get('automatic_retry') is False)
        if not valid:return False
        if data.get('request_state')=='plan_found' and data.get('request_kind')!='plan':return False
        if data.get('request_kind')=='plan' and ('plan' in data or data.get('request_state') in ('completed','plan_found')):
            receipt=data.get('plan')
            return (isinstance(receipt,dict) and isinstance(receipt.get('goal_id'),str) and bool(receipt['goal_id'])
                    and receipt.get('action') in ('draft','prepare','resume','handover'))
        return True
    if operation in ('dispatch', 'rework'):
        return isinstance(data.get('task'), dict) and bool(data['task'].get('task_id'))
    if operation in ('plan_status','plan_preview'):
        result=data.get('result')
        valid=(data.get('operation')==operation and isinstance(data.get('goal_id'),str) and bool(data['goal_id'])
               and isinstance(result,dict) and data.get('read_only') is True and result.get('read_only') is True)
        if not valid:return False
        if operation=='plan_status':
            return result.get('goal_id')==data['goal_id'] and isinstance(result.get('project'),str) and isinstance(result.get('stages'),list)
        document=result.get('document');definition=document.get('definition') if isinstance(document,dict) else None
        goal=definition.get('goal') if isinstance(definition,dict) else None
        return (isinstance(goal,dict) and goal.get('id')==data['goal_id']
                and isinstance(result.get('digest'),str) and len(result['digest'])==64)
    if operation.startswith('plan_'):
        receipt=data.get('receipt')
        return (data.get('operation')==operation and isinstance(data.get('request_id'),str)
                and isinstance(receipt,dict) and isinstance(receipt.get('goal_id'),str) and bool(receipt['goal_id'])
                and receipt.get('action')==operation.removeprefix('plan_'))
    if operation == 'review':
        return isinstance(data.get('receipt'), dict) and isinstance(data['receipt'].get('review'), dict)
    if operation in ('integrate', 'accept_integration'):
        integration = data.get('integration')
        return (data.get('operation') == operation and isinstance(data.get('request_id'), str)
                and bool(data['request_id']) and isinstance(integration, dict)
                and isinstance(integration.get('name'), str) and bool(integration['name'])
                and (integration.get('verdict') in {'passed', 'merge_conflict', 'check_failed'} or
                     (operation == 'integrate' and isinstance(integration.get('queue'), dict)
                      and type(integration['queue'].get('schema')) is int and integration['queue']['schema'] == 1
                      and isinstance(integration['queue'].get('state'),str)
                      and integration['queue'].get('state') in {'queued','preparing','merging','checking',
                          'blocked','recovery_required','cancelled'}
                      and integration.get('accepted') is False))
                and type(integration.get('accepted')) is bool
                and (operation != 'accept_integration' or
                     (integration['accepted'] is True and integration['verdict'] == 'passed'
                      and isinstance(integration.get('accepted_commit'), str)
                      and bool(integration['accepted_commit']))))
    if operation == 'reconcile_integration':
        record=data.get('integration')
        job=record.get('queue') if isinstance(record,dict) else None
        receipt=job.get('reconciliation') if isinstance(job,dict) else None
        return (data.get('operation')==operation and isinstance(data.get('request_id'),str)
                and bool(data['request_id']) and data.get('executor_replayed') is False
                and isinstance(record,dict) and isinstance(record.get('name'),str)
                and bool(record['name']) and isinstance(receipt,dict)
                and receipt.get('state') in {'not_started','not_held','needs_evidence','unknown','running','ready','settled'})
    if operation == 'finalize':
        result = data.get('result')
        return (data.get('operation') == operation and isinstance(data.get('request_id'), str)
                and bool(data['request_id']) and isinstance(result, dict)
                and isinstance(result.get('task_id'), str) and bool(result['task_id'])
                and isinstance(result.get('controller_finalization'), dict)
                and result['controller_finalization'].get('state') == 'settled'
                and result.get('executor_replayed') is False)
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
                # Bounded HTTPResponse.read(amt) may return syntactically valid
                # JSON even when Content-Length bytes were not all received.
                # Do not turn a partial transport response into a success.
                remaining = getattr(response, 'length', None)
                if type(remaining) is int and remaining > 0:
                    raise ValueError('incomplete response body')
                return response.status, _decode(raw)
        except HTTPError as error:
            try:
                raw = error.read(65536)
            except Exception:
                raw = b''
            return error.code, _decode(raw)
        except (OSError, ValueError, HTTPException):
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
        try:
            status, data = self._request(
                'POST', '/api/brain/call', {'operation': operation, 'payload': body},
                headers={CAPABILITY_HEADER: capability}, mutating=mutating)
        except OutcomeUnknown as error:
            error.operation = operation
            if error.reference is None:
                error.reference = body.get('request_id')
            raise
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
        if operation=='request_status' and data.get('request_kind')=='plan':
            public=self.context_public() or {};identity=public.get('owner') or {}
            reference=identity.get('binding_key','')+'/'+body.get('request_id','')
            if (not identity.get('binding_key') or data.get('request_id')!=body.get('request_id')
                    or data.get('project')!=public.get('project') or data.get('owner')!=identity.get('id')
                    or data.get('request_reference')!=reference
                    or ('plan' in data and data['plan'].get('request_reference')!=reference)):
                raise TransportError('计划请求查询返回了其他请求或归属')
        if operation.startswith('plan_'):
            mismatch=(data.get('goal_id')!=body.get('goal_id') if not mutating else False)
            if not mutating:
                public=self.context_public() or {};result=data['result']
                project=(result.get('project') if operation=='plan_status' else result['document']['definition']['goal'].get('project'))
                mismatch=mismatch or project!=public.get('project')
            if mutating:
                public=self.context_public() or {};identity=public.get('owner') or {}
                receipt=data['receipt']
                mismatch=(not identity.get('binding_key') or data.get('request_id')!=body.get('request_id') or data.get('owner')!=identity.get('id')
                    or data.get('project')!=public.get('project') or
                    receipt.get('request_reference')!=identity.get('binding_key','')+'/'+body.get('request_id',''))
                if operation!='plan_draft':mismatch=mismatch or receipt.get('goal_id')!=body.get('goal_id',receipt.get('goal_id'))
                if operation=='plan_handover':mismatch=mismatch or receipt.get('task_id')!=body.get('task_id')
            if mismatch:
                if mutating:raise OutcomeUnknown('计划回执与原请求或归属不符；未自动重试',operation,reference=body.get('request_id'))
                raise TransportError('计划读取返回了其他目标')
        if operation in ('integrate', 'accept_integration', 'finalize', 'reconcile_integration'):
            mismatch = data['request_id'] != body.get('request_id')
            if operation in ('accept_integration','reconcile_integration'):
                mismatch = mismatch or data['integration']['name'] != body.get('name')
                if operation == 'reconcile_integration':
                    public=self.context_public() or {}
                    binding=(public.get('owner') or {}).get('binding_key')
                    actual=data['integration']['queue']['reconciliation'].get('request_reference')
                    mismatch=mismatch or not binding or actual!=binding+'/'+body.get('request_id','')
            elif operation == 'finalize':
                mismatch = mismatch or data['result']['task_id'] != body.get('task_id')
            if mismatch:
                raise OutcomeUnknown('变更回执与原请求不匹配；未自动重试，请核查原请求。',
                                     operation, reference=body.get('request_id'))
        return data
