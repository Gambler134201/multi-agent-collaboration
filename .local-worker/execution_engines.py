"""Execution-agent selection, independent of provider/model routing."""
from copy import deepcopy

AGENTS = ('dsh', 'pi', 'workbuddy')
ENGINES = ('dsh', 'pi', 'workbuddy', 'direct', 'aider')
DEFAULT = 'dsh'
# Explicit form value meaning "omit the override and let the selected model (then
# the global default) decide".  It is accepted as the absence of an override, not
# as an execution engine.
TASK_DEFAULT = 'model_default'
LABELS = {'dsh': 'DeepSeek Harness', 'pi': 'Pi Agent', 'workbuddy': 'WorkBuddy', 'direct': '直接调用', 'aider': 'Aider'}

# Each adapter declares its own controls. Model connection forms never interpret
# another adapter's configuration. The legacy DSH storage key stays compatible
# with existing task/session/evidence identities.
CONFIGURATION = {
    'dsh': {'description': '按模型选择 DeepSeek Harness 的运行预设；更改后用于新的 DSH 会话。已有会话需要保持原配置。',
            'model_fields': [{'key': 'agent_preset', 'label': '运行预设', 'type': 'select',
                              'default': 'standard', 'provider_defaults': {'local': 'native'},
                              'options': [{'value': 'native', 'label': '原生模式 · native'},
                                          {'value': 'standard', 'label': '标准模式 · standard'}],
                              'note': '原生模式保留工具并精简提示；使用 DSH 实际预设。不会改变 Pi 或其他 Agent。'}]},
    'pi': {'description': 'Pi 使用工作台管理的独立会话配置，目前没有需要手动设置的额外选项。模型、思考等级和用量限制继续使用工作台设置。',
           'model_fields': []},
    'workbuddy': {'description': '使用 WorkBuddy 登录账户的指定内置模型；仅限定文件读写，无命令/子代理/会话续接。积分与人民币费用未知。',
                 'model_fields': []},
}


def configuration(engine):
    if engine not in AGENTS:
        raise ValueError('未知执行 Agent；不能保存配置')
    return deepcopy(CONFIGURATION.get(engine, {'description': '此 Agent 暂无额外配置。', 'model_fields': []}))


def validate_configuration(engine, values):
    fields = {field['key']: field for field in configuration(engine)['model_fields']}
    if not isinstance(values, dict) or not values or set(values) - fields.keys():
        raise ValueError('请只提交此 Agent 支持的配置项')
    for key, value in values.items():
        field = fields[key]
        if field['type'] != 'select' or not isinstance(value, str) or value not in {o['value'] for o in field['options']}:
            raise ValueError('Agent 配置值无效：' + key)
    return dict(values)


def normalize(value):
    if not isinstance(value, str) or value.strip().lower() not in AGENTS:
        raise ValueError('执行 Agent 无效；请选择已支持的执行端')
    return value.strip().lower()


def selected(settings):
    return normalize(settings.get('execution_agent', DEFAULT))


def requested_engine(body):
    """Return an explicit task-level engine, or ``None`` for no override.

    A form/CLI that simply wants the selected model's preference must omit the
    field (or send ``model_default``); sending the current global value would be
    a real explicit override and is therefore avoided by the entry points.
    """
    if not isinstance(body, dict):
        raise ValueError('执行方式参数需要是对象')
    value = body.get('engine')
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('执行方式无效；请选择已支持的执行 Agent')
    text = value.strip().lower()
    if not text or text == TASK_DEFAULT:
        return None
    if text not in ENGINES:
        raise ValueError('执行方式无效；请选择已支持的执行 Agent')
    return text


def resolution(body, settings, model=None):
    """Resolve ``(engine, source)`` using explicit > model > global.

    ``source`` is ``explicit``/``model``/``global``.  A model preference only
    applies to agent engines; callers still validate direct/aider compatibility.
    """
    explicit = requested_engine(body)
    if explicit is not None:
        validate_model(explicit, model)
        return explicit, 'explicit'
    if model is not None:
        import model_agent_defaults
        value = model_agent_defaults.effective(model, settings)
        validate_model(value[0], model)
        return value
    return selected(settings), 'global'


def resolve(body, settings, model=None):
    return resolution(body, settings, model=model)[0]


def validate_model(engine, model):
    if model is not None and (engine == 'workbuddy') != (model.get('provider') == 'workbuddy'):
        raise ValueError('WorkBuddy 仅执行其原生账户模型；不能与 API 模型或其他执行端互换')


def has_session(task):
    if task.get('engine') == 'pi':
        return bool(task.get('pi_session_id') and task.get('pi_session_file'))
    return task.get('engine') == 'dsh' and bool(task.get('dsh_session_id') and task.get('dsh_session_key'))


def session_fields(task):
    names = ('pi_session_id', 'pi_session_file') if task.get('engine') == 'pi' else ('dsh_session_key', 'dsh_session_id', 'relay_port')
    return {name: task[name] for name in (*names, 'connection_fingerprint') if task.get(name) is not None}


def availability(engine):
    if engine == 'workbuddy':
        import workbuddy_worker
        return workbuddy_worker.availability()
    if engine == 'pi':
        import pi_worker
        return pi_worker.availability()
    if engine != 'dsh':
        raise ValueError('未知执行 Agent；不会切换到其他执行器')
    import dsh_worker
    return dsh_worker.availability()


def public_agents():
    return {engine: {**availability(engine), 'configuration': configuration(engine)} for engine in AGENTS}


def public_selection(settings):
    """Let a broken setting remain visible and repairable, without executing it."""
    try:
        return selected(settings), availability(selected(settings))
    except ValueError as error:
        return 'invalid', {'ready': False, 'message': str(error)}
