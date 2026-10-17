"""Execution-agent selection, independent of provider/model routing."""
from copy import deepcopy

AGENTS = ('dsh', 'pi')
ENGINES = ('dsh', 'pi', 'direct', 'aider')
DEFAULT = 'dsh'
LABELS = {'dsh': 'DeepSeek Harness', 'pi': 'Pi Agent', 'direct': '直接调用', 'aider': 'Aider'}

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
        raise ValueError('执行 Agent 无效；请选择 DeepSeek Harness 或 Pi Agent')
    return value.strip().lower()


def selected(settings):
    return normalize(settings.get('execution_agent', DEFAULT))


def resolve(body, settings):
    value = body['engine'] if 'engine' in body else selected(settings)
    if not isinstance(value, str) or value not in ENGINES:
        raise ValueError('执行方式无效；请选择已支持的执行 Agent')
    return value


def has_session(task):
    if task.get('engine') == 'pi':
        return bool(task.get('pi_session_id') and task.get('pi_session_file'))
    return task.get('engine') == 'dsh' and bool(task.get('dsh_session_id') and task.get('dsh_session_key'))


def session_fields(task):
    names = ('pi_session_id', 'pi_session_file') if task.get('engine') == 'pi' else ('dsh_session_key', 'dsh_session_id', 'relay_port')
    return {name: task[name] for name in (*names, 'connection_fingerprint') if task.get(name) is not None}


def availability(engine):
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
