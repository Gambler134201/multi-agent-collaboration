"""Panel-side, fixed-operation broker for the host-neutral main brain.

The controller (the existing local panel process) authorizes one private
connection capability per explicit launch context.  Every model-visible call is
one of a small fixed set of collaboration operations; the broker looks up the
immutable context from the capability and never trusts actor/owner/project/role
fields supplied by a model tool call.  It reuses the existing routing,
``submit_task`` and delegation/acceptance core instead of duplicating it.

Honest boundary: the existing local controller authorization is the trust
boundary.  This is application-level ownership, not OS isolation.  A malicious
same-user Python process that imports these private modules can bypass it; the
workbench does not claim to resist arbitrary same-user scripts.
"""
from __future__ import annotations

import secrets
import threading

import brain_context
import project_scope
import project_storage

CAPABILITY_HEADER = 'X-Brain-Capability'
MAX_CAPABILITIES = 64
MAX_ANSWER = 20000

OP_DESCRIBE = 'describe'
OP_CANDIDATES = 'candidates'
OP_PREVIEW = 'route_preview'
OP_DISPATCH = 'dispatch'
OP_STATUS = 'status'
OP_DELIVERY = 'delivery'
OP_REVIEW = 'review'
OP_REWORK = 'rework'
OPERATIONS = (OP_DESCRIBE, OP_CANDIDATES, OP_PREVIEW, OP_DISPATCH,
              OP_STATUS, OP_DELIVERY, OP_REVIEW, OP_REWORK)

_LOCK = threading.RLock()
_CAPABILITIES = {}


class BrokerError(ValueError):
    """A safe, user-facing broker refusal (never carries a credential)."""


class AuthError(BrokerError):
    """The private connection capability is missing/unknown; reconnect explicitly."""


def _now():
    import model_registry as registry
    return registry.now()


def _limits():
    import delegation_policy
    return {
        'dispatch_requires_acceptance': True,
        'review_requires_independent_evidence': True,
        'max_auto_rework': delegation_policy.MAX_AUTO_REWORK,
        'getters_wait': False,
        'dispatch_returns_original_task_id': True,
        'auto_rework_dispatch': False,
        'note': ('主脑接口只提供固定的协作操作；状态/交付读取不等待模型、不重复派发。'
                 '本机控制器授权是信任边界，不是 OS 沙箱。'),
    }


def _register(context):
    capability = secrets.token_urlsafe(32)
    with _LOCK:
        while len(_CAPABILITIES) >= MAX_CAPABILITIES:
            _CAPABILITIES.pop(next(iter(_CAPABILITIES)))
        _CAPABILITIES[capability] = {'context': context, 'registered_at': _now()}
    return capability


def register_connection(declaration):
    """Controller-authorized registration of one private capability.

    Callable only from the panel's own token-protected route, never from a model
    tool.  The returned capability must stay inside the MCP transport process.
    """
    try:
        context = brain_context.from_declaration(declaration)
    except brain_context.ContextError as error:
        raise BrokerError(str(error)) from None
    capability = _register(context)
    return {'capability': capability, 'context': context.public(),
            'registered_at': _now(), 'limits': _limits()}


def _connection(capability):
    if not isinstance(capability, str) or not capability or len(capability) > 512:
        raise AuthError('主脑连接能力无效或已失效；请显式重新连接')
    with _LOCK:
        record = _CAPABILITIES.get(capability)
    if record is None:
        raise AuthError('主脑连接能力无效或已失效（控制器可能已重启）；请显式重新连接')
    return record


def handle(operation, payload, capability):
    """Execute exactly one fixed operation for a validated capability."""
    record = _connection(capability)
    context = record['context']
    try:
        brain_context.require_operation(context, operation)
    except brain_context.ContextError as error:
        raise BrokerError(str(error)) from None
    if not isinstance(payload, dict):
        raise BrokerError('操作参数需要是对象')
    handler = {
        OP_DESCRIBE: _describe,
        OP_CANDIDATES: _candidates,
        OP_PREVIEW: _preview,
        OP_DISPATCH: _dispatch,
        OP_STATUS: _status,
        OP_DELIVERY: _delivery,
        OP_REVIEW: _review,
        OP_REWORK: _rework,
    }.get(operation)
    if handler is None:
        raise BrokerError('未知的主脑操作；只允许固定的协作接口')
    import brain_requests
    try:
        result = handler(context, payload)
    except brain_requests.UnknownOutcome:
        raise
    except brain_requests.RequestError as error:
        raise BrokerError(str(error)) from None
    except ValueError as error:
        raise BrokerError(str(error)) from None
    if not isinstance(result, dict):
        raise BrokerError('主脑操作返回了意外的结构')
    return result


# ------------------------------------------------------------------- validation

def _keys(payload, allowed, required=()):
    if not isinstance(payload, dict):
        raise BrokerError('操作参数需要是对象')
    unknown = set(payload) - set(allowed)
    if unknown:
        raise BrokerError('操作参数含未知字段：' + '、'.join(sorted(unknown)))
    missing = [name for name in required if payload.get(name) in (None, '')]
    if missing:
        raise BrokerError('缺少必要参数：' + '、'.join(missing))


def _text(value, limit):
    return str(value if value is not None else '').strip()[:limit]


def _string_list(value, label, minimum, maximum):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise BrokerError('%s 需要是 %d 到 %d 项数组' % (label, minimum, maximum))
    out = []
    for item in value:
        text = _text(item, 500)
        if not text:
            raise BrokerError('%s 不能包含空项' % label)
        out.append(text)
    return out


# --------------------------------------------------------------------- ownership

def _brain_meta(task):
    brain = task.get('brain') if isinstance(task.get('brain'), dict) else {}
    owner = str(brain.get('owner') or task.get('owner') or '').strip()
    project = str(brain.get('project') or task.get('project') or '').strip()
    owned = bool(brain) or bool(owner) or str(task.get('source') or '') == 'brain'
    return owned, owner, project


def _task(context, task_id, *, read_only=False):
    """Read project history; mutations still require original responsibility."""
    task_id = brain_context.clean_task_id(task_id)
    run = project_storage.task_dir(context.project, task_id)
    if not run.is_dir():
        raise BrokerError('任务不存在或不属于当前项目')
    import model_registry as registry
    try:
        task = registry.read_task(run)
    except FileNotFoundError:
        raise BrokerError('任务记录已删除或不存在') from None
    except (OSError, ValueError):
        raise BrokerError('任务记录暂时不可读，请稍后重试') from None
    owned, owner, project = _brain_meta(task)
    if not owned:
        raise BrokerError('旧任务没有主脑归属，不能由主脑接口读取、验收或整改；不会自动收养')
    if not owner or not (task.get('brain') or {}).get('binding_key'):
        raise BrokerError('任务的主脑归属记录不完整，拒绝操作')
    if project:
        try:
            mismatch = project_scope.project_key(project) != context.project_key
        except (ValueError, OSError, TypeError):
            raise BrokerError('任务的项目归属无法核实，拒绝操作') from None
        if mismatch:
            raise BrokerError('任务属于另一个项目；拒绝跨项目读取或验收')
    else:
        raise BrokerError('任务缺少可核对的项目归属，拒绝操作')
    if task.get('project'):
        try:
            if project_scope.project_key(task['project']) != context.project_key:
                raise BrokerError('任务的项目归属字段不一致，拒绝操作')
        except (ValueError, OSError, TypeError):
            raise BrokerError('任务的项目归属无法核实，拒绝操作') from None
    if read_only and context.role == 'brain':
        return run, task
    if owner != context.owner:
        raise BrokerError('任务属于另一个主脑拥有者；拒绝跨拥有者变更')
    if (task.get('brain') or {}).get('binding_key') != context.binding_key:
        raise BrokerError('任务属于另一个宿主或责任会话；拒绝跨会话变更')
    return run, task


def _read_access(context, task):
    same=((task.get('brain') or {}).get('owner', task.get('owner')) == context.owner
          and (task.get('brain') or {}).get('binding_key') == context.binding_key)
    return {'mode':'original_owner' if same else 'project_read_only', 'read_only':not same,
            'note':('变更仍需原权限与任务状态校验。' if same else
                    '同项目历史只读；不授予验收、整改、继续执行或收养权限。')}


def _task_receipt(task):
    return {
        'task_id': task.get('id'),
        'status': task.get('status'),
        'phase': task.get('phase'),
        'owner': task.get('owner') or (task.get('brain') or {}).get('owner'),
        'request_id': task.get('request_id'),
        'model_id': task.get('model_id'),
        'model_name': task.get('model_name') or task.get('model'),
        'engine': task.get('engine'),
        'kind': task.get('kind'),
        'project': task.get('project'),
        'accepted': False,
        'review_state': task.get('review_state'),
    }


def _find_task_receipt(context, request_id):
    """Recover only an existing task attributable to this owner and request."""
    runs = project_storage.runs_dir(context.project)
    if not runs.is_dir():
        return None
    matches = []
    for folder in runs.iterdir():
        if not folder.is_dir() or project_storage._is_link(folder):
            continue
        try:
            task = project_storage.read_task_record(folder)
        except (OSError, ValueError, FileNotFoundError):
            continue
        if not isinstance(task, dict):
            continue
        brain = task.get('brain') if isinstance(task.get('brain'), dict) else {}
        if (task.get('request_id') == request_id and brain.get('owner') == context.owner
                and brain.get('binding_key') == context.binding_key):
            try:
                _task(context, folder.name)
            except (BrokerError, ValueError):
                continue
            matches.append(_task_receipt(task))
    return matches[0] if len(matches) == 1 else None


# ----------------------------------------------------------------- broker ops

def _describe(context, payload):
    _keys(payload, ())
    with _LOCK:
        count = sum(1 for row in _CAPABILITIES.values() if row['context'] is context)
    return {'connected': True, 'context': context.public(), 'limits': _limits(),
            'history_policy': {'read':'same_project_brain_tasks',
                               'mutate':'original_owner_and_responsibility_binding',
                               'automatic_adoption':False},
            'connection': {'capabilities_for_context': count},
            'operations': list(OPERATIONS)}


def _settings():
    import workflow_settings
    return workflow_settings.snapshot()


def _public_route(route):
    return {
        'mode': route.get('mode'),
        'model_id': route.get('model_id'),
        'engine': route.get('engine'),
        'agent_source': route.get('agent_source'),
        'agent_label': route.get('agent_label'),
        'model_default_agent': route.get('model_default_agent'),
        'global_agent': route.get('global_agent'),
        'routing_preset': route.get('routing_preset'),
        'reason': route.get('reason'),
        'warnings': route.get('warnings') or [],
        'excluded': route.get('excluded') or [],
        'estimated_cost_cny': route.get('estimated_cost_cny'),
        'budget_cny': route.get('budget_cny'),
        'budget_enforced': route.get('budget_enforced'),
        'pricing': (route.get('cost') or {}).get('pricing'),
        'single_request_output': route.get('single_request_output'),
        'model_scope': route.get('model_scope'),
        'note': '这是路由预览，不派发任务、不等待模型。',
    }


def _candidates(context, payload):
    import execution_endpoints
    endpoint_controls = execution_endpoints.snapshot()
    _keys(payload, ())
    import execution_engines
    import model_agent_defaults
    import model_registry as registry
    import model_scope
    settings = _settings()
    try:
        scope = model_scope.normalize(settings.get('model_scope', model_scope.DEFAULT_SCOPE))
    except ValueError as error:
        raise BrokerError('执行模型范围设置无效，已拒绝候选查询：' + str(error)) from None
    data = registry.list_models()
    ready = {}

    def agent_ready(engine):
        if engine not in ready:
            try:
                ready[engine] = execution_engines.availability(engine)
            except Exception as error:
                ready[engine] = {'ready': False, 'message': str(error)[:300]}
        return ready[engine]

    rows = []
    paid_allowed = bool(settings.get('paid_allowed'))
    for model in data.get('models') or []:
        try:
            engine, source = execution_engines.resolution({}, settings, model=model)
        except ValueError as error:
            engine, source = 'invalid', 'invalid'
            agent = {'ready': False, 'message': str(error)[:300]}
        else:
            agent = agent_ready(engine) if engine in execution_engines.AGENTS else {'ready': False, 'message': '不是 Agent 执行器'}
        paid = model.get('provider') != 'local'
        scope_ok = model_scope.matches(scope, paid)
        enabled = bool(model.get('enabled', True))
        if engine != 'invalid' and endpoint_controls['readable']:
            model = execution_endpoints.apply_preset(model, engine, endpoint_controls)
        default_agent = model_agent_defaults.describe(model, settings)
        endpoint_error = ''
        try:
            execution_endpoints.gate(engine, model, settings, controls=endpoint_controls, automatic=True)
        except ValueError as error:
            endpoint_error = str(error)
        selectable = bool(enabled and scope_ok and agent.get('ready') and not endpoint_error)
        native_policy_error = ''
        if model.get('provider') == 'workbuddy':
            import workbuddy_executor
            try:
                workbuddy_executor.authorize_native_chat(settings)
            except ValueError as error:
                native_policy_error = str(error)
        reasons = []
        if endpoint_error:
            reasons.append(endpoint_error)
        if not enabled:
            reasons.append('模型已停用')
        if not scope_ok:
            reasons.append(model_scope.reason(scope, paid))
        if not agent.get('ready'):
            reasons.append(_text(agent.get('message'), 300) or '执行 Agent 未就绪')
        rows.append({
            'model_id': model.get('id'), 'name': model.get('name'),
            'provider': model.get('provider'), 'model': model.get('model'),
            'paid': paid, 'enabled': enabled,
            'api_key_set': bool(model.get('api_key_set')),
            'engine': engine, 'agent_source': source,
            'endpoint_allowed': not endpoint_error,
            'agent_label': execution_engines.LABELS.get(engine, engine),
            'agent_ready': bool(agent.get('ready')),
            'agent_message': _text(agent.get('message'), 300),
            'default_agent': default_agent,
            'scope_ok': scope_ok,
            # Selectability is about the model/Agent/scope, not about provider
            # or name deduplication.  paid_allowed is a separate authorization
            # and must not make an in-scope model look absent.
            'selectable': selectable,
            'selectable_reason': '；'.join(reasons),
            'paid_allowed': paid_allowed,
            'dispatch_ready': bool(selectable and (not paid or paid_allowed) and not native_policy_error),
            'execution_capabilities': agent.get('capabilities'),
            'billing_source': 'workbuddy-native-account' if model.get('provider') == 'workbuddy' else 'model-api',
            'authorization_note': native_policy_error or ('付费执行未开启；这是独立授权，不是范围或候选排除。'
                                   if paid and not paid_allowed else ''),
            'max_input_tokens': model.get('max_input_tokens'),
            'max_output_tokens': model.get('max_output_tokens'),
            'model_version': model.get('model_version') or '',
            'preferred_tasks': model.get('preferred_tasks') or [],
            'capability_notes': model.get('capability_notes') or '',
            'max_task_complexity': model.get('max_task_complexity'),
            'cost_preference': model.get('cost_preference'),
            'routing_preset': model.get('routing_preset'),
            'identity_state': 'declared',
        })
    default_model = data.get('default_model')
    default_row = next((row for row in rows if row['model_id'] == default_model), None)
    in_scope_ids = [row['model_id'] for row in rows if row['scope_ok']]
    selectable_ids = [row['model_id'] for row in rows if row['selectable']]
    dispatch_ready_ids = [row['model_id'] for row in rows if row['dispatch_ready']]
    if not default_model:
        default_note = '尚未配置手动默认模型；省略 model_id 时无法派发。'
    elif default_row is None:
        default_note = '手动默认模型不在已配置模型列表中；省略 model_id 时不会自动换模型。'
    elif not default_row['scope_ok']:
        hint = ('；范围内已配置候选：' + '、'.join(in_scope_ids)) if in_scope_ids else '；范围内当前没有已配置候选'
        default_note = ('手动默认模型 %s 在执行模型范围“%s”之外；省略 model_id 时不会自动换模型。'
                        '请显式提供范围内的 model_id（见 selectable_model_ids）或调整默认/范围%s。'
                        % (default_model, model_scope.LABELS[scope], hint))
    elif not default_row['selectable']:
        default_note = ('手动默认模型 %s 在范围内，但当前不可选择：%s'
                        % (default_model, default_row['selectable_reason'] or '状态未就绪'))
    else:
        default_note = '手动默认模型在范围内；省略 model_id 时仍使用该默认模型。'
    return {'models': rows, 'default_model': default_model,
            'workflow': {key: settings.get(key) for key in
                         ('enabled', 'readable', 'model_scope', 'paid_allowed',
                          'enforce_paid_budget', 'enforce_model_call_limit', 'execution_agent')},
            'default_model_scope_ok': bool(default_row and default_row['scope_ok']),
            'default_model_note': default_note,
            'paid_allowed': paid_allowed,
            'in_scope_model_ids': in_scope_ids,
            'selectable_model_ids': selectable_ids,
            'dispatch_ready_model_ids': dispatch_ready_ids,
            'execution_agent': settings.get('execution_agent'),
            'scope': model_scope.describe(scope),
            'note': ('候选按已保存默认执行 Agent 解析；这里不启动服务、不调用模型、不派发。'
                     '不按同名或同 provider 去重/排除，subagent deny 不影响普通候选；'
                     '默认模型在范围外不等于范围内没有候选。历史提示无法复现时保留未知，'
                     '请以真实候选和 brain_route_preview 为准。')}


def _route_body(context, payload, require_model):
    allowed = ('model_id', 'prompt', 'kind', 'files', 'acceptance')
    _keys(payload, allowed)
    model_id = _text(payload.get('model_id'), 80)
    if require_model and not model_id:
        raise BrokerError('路由预览需要明确 model_id；不会退回到另一个模型')
    kind = payload.get('kind', 'edit')
    if kind not in ('edit', 'chat'):
        raise BrokerError('任务类型只能是 edit 或 chat')
    files = payload.get('files') or []
    if not isinstance(files, list) or len(files) > 30 or any(not isinstance(item, str) or not item.strip() for item in files):
        raise BrokerError('文件范围需要是 0 到 30 个非空字符串')
    acceptance = payload.get('acceptance')
    if acceptance is not None:
        acceptance = _string_list(acceptance, '验收条件', 1, 20)
    body = {'project': context.project, 'prompt': _text(payload.get('prompt'), 50000),
            'kind': kind, 'files': [item.strip() for item in files], 'route_mode': 'manual'}
    if model_id:
        body['model_id'] = model_id
    if acceptance is not None:
        body['routing'] = {'acceptance': acceptance}
    return body, acceptance


def _default_scope_error(models, settings, default_model):
    """Explain an out-of-scope manual default without claiming the scope is empty."""
    import model_scope
    scope = model_scope.normalize(settings.get('model_scope', model_scope.DEFAULT_SCOPE))
    row = next((model for model in models if model.get('id') == default_model), None)
    if row is None:
        return None
    if model_scope.matches(scope, row.get('provider') != 'local'):
        return None
    ids = [model.get('id') for model in models
           if model.get('enabled', True)
           and model_scope.matches(scope, model.get('provider') != 'local')]
    hint = ('；范围内已配置候选：' + '、'.join(ids)) if ids else '；范围内当前没有已配置候选'
    return ('省略 model_id 时使用手动默认模型 %s，但它在执行模型范围“%s”之外；不会自动更换模型。'
            '请显式提供范围内的 model_id（候选见 brain_candidates/selectable_model_ids）'
            '或调整手动默认模型/执行模型范围%s。'
            % (default_model, model_scope.LABELS[scope], hint))


def _choose_route(context, body):
    import budget_relay
    import deepseek_billing
    import model_registry as registry
    import model_router
    import panel_server as panel
    settings = _settings()
    data = deepseek_billing.enrich_models(registry.list_models())
    if not body.get('model_id'):
        default_model = data.get('default_model')
        if not default_model:
            raise BrokerError('尚未配置手动默认模型；请显式提供 model_id（可先调用 brain_candidates 查看候选）或设置默认模型')
        scope_error = _default_scope_error(data.get('models') or [], settings, default_model)
        if scope_error:
            raise BrokerError(scope_error)
        body = {**body, 'model_id': default_model}
    daily = budget_relay.daily_state(settings)
    return model_router.choose(body, data['models'], settings, panel.public_tasks(),
                               daily['remaining_cny'], daily_uncertain=daily.get('unresolved', False))


def _preview(context, payload):
    body, _acceptance = _route_body(context, payload, require_model=True)
    try:
        route = _choose_route(context, body)
    except ValueError as error:
        raise BrokerError(str(error)) from None
    if route.get('engine') not in ('dsh', 'pi', 'workbuddy'):
        raise BrokerError('主脑派发需要 Agent 执行器，当前模型解析到 %s；不会静默回退'
                          % route.get('engine'))
    return {'preview': _public_route(route), 'project': context.project}


def _dispatch(context, payload):
    allowed = ('request_id', 'prompt', 'model_id', 'kind', 'files', 'acceptance',
               'title')
    _keys(payload, allowed, required=('request_id', 'prompt'))
    request_id = brain_context.clean_request_id(payload.get('request_id'))
    prompt = _text(payload.get('prompt'), 50000)
    if not prompt:
        raise BrokerError('请填写任务内容，最多 50000 个字符')
    kind = payload.get('kind', 'edit')
    if kind not in ('edit', 'chat'):
        raise BrokerError('任务类型只能是 edit 或 chat')
    files = payload.get('files') or []
    if not isinstance(files, list) or len(files) > 30 or any(not isinstance(item, str) or not item.strip() for item in files):
        raise BrokerError('文件范围需要是 0 到 30 个非空字符串')
    if kind == 'edit' and not files:
        raise BrokerError('修改任务至少需要一个项目内文件；不会猜测文件范围')
    acceptance = _string_list(payload.get('acceptance'), '验收条件', 1, 20)
    model_id = _text(payload.get('model_id'), 80)
    title = _text(payload.get('title'), 100)
    journal_payload = {'prompt': prompt, 'kind': kind,
                       'files': [item.strip() for item in files],
                       'model_id': model_id, 'acceptance': acceptance,
                       'title': title}
    import brain_requests
    existing = brain_requests.replay(context.project, context.binding_key, 'dispatch',
                                     request_id, journal_payload)
    if existing is not None:
        receipt = existing.get('receipt') or {}
        return {'operation': OP_DISPATCH, 'request_id': request_id, 'owner': context.owner,
                'reused': True, 'receipt': receipt, 'task': receipt,
                'next': '用 brain_status / brain_delivery 读取，不等待模型。'}
    body = {'prompt': prompt, 'kind': kind, 'files': [item.strip() for item in files],
            'project': context.project, 'route_mode': 'manual',
            'routing': {'acceptance': acceptance}, 'source': 'brain'}
    if not _settings().get('enabled'):
        raise BrokerError('协作总开关已关闭，主脑接口不会派发新任务')
    if model_id:
        body['model_id'] = model_id
    if title:
        body['title'] = title
    route = _choose_route(context, body)
    if route.get('engine') not in ('dsh', 'pi', 'workbuddy'):
        raise BrokerError('主脑派发需要 Agent 执行器，当前模型解析到 %s；不会静默回退'
                          % route.get('engine'))

    import panel_server as panel

    def effect():
        row = panel.submit_task(body, trusted=context,
                                authority=brain_context.issue_authority(context),
                                request_id=request_id)
        return _task_receipt(row)

    row = brain_requests.run_once(
        context.project, context.binding_key, 'dispatch', request_id, journal_payload,
        effect, recover=lambda: _find_task_receipt(context, request_id),
        reject_on=(panel.DispatchRefused,))
    receipt = row.get('receipt') or {}
    return {'operation': OP_DISPATCH, 'request_id': request_id, 'owner': context.owner,
            'reused': bool(row.get('reused')), 'receipt': receipt, 'task': receipt,
            'next': '用 brain_status / brain_delivery 读取，不等待模型。'}


def _native_execution(task):
    if task.get('engine') != 'workbuddy':
        return None
    native = task.get('workbuddy') or {}
    return {'session_id': task.get('workbuddy_session_id'), 'model': task.get('model'),
            'version': task.get('executor_version'), 'billing_source': 'workbuddy-native-account',
            'credits_used': None, 'actual_model_calls': None, 'cost_cny': None,
            'cleanup': native.get('cleanup'), 'prompt_attempted': bool(native.get('prompt_attempted')),
            'outcome_unknown': bool(native.get('outcome_unknown'))}


def _status(context, payload):
    _keys(payload, ('task_id',), required=('task_id',))
    import delegation_policy
    run, task = _task(context, payload.get('task_id'), read_only=True)
    record, error = delegation_policy.safe_load(run)
    return {'task_id': task.get('id'), 'status': task.get('status'), 'phase': task.get('phase'),
            'owner': task.get('owner'), 'request_id': task.get('request_id'),
            'access':_read_access(context, task),
            'project': task.get('project'), 'engine': task.get('engine'),
            'model_id': task.get('model_id'), 'model_name': task.get('model_name') or task.get('model'),
            'kind': task.get('kind'), 'files': task.get('files') or [],
            'created_at': task.get('created_at'), 'started_at': task.get('started_at'),
            'ended_at': task.get('ended_at'), 'error': _text(task.get('error'), 1500),
            'execution_error': _text(task.get('error'), 1500),
            'native_execution': _native_execution(task),
            'review': delegation_policy.panel_summary(record) if record else None,
            'review_error': _text(error, 500),
            'note': '状态读取不等待模型、不重复派发；工具超时不是任务总时限。'}


def _delivery(context, payload):
    _keys(payload, ('task_id',), required=('task_id',))
    import delegation_policy
    import task_handover
    run, task = _task(context, payload.get('task_id'), read_only=True)
    record, review_error = delegation_policy.safe_load(run)
    answer = ''
    for name in ('answer.txt', 'result.md'):
        path = run / name
        if path.is_file():
            try:
                answer = path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                answer = ''
            break
    if not answer:
        answer = str(task.get('answer') or '')
    truncated = len(answer) > MAX_ANSWER
    accepted = False
    problems = []
    independent = 0
    if record is not None:
        accepted = (record.get('state') in delegation_policy.ACCEPTED_STATES
                    and not delegation_policy.acceptance_problems(record))
        problems = delegation_policy.acceptance_problems(record)
        for item in record.get('criteria') or []:
            independent += sum(1 for entry in item.get('evidence') or []
                               if entry.get('source') in delegation_policy.INDEPENDENT_SOURCES)
    handover = None
    handover_record, handover_error = task_handover.safe_load(run)
    if handover_record is not None:
        try:
            verification = task_handover.verify(run, handover_record)
        except Exception as error:
            verification = {'valid': False, 'stale': True, 'message': str(error)[:500]}
        handover = {'record': handover_record, 'verification': verification}
    executor = (record or {}).get('executor') or {}
    if accepted:
        state = 'accepted'
    elif record is not None and record.get('state') == delegation_policy.EXECUTOR_COMPLETED:
        state = 'executor_completed_unaccepted'
    elif task.get('status') in ('queued', 'running'):
        state = 'in_progress'
    elif task.get('status') in ('failed', 'cancelled'):
        state = 'executor_failed'
    else:
        state = 'not_ready'
    return {'task_id': task.get('id'), 'status': task.get('status'), 'phase': task.get('phase'),
            'owner': task.get('owner'), 'request_id': task.get('request_id'),
            'executor': {'status': executor.get('status'),
                         'changed_files': executor.get('changed_files') or [],
                         'test': executor.get('test')},
            'answer': answer[:MAX_ANSWER], 'answer_truncated': truncated,
            'engine': task.get('engine'), 'model_id': task.get('model_id'),
            'native_execution': _native_execution(task),
            'execution_error': _text(task.get('error'), 1500),
            'review_state': (record or {}).get('state'),
            'review': delegation_policy.panel_summary(record) if record else None,
            'review_error': _text(review_error, 500),
            'accepted': accepted, 'acceptance_problems': problems,
            'independent_evidence': independent,
            'delivery_state': state, 'handover': handover, 'access':_read_access(context, task),
            'note': ('执行器完成不等于验收通过；主脑需要逐条独立证据。'
                     if state == 'executor_completed_unaccepted' else
                     '交付读取不等待模型、不重复派发。')}


def _review(context, payload):
    allowed = ('request_id', 'task_id', 'action', 'criteria', 'notes',
               'failed_criteria', 'problems', 'boundaries', 'reason')
    _keys(payload, allowed, required=('request_id', 'task_id', 'action'))
    request_id = brain_context.clean_request_id(payload.get('request_id'))
    task_id = brain_context.clean_task_id(payload.get('task_id'))
    action = payload.get('action')
    if action not in ('accept', 'final_accept', 'return', 'block'):
        raise BrokerError('验收操作只能是 accept / final_accept / return / block')
    run, _task_row = _task(context, task_id)
    criteria = payload.get('criteria') or []
    if criteria and not isinstance(criteria, list):
        raise BrokerError('验收结论需要是数组')
    failed = payload.get('failed_criteria') or []
    if failed and not isinstance(failed, list):
        raise BrokerError('未通过条件需要是数组')
    body = {'action': action, 'criteria': criteria,
            'notes': _text(payload.get('notes'), 2000),
            'failed_criteria': failed,
            'problems': _text(payload.get('problems'), 2000),
            'boundaries': _text(payload.get('boundaries'), 1000),
            'reason': _text(payload.get('reason'), 2000)}
    journal_payload = {'task_id': task_id, **{key: body[key] for key in
                                              ('action', 'criteria', 'notes', 'failed_criteria',
                                               'problems', 'boundaries', 'reason')}}

    def effect():
        import panel_server as panel
        result = panel.update_review(task_id, body,
                                     authority=brain_context.issue_authority(context),
                                     request_id=request_id)
        return {'task_id': task_id, 'owner': context.owner, 'request_id': request_id,
                'action': action, 'review': result.get('review'), 'summary': result.get('summary')}

    def recover():
        import delegation_policy
        record = delegation_policy.find_request(run, request_id)
        if record is None:
            return None
        return {'task_id': task_id, 'owner': context.owner, 'request_id': request_id,
                'action': action, 'review': record,
                'summary': delegation_policy.panel_summary(record)}

    import brain_requests
    import panel_server as panel
    existing = brain_requests.replay(context.project, context.binding_key, 'review',
                                     request_id, journal_payload)
    if existing is not None:
        receipt = existing.get('receipt') or {}
        return {'operation': OP_REVIEW, 'request_id': request_id, 'owner': context.owner,
                'reused': True, 'receipt': receipt, 'task': receipt}
    row = brain_requests.run_once(context.project, context.binding_key, 'review', request_id,
                                  journal_payload, effect, recover=recover,
                                  reject_on=(panel.ReviewRefused,))
    receipt = row.get('receipt') or {}
    return {'operation': OP_REVIEW, 'request_id': request_id, 'owner': context.owner,
            'reused': bool(row.get('reused')), 'receipt': receipt, 'task': receipt}


def _rework(context, payload):
    _keys(payload, ('request_id', 'parent_task_id'), required=('request_id', 'parent_task_id'))
    request_id = brain_context.clean_request_id(payload.get('request_id'))
    parent_id = brain_context.clean_task_id(payload.get('parent_task_id'))
    import delegation_policy
    run, parent = _task(context, parent_id)
    journal_payload = {'parent_task_id': parent_id, 'model_id': parent.get('model_id'),
                       'kind': parent.get('kind') or 'edit',
                       'files': parent.get('files') or [], 'engine': parent.get('engine')}

    def effect():
        # State/claim checks live inside the effect so an identical completed
        # request is served from the journal without consuming anything twice.
        if not _settings().get('enabled'):
            raise delegation_policy.ReworkClaimRefused('协作总开关已关闭，不派发整改')
        record = delegation_policy.load(run)
        if record is None or record.get('state') != delegation_policy.RETURNED:
            raise delegation_policy.ReworkClaimRefused('原任务当前不是退回整改状态，不能派发整改')
        rework = record.get('rework') or {}
        if rework.get('dispatched_task_id'):
            raise delegation_policy.ReworkClaimRefused(
                '整改已经派发为任务 ' + str(rework['dispatched_task_id']) + '；一次整改只能派发一次')
        if parent.get('status') in ('queued', 'running'):
            raise delegation_policy.ReworkClaimRefused('原任务还在执行，不能派发整改')
        executor = record.get('executor') or {}
        deliverable = {'summary': _delivery_answer(run),
                       'changed_files': executor.get('changed_files') or []}
        prompt = delegation_policy.rework_prompt(record, deliverable)
        body = {'prompt': prompt, 'kind': parent.get('kind') or 'edit',
                'files': parent.get('files') or [], 'model_id': parent.get('model_id'),
                'route_mode': 'manual', 'project': context.project,
                'rework_of': parent_id, 'source': 'brain',
                'title': ('整改：' + str(parent.get('title') or ''))[:100]}
        import panel_server as panel
        row = panel.submit_task(body, trusted=context,
                                authority=brain_context.issue_authority(context),
                                request_id=request_id)
        return _task_receipt(row)

    import brain_requests
    existing = brain_requests.replay(context.project, context.binding_key, 'rework',
                                     request_id, journal_payload)
    if existing is not None:
        receipt = existing.get('receipt') or {}
        return {'operation': OP_REWORK, 'request_id': request_id, 'owner': context.owner,
                'reused': True, 'receipt': receipt, 'task': receipt}
    row = brain_requests.run_once(
        context.project, context.binding_key, 'rework', request_id, journal_payload,
        effect, recover=lambda: _find_task_receipt(context, request_id),
        reject_on=(delegation_policy.ReworkClaimRefused,))
    receipt = row.get('receipt') or {}
    return {'operation': OP_REWORK, 'request_id': request_id, 'owner': context.owner,
            'reused': bool(row.get('reused')), 'receipt': receipt, 'task': receipt,
            'note': '整改在同一模型/执行器上使用新会话；仍受一次整改限制。'}


def _delivery_answer(run):
    for name in ('answer.txt', 'result.md'):
        path = run / name
        if path.is_file():
            try:
                return path.read_text(encoding='utf-8', errors='replace')[-1500:]
            except OSError:
                return ''
    return ''
