"""Global execution-endpoint controls, shared by every brain and task entry.

No model calls, service startup, credential access or writes on inspection.
Missing configuration preserves DSH/Pi behaviour; a new adapter starts disabled.
"""
import json
from pathlib import Path

import execution_engines
import model_registry as registry

IDS = ('pi', 'dsh', 'workbuddy')
LABELS = {**execution_engines.LABELS, 'workbuddy': 'WorkBuddy'}
CONFIG_NAME = 'execution-endpoints.json'


def defaults():
    return {key: {'enabled': key != 'workbuddy', 'current_model': None} for key in IDS}


def snapshot():
    path = Path(registry.HERE) / CONFIG_NAME
    try:
        if not path.exists():
            return {'endpoints': defaults(), 'readable': True, 'error': ''}
        raw = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or raw.get('schema') != 1 or not isinstance(raw.get('endpoints'), dict):
            raise ValueError('执行端配置结构无效')
        result = defaults()
        if set(raw['endpoints']) - set(IDS):
            raise ValueError('执行端配置含未知执行端')
        for key, value in raw['endpoints'].items():
            if not isinstance(value, dict) or set(value) != {'enabled', 'current_model'}:
                raise ValueError('执行端配置字段无效')
            if type(value['enabled']) is not bool or (value['current_model'] is not None and
                    (not isinstance(value['current_model'], str) or not value['current_model'])):
                raise ValueError('执行端配置值无效')
            result[key] = dict(value)
        return {'endpoints': result, 'readable': True, 'error': ''}
    except (OSError, ValueError, TypeError):
        return {'endpoints': {key: {'enabled': False, 'current_model': None} for key in IDS},
                'readable': False, 'error': '执行端配置无法读取；已阻止新派发，请检查配置'}


def associated(model, settings):
    return execution_engines.resolution({}, settings, model=model)[0]


def gate(engine, model, settings, *, controls=None, automatic=False):
    controls = controls if controls is not None else snapshot()
    if not controls['readable']:
        raise ValueError(controls['error'])
    # Legacy direct/Aider calls must not bypass the model's disabled endpoint.
    key = engine if engine in IDS else associated(model, settings)
    row = controls['endpoints'].get(key)
    if row and not row['enabled']:
        raise ValueError(LABELS[key] + ' 执行端已禁用；不会切换到其他执行端')
    if key == 'workbuddy':
        raise ValueError('WorkBuddy 原生文件验证已通过，但正式队列尚未接入；不会改用 API 或 Auto')
    if automatic and row and row['current_model'] and model.get('id') != row['current_model']:
        raise ValueError(LABELS[key] + ' 当前模型为 ' + row['current_model'] + '；此模型未选作该执行端的当前模型')
    return key


def update(endpoint, body, settings):
    if endpoint not in IDS or not isinstance(body, dict) or not body or set(body) - {'enabled', 'current_model'}:
        raise ValueError('执行端设置无效')
    if 'enabled' in body and type(body['enabled']) is not bool:
        raise ValueError('执行端开关必须是布尔值')
    if 'current_model' in body:
        selected = body['current_model']
        if selected is not None:
            if endpoint == 'workbuddy' or not isinstance(selected, str):
                raise ValueError('当前模型无效')
            models = registry.list_models()['models']
            model = next((m for m in models if m['id'] == selected), None)
            if not model or not model.get('enabled', True) or associated(model, settings) != endpoint:
                raise ValueError('请选择属于此执行端且已启用的模型')
    with registry.file_lock(Path(registry.HERE) / 'execution-endpoints.lock'):
        old = snapshot()
        if not old['readable']:
            raise ValueError(old['error'])
        old['endpoints'][endpoint].update(body)
        registry.write_json(Path(registry.HERE) / CONFIG_NAME, {'schema': 1, 'endpoints': old['endpoints']})
    return {'ok': True, 'endpoint': endpoint, **old['endpoints'][endpoint]}


def public_rows(models, settings, agents=None):
    controls = snapshot()
    agents = execution_engines.public_agents() if agents is None else agents
    rows = []
    assigned = set()
    for key in IDS:
        group = []
        for model in models:
            try:
                if associated(model, settings) == key:
                    group.append(model)
                    assigned.add(model['id'])
            except ValueError:
                pass
        pref = controls['endpoints'][key]
        enabled_models = [m for m in group if m.get('enabled', True)]
        selected = next((m for m in group if m['id'] == pref['current_model']), None)
        # No migration: several legacy models remain eligible until explicitly selected.
        display = selected if pref['current_model'] else enabled_models[0] if len(enabled_models) == 1 else None
        info = agents.get(key, {})
        rows.append({'id': key, 'name': LABELS[key], **pref, 'models': group,
                     'model': display, 'runtime': info, 'ready': bool(info.get('ready')),
                     'readable': controls['readable'], 'error': controls['error'],
                     'selection_error': ('当前模型已移除或改属其他执行端，请重新选择'
                                         if pref['current_model'] and selected is None else
                                         '当前模型已停用，请启用或重新选择' if selected and not selected.get('enabled', True) else ''),
                     'billing_source': 'workbuddy-native-account' if key == 'workbuddy' else 'model-provider',
                     'balance': None,
                     'message': '原生文件验证已通过；正式队列待接入，积分未同步' if key == 'workbuddy' else info.get('message', '')})
    return {'rows': rows, 'unassigned_models': [m for m in models if m['id'] not in assigned],
            'readable': controls['readable'], 'error': controls['error']}
