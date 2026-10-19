"""Separate stdio MCP entry for the host-neutral main brain (phase A).

This is *not* the Codex panel MCP: it exposes a small fixed set of collaboration
operations to a main-brain host, never a generic HTTP proxy, command runner, file
reader, configuration writer or token getter.  The project, host, role, owner,
session binding and broker URL come from explicit trusted launcher flags; a tool
call can never override them.

Importing this module, MCP ``initialize``, ``tools/list`` and ``brain_describe``
perform no socket I/O and never start the workbench.  Only an explicit
``brain_connect`` contacts an already-running local controller; the private
capability stays in this process and is not returned by any tool.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

import brain_context
import brain_transport

TOOL_CONNECT = 'brain_connect'
TOOL_DESCRIBE = 'brain_describe'
TOOL_CANDIDATES = 'brain_candidates'
TOOL_PREVIEW = 'brain_route_preview'
TOOL_DISPATCH = 'brain_dispatch'
TOOL_STATUS = 'brain_status'
TOOL_DELIVERY = 'brain_delivery'
TOOL_REVIEW = 'brain_review'
TOOL_REWORK = 'brain_rework'
TOOL_WAKE_PROBE = 'brain_wakeup_probe'

TOOLS = (TOOL_CONNECT, TOOL_DESCRIBE, TOOL_CANDIDATES, TOOL_PREVIEW, TOOL_DISPATCH,
         TOOL_STATUS, TOOL_DELIVERY, TOOL_REVIEW, TOOL_REWORK)

_CONTEXT = None
_TRANSPORT = None
_PANEL_URL = brain_transport.DEFAULT_BASE_URL
_TIMEOUT = 15


def probe_available():
    if _CONTEXT is None or _CONTEXT.host!='dsh' or _CONTEXT.role!='brain':return False
    try:
        import dsh_notify
        config=dsh_notify.config()
        return config['SESSION']==_CONTEXT.native_session.value and config['OWNER']==_CONTEXT.owner
    except ValueError:return False


def available_tools():
    return (*TOOLS,TOOL_WAKE_PROBE) if probe_available() else TOOLS

_CONNECT_HINT = ('模型工作台未连接。请先运行本机启动脚本（打开模型工作台）或 Codex 的 open_model_desk，'
                 '然后显式调用 brain_connect；本接口不会自动启动服务。')

_EVIDENCE_SOURCES = ['reviewer_file', 'reviewer_test', 'reviewer_inspection',
                     'executor_report', 'executor_test', 'self_report']
_IDENTITY_PROPERTIES = {
    'value': {'type': ['string', 'null']},
    'state': {'type': 'string', 'enum': ['declared', 'unknown']},
    'source': {'type': 'string'},
}


def _object(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


_CRITERIA = {
    'type': 'array',
    'maxItems': 20,
    'items': _object({
        'id': {'type': 'string', 'maxLength': 120},
        'text': {'type': 'string', 'maxLength': 500},
        'status': {'type': 'string', 'enum': ['pass', 'fail']},
        'evidence': {
            'type': 'array',
            'maxItems': 20,
            'items': _object({
                'source': {'type': 'string', 'enum': _EVIDENCE_SOURCES},
                'detail': {'type': 'string', 'maxLength': 1000},
                'exit_code': {'type': 'integer'},
            }, required=('source', 'detail')),
        },
        'note': {'type': 'string', 'maxLength': 1000},
    }, required=('status', 'evidence')),
}


def tool_definitions():
    tools = [
        types.Tool(
            name=TOOL_CONNECT, title='显式连接主脑控制器',
            description=('显式连接已经运行的本机模型工作台，登记受信启动上下文并取得进程内私有连接能力。'
                         '不会自动启动服务；连接能力不会出现在任何工具结果中。'),
            inputSchema=_object({}),
            annotations=types.ToolAnnotations(title='显式连接主脑控制器', destructiveHint=False,
                                              idempotentHint=False, openWorldHint=False),
        ),
        types.Tool(
            name=TOOL_DESCRIBE, title='说明主脑身份与权限',
            description=('返回受信启动上下文：项目、宿主、角色、不可变拥有者、声明/核实/未知身份、'
                         '会话责任归属与当前连接状态。不联网、不派发。'),
            inputSchema=_object({}),
            annotations=types.ToolAnnotations(title='说明主脑身份与权限', destructiveHint=False,
                                              idempotentHint=True, openWorldHint=False),
        ),
        types.Tool(
            name=TOOL_CANDIDATES, title='列出候选执行模型',
            description=('列出已配置模型、默认执行 Agent、范围/可选择性/付费授权和就绪状态；'
                         'scope_ok 与 selectable_model_ids 只反映真实范围，不按同名/同 provider 排除，'
                         'subagent deny 不影响普通候选。不调用模型、不派发。'),
            inputSchema=_object({}),
            annotations=types.ToolAnnotations(title='列出候选执行模型', destructiveHint=False,
                                              idempotentHint=True, openWorldHint=False),
        ),
        types.Tool(
            name=TOOL_PREVIEW, title='按模型预览路由',
            description=('对显式 model_id 做一次与实际派发相同的路由预览（显式模型 -> 已保存默认 Agent），'
                         '返回执行器、原因、警告和预算预测；不派发、不等待模型。'
                         '不会用默认模型替代缺失的 model_id。'),
            inputSchema=_object({
                'model_id': {'type': 'string', 'maxLength': 80},
                'prompt': {'type': 'string', 'maxLength': 50000},
                'kind': {'type': 'string', 'enum': ['edit', 'chat']},
                'files': {'type': 'array', 'maxItems': 30, 'items': {'type': 'string'}},
                'acceptance': {'type': 'array', 'minItems': 1, 'maxItems': 20,
                               'items': {'type': 'string', 'maxLength': 500}},
            }, required=('model_id',)),
            annotations=types.ToolAnnotations(title='按模型预览路由', destructiveHint=False,
                                              idempotentHint=True, openWorldHint=False),
        ),
        types.Tool(
            name=TOOL_DISPATCH, title='快速派发执行任务',
            description=('按调用方明确给出的 request_id 异步派发一个执行任务，快速返回原任务 ID。'
                         '项目/拥有者/角色固定来自受信启动上下文；不能在此指定它们或执行器。'
                         '省略 model_id 时使用已保存的手动默认模型；默认在范围外会明确拒绝并提示范围候选，'
                         '不会静默换模型。同一 request_id 同内容返回原结果，不同内容拒绝；响应丢失不自动重派。'),
            inputSchema=_object({
                'request_id': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$'},
                'prompt': {'type': 'string', 'maxLength': 50000},
                'model_id': {'type': 'string', 'maxLength': 80},
                'kind': {'type': 'string', 'enum': ['edit', 'chat']},
                'files': {'type': 'array', 'maxItems': 30, 'items': {'type': 'string'}},
                'acceptance': {'type': 'array', 'minItems': 1, 'maxItems': 20,
                               'items': {'type': 'string', 'maxLength': 500}},
                'title': {'type': 'string', 'maxLength': 100},
            }, required=('request_id', 'prompt', 'acceptance')),
            annotations=types.ToolAnnotations(title='快速派发执行任务', destructiveHint=False,
                                              idempotentHint=True, openWorldHint=False),
        ),
        types.Tool(
            name=TOOL_STATUS, title='读取任务状态',
            description='按任务 ID 读取状态与验收摘要；只读、不等待模型、不重复派发。',
            inputSchema=_object({'task_id': {'type': 'string', 'maxLength': 80}},
                                required=('task_id',)),
            annotations=types.ToolAnnotations(title='读取任务状态', destructiveHint=False,
                                              idempotentHint=True, openWorldHint=False),
        ),
        types.Tool(
            name=TOOL_DELIVERY, title='取回任务交付',
            description=('读取执行器交付、自报测试、独立证据计数和验收状态。执行器完成不等于验收通过；'
                         '不等待模型、不自动补证据。'),
            inputSchema=_object({'task_id': {'type': 'string', 'maxLength': 80}},
                                required=('task_id',)),
            annotations=types.ToolAnnotations(title='取回任务交付', destructiveHint=False,
                                              idempotentHint=True, openWorldHint=False),
        ),
        types.Tool(
            name=TOOL_REVIEW, title='独立验收或退回整改',
            description=('记录主脑逐条验收结论与证据，或退回整改/记录受阻。'
                         '验收通过必须为每条条件提供主脑独立证据；执行器自报不能通过。'
                         '同一 request_id 不会重复消耗整改次数。'),
            inputSchema=_object({
                'request_id': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$'},
                'task_id': {'type': 'string', 'maxLength': 80},
                'action': {'type': 'string', 'enum': ['accept', 'final_accept', 'return', 'block']},
                'criteria': _CRITERIA,
                'notes': {'type': 'string', 'maxLength': 2000},
                'failed_criteria': {'type': 'array', 'maxItems': 20, 'items': {'type': 'string'}},
                'problems': {'type': 'string', 'maxLength': 2000},
                'boundaries': {'type': 'string', 'maxLength': 1000},
                'reason': {'type': 'string', 'maxLength': 2000},
            }, required=('request_id', 'task_id', 'action')),
            annotations=types.ToolAnnotations(title='独立验收或退回整改', destructiveHint=True,
                                              idempotentHint=True, openWorldHint=False),
        ),
        types.Tool(
            name=TOOL_REWORK, title='派发一次整改',
            description=('对已退回整改的同一任务派发一次整改，保持原模型与执行器，默认使用新会话。'
                         '仍然最多一次；不能借整改接管其他拥有者或项目的任务。'),
            inputSchema=_object({
                'request_id': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$'},
                'parent_task_id': {'type': 'string', 'maxLength': 80},
            }, required=('request_id', 'parent_task_id')),
            annotations=types.ToolAnnotations(title='派发一次整改', destructiveHint=True,
                                              idempotentHint=True, openWorldHint=False),
        ),
    ]
    if probe_available():
        tools.append(types.Tool(name=TOOL_WAKE_PROBE,title='原聊天自动唤醒探针',
            description=('仅在用户要求诊断时使用。start 后在约30秒及原聊天空闲时投递一次固定诊断事件，'
                         '应结束本轮，明确告知用户等待期间不要发消息或关闭DSH，90秒未收到可询问状态。'
                         '不派发执行任务；本阶段不启用真实任务自动唤醒。收到事件后用received确认。'
                         '不接受自定义消息、目标聊天、模型、项目或任意时间设置；未知结果不重试。'),
            inputSchema=_object({'action':{'type':'string','enum':['start','status','received','stop']},
                                 'probe_id':{'type':'string','pattern':'^probe-[0-9a-f]{32}$'}},required=('action',)),
            annotations=types.ToolAnnotations(title='原聊天唤醒探针',destructiveHint=False,
                                              idempotentHint=True,openWorldHint=False)))
    return tools


# -------------------------------------------------------------------- transport

def _transport():
    global _TRANSPORT
    if _TRANSPORT is None:
        _TRANSPORT = brain_transport.BrainTransport(_PANEL_URL, timeout=_TIMEOUT)
    return _TRANSPORT


def _connected():
    return _TRANSPORT is not None and _TRANSPORT.connected


def _call(operation, payload):
    transport = _transport()
    if not transport.connected:
        raise brain_transport.NotConnected(_CONNECT_HINT)
    return transport.call(operation, payload)


def _connect():
    global _TRANSPORT
    transport = _transport()
    result = transport.connect(_CONTEXT.declaration())
    # The capability is intentionally not copied into this return value.
    return {'connected': True, 'base_url': transport.base_url,
            'context': result.get('context') or _CONTEXT.public(),
            'limits': result.get('limits') or {},
            'next': '现在可以调用候选、预览、派发、状态、交付和验收工具。'}


def _describe():
    return {'connected': _connected(), 'context': _CONTEXT.public(),
            'tools': list(available_tools()),
            'wakeup':{'probe_available':probe_available(),'task_completion_enabled':False},
            'limits': {'mutations_require_session_binding': True,
                       'brain_vs_executor_from_launch': True,
                       'project_owner_from_launch': True,
                       'arbitrary_http_command_file_config_tool': False,
                       'getters_wait': False},
            'note': ('身份、角色、项目与拥有者来自受信启动配置；工具参数不能修改。'
                     '声明值不是宿主核实值。若历史提示与当前真实候选/路由预览不一致，'
                     '保留历史原因为未知，不要据此声称整个范围没有模型。')}


def _strict(arguments, allowed, label):
    if not isinstance(arguments, dict):
        raise ValueError(label + ' 参数需要是对象')
    unknown = set(arguments) - set(allowed)
    if unknown:
        raise ValueError(label + ' 含未知参数：' + '、'.join(sorted(unknown)))
    return arguments


def _check_criteria(criteria):
    if not isinstance(criteria, list):
        raise ValueError('验收结论需要是数组')
    for item in criteria:
        _strict(item, ('id', 'text', 'status', 'evidence', 'note'), '验收条件')
        if item.get('status') not in ('pass', 'fail'):
            raise ValueError('验收结论只能是 pass 或 fail')
        evidence = item.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('每条验收条件都需要证据数组（不能为空）')
        for entry in evidence:
            _strict(entry, ('source', 'detail', 'exit_code'), '验收证据')
            if entry.get('source') not in _EVIDENCE_SOURCES:
                raise ValueError('证据来源不正确')


def _redact(message):
    text = str(message or '')
    # No credential is ever placed in these messages; this is defence in depth.
    for marker in ('X-Brain-Capability', 'X-Panel-Token'):
        text = text.replace(marker, '[已隐藏]')
    return text[:1500]


def _error(message, **extra):
    text = _redact(message)
    data = {'error': text, **extra}
    return types.CallToolResult(content=_result_content(text, data),
                                structuredContent=data, isError=True)


def _result_content(summary, data):
    # Some hosts preserve structuredContent internally but project only content
    # into the model prompt (including installed DSH 0.2.1-rc.2). Mirror the
    # already-scoped public broker result here, never a transport/capability or
    # raw /api/state object. Compact JSON avoids formatting overhead and retains
    # all fields needed for routing, delivery, acceptance and unknown outcomes.
    return [types.TextContent(type='text', text=summary),
            types.TextContent(type='text', text=json.dumps(
                data, ensure_ascii=False, separators=(',', ':'), allow_nan=False))]


def _ok(result, text):
    return types.CallToolResult(content=_result_content(text, result),
                                structuredContent=result)


def _run(name, arguments):
    arguments = arguments or {}
    if name == TOOL_WAKE_PROBE:
        _strict(arguments,('action','probe_id'),'wakeup_probe')
        if not probe_available() or not _CONTEXT.mutations_allowed:
            raise ValueError('原聊天唤醒诊断通道未就绪或责任会话未绑定')
        action=arguments.get('action')
        probe_id=arguments.get('probe_id')
        if action not in {'start','status','received','stop'}:
            raise ValueError('未知唤醒诊断操作')
        if action=='start' and probe_id is not None:raise ValueError('start 不接受其他探针编号')
        if action!='start' and (not isinstance(probe_id,str) or not re.fullmatch(r'probe-[0-9a-f]{32}',probe_id)):
            raise ValueError('需要明确的原探针编号')
        _call('describe',{})  # stale broker capability must be reconnected explicitly
        import dsh_notify
        return _ok(dsh_notify.probe(action,probe_id),'已返回原聊天唤醒诊断状态；不是任务执行或验收。')
    if name == TOOL_CONNECT:
        _strict(arguments, (), 'connect')
        return _ok(_connect(), '已显式连接本机主脑控制器；私有连接能力仅保存在本进程内。')
    if name == TOOL_DESCRIBE:
        _strict(arguments, (), 'describe')
        return _ok(_describe(), '已返回受信启动上下文与连接状态。')
    if name == TOOL_CANDIDATES:
        _strict(arguments, (), 'candidates')
        result = _call('candidates', {})
        return _ok(result, ('已返回候选模型；未调用模型。请检查 models[].scope_ok/selectable、'
                            'selectable_model_ids 和 default_model_note；默认模型在范围外'
                            '不代表范围内没有候选，历史提示无法复现时保留未知。'))
    if name == TOOL_PREVIEW:
        _strict(arguments, ('model_id', 'prompt', 'kind', 'files', 'acceptance'), 'route_preview')
        return _ok(_call('route_preview', arguments), '已返回路由预览；未派发任务，未替换 model_id。')
    if name == TOOL_DISPATCH:
        _strict(arguments, ('request_id', 'prompt', 'model_id', 'kind', 'files',
                            'acceptance', 'title'), 'dispatch')
        acceptance = arguments.get('acceptance')
        if not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 20:
            raise ValueError('dispatch 需要 1 到 20 条验收条件')
        result = _call('dispatch', arguments)
        task = result.get('task') or {}
        return _ok(result, '已异步派发任务 %s；读取状态/交付不会重复派发。' % task.get('task_id'))
    if name == TOOL_STATUS:
        _strict(arguments, ('task_id',), 'status')
        return _ok(_call('status', arguments), '已返回任务状态；未等待模型。')
    if name == TOOL_DELIVERY:
        _strict(arguments, ('task_id',), 'delivery')
        return _ok(_call('delivery', arguments), '已返回任务交付与验收状态。')
    if name == TOOL_REVIEW:
        _strict(arguments, ('request_id', 'task_id', 'action', 'criteria', 'notes',
                            'failed_criteria', 'problems', 'boundaries', 'reason'), 'review')
        if arguments.get('criteria'):
            _check_criteria(arguments['criteria'])
        return _ok(_call('review', arguments), '已记录验收操作；不会自动重试。')
    if name == TOOL_REWORK:
        _strict(arguments, ('request_id', 'parent_task_id'), 'rework')
        return _ok(_call('rework', arguments), '已派发一次整改（如原请求未重复）。')
    raise ValueError('未知的主脑工具')


# -------------------------------------------------------------------- MCP server

def build_server():
    server = Server('model-desk-brain')

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return tool_definitions()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        if name not in available_tools():
            return _error('未知的主脑工具')
        try:
            return await anyio.to_thread.run_sync(lambda: _run(name, arguments))
        except brain_transport.OutcomeUnknown as error:
            return _error(str(error), outcome='unknown',
                          request_reference=getattr(error, 'reference', None))
        except brain_transport.NotConnected as error:
            return _error(str(error), outcome='not_connected')
        except brain_transport.CallRejected as error:
            return _error(str(error), outcome='rejected', status=error.status)
        except (brain_transport.TransportError, brain_context.ContextError,
                ValueError, OSError) as error:
            return _error(str(error))
        except Exception as error:  # never leak internals or credentials in a result
            return _error('主脑接口内部错误（%s）；未自动重试。' % type(error).__name__)

    return server


async def serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='模型工作台主脑 MCP（独立 stdio 入口；不会自动启动服务）')
    parser.add_argument('--project', required=True,
                        help='明确的本机项目绝对目录；不从 cwd、安装目录或最近项目推断')
    parser.add_argument('--host', required=True, help='宿主标识，例如 dsh / zcode / workbuddy')
    parser.add_argument('--owner', required=True, help='不可变的主脑拥有者标识')
    parser.add_argument('--role', required=True, choices=brain_context.ROLES,
                        help='brain 可变更加只读；executor 只能读取且变更被拒绝')
    parser.add_argument('--session-binding', choices=('user_bound', 'unknown'), default='unknown',
                        help='显式用户绑定或 unknown；阶段 A 不提供 host_verified')
    parser.add_argument('--session-id', help='显式用户绑定的专用会话标识（不是宿主原生会话）')
    parser.add_argument('--native-session-id', help='宿主原生会话 ID（仅声明，未核实）')
    parser.add_argument('--host-version', help='宿主版本（仅声明，未核实）')
    parser.add_argument('--model', help='主脑模型标识（仅声明，未核实）')
    parser.add_argument('--launcher', default='brain-mcp', help='启动入口标识')
    parser.add_argument('--panel-url', default=brain_transport.DEFAULT_BASE_URL,
                        help='已运行的本机模型工作台回环地址')
    parser.add_argument('--timeout', type=int, default=15, help='单次本机 HTTP 超时秒数')
    args = parser.parse_args(argv)
    if args.session_binding == 'user_bound' and not args.session_id:
        parser.error('--session-binding user_bound 需要同时提供 --session-id')
    if args.session_binding == 'unknown' and args.session_id:
        parser.error('提供 --session-id 时必须声明 --session-binding user_bound')
    return args


def configure(args):
    """Validate launch paths/settings without network I/O or service startup."""
    global _CONTEXT, _PANEL_URL, _TIMEOUT, _TRANSPORT
    _PANEL_URL = brain_transport.normalize_base_url(args.panel_url)
    _TIMEOUT = max(1, min(int(args.timeout or 15), 120))
    _CONTEXT = brain_context.build_context(
        project=args.project, host=args.host, owner=args.owner, role=args.role,
        host_version=args.host_version, model=args.model,
        native_session=args.native_session_id,
        operator_session=args.session_id, session_binding=args.session_binding,
        launcher=args.launcher)
    if os.environ.get('MODEL_DESK_COMPLETION_HOST')=='dsh':
        import dsh_notify
        config=dsh_notify.config()
        if (_CONTEXT.host!='dsh' or _CONTEXT.role!='brain'
                or config['SESSION']!=_CONTEXT.native_session.value or config['OWNER']!=_CONTEXT.owner):
            raise ValueError('DSH 通知通道与主脑启动身份不一致')
        os.environ['MODEL_DESK_DSH_WAKE_BINDING']=_CONTEXT.binding_key
    _TRANSPORT = None
    return _CONTEXT


def main(argv=None) -> None:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    args = parse_args(argv)
    try:
        configure(args)
    except (brain_context.ContextError, brain_transport.TransportError) as error:
        print('主脑 MCP 启动配置无效：' + _redact(str(error)), file=sys.stderr)
        raise SystemExit(2)
    anyio.run(serve)


if __name__ == '__main__':
    main()
