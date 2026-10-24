"""Global execution-endpoint controls, shared by every brain and task entry.

No model calls, service startup, credential access or writes on inspection.
Missing configuration preserves DSH/Pi behaviour; a new adapter starts disabled.
"""
import json
from pathlib import Path

import execution_engines
import model_registry as registry

IDS = ('pi', 'dsh', 'workbuddy', 'zcode')
LABELS = {**execution_engines.LABELS, 'workbuddy': 'WorkBuddy'}
CONFIG_NAME = 'execution-endpoints.json'
PRESET_FIELDS = ('preferred_tasks', 'capability_notes', 'cost_preference', 'max_task_complexity')


def validate_preset(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(PRESET_FIELDS):
        raise ValueError('执行端预设需完整包含任务类型、能力说明、成本定位和复杂度上限')
    tags = value['preferred_tasks']
    if (not isinstance(tags, list) or len(tags) > 12 or
            any(not isinstance(t, str) or not 1 <= len(t.strip()) <= 40 for t in tags)):
        raise ValueError('适合的任务最多 12 项，每项 1 到 40 字')
    if (not isinstance(value['capability_notes'], str) or len(value['capability_notes']) > 3000 or
            value['cost_preference'] not in ('economy', 'balanced', 'quality') or
            value['max_task_complexity'] not in ('simple', 'medium', 'complex')):
        raise ValueError('执行端能力与成本预设无效')
    return {**value, 'preferred_tasks': list(dict.fromkeys(t.strip() for t in tags)),
            'capability_notes': value['capability_notes'].strip()}


def apply_preset(model, engine, controls=None):
    controls = snapshot() if controls is None else controls
    if not controls['readable']:
        raise ValueError(controls['error'])
    preset = controls['endpoints'].get(engine, {}).get('preset')
    return {**model, **(preset or {}), 'routing_preset': {
        'source': 'endpoint' if preset is not None else 'legacy_model', 'endpoint': engine,
        'values': {key: (preset or model).get(key) for key in PRESET_FIELDS}}}


def defaults():
    return {key: {'enabled': key in ('pi', 'dsh'), 'current_model': None} for key in IDS}


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
            if (not isinstance(value, dict) or not {'enabled', 'current_model'} <= set(value) or
                    set(value) - {'enabled', 'current_model', 'preset'}):
                raise ValueError('执行端配置字段无效')
            if type(value['enabled']) is not bool or (value['current_model'] is not None and
                    (not isinstance(value['current_model'], str) or not value['current_model'])):
                raise ValueError('执行端配置值无效')
            result[key] = dict(value)
            if 'preset' in value:
                result[key]['preset'] = validate_preset(value['preset'])
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
    execution_engines.validate_model(key, model)
    if automatic and row and row['current_model'] and model.get('id') != row['current_model']:
        raise ValueError(LABELS[key] + ' 当前模型为 ' + row['current_model'] + '；此模型未选作该执行端的当前模型')
    return key


def update(endpoint, body, settings):
    if endpoint == 'zcode' and isinstance(body, dict):
        import zcode_worker
        if set(body) == {'access_mode'}:
            return zcode_worker.set_access_mode(body['access_mode'])
        if set(body) == {'native_setup'}:
            return zcode_worker.configure(body['native_setup'])
        if body == {'connect_native': True}:
            return zcode_worker.launch()
        if body == {'refresh_native_models': True}:
            return zcode_worker.refresh_catalog()
        if set(body) == {'native_model'}:
            model = zcode_worker.select_model(body['native_model'])
            return update(endpoint, {'current_model': model['id']}, settings)
    if endpoint in ('pi', 'dsh') and isinstance(body, dict) and set(body) == {'access_mode'}:
        import endpoint_access
        return endpoint_access.update(endpoint, body['access_mode'])
    if endpoint == 'workbuddy' and isinstance(body, dict) and set(body) == {'access_mode'}:
        import workbuddy_native_tools
        return workbuddy_native_tools.set_access_mode(body['access_mode'])
    if endpoint in IDS and isinstance(body, dict) and set(body) == {'preset'}:
        preset = validate_preset(body['preset'])
        with registry.file_lock(Path(registry.HERE) / 'execution-endpoints.lock'):
            old = snapshot()
            if not old['readable']:
                raise ValueError(old['error'])
            old['endpoints'][endpoint]['preset'] = preset
            registry.write_json(Path(registry.HERE) / CONFIG_NAME, {'schema': 1, 'endpoints': old['endpoints']})
        return {'ok': True, 'endpoint': endpoint, 'preset': preset}
    if endpoint == 'workbuddy' and isinstance(body, dict) and set(body) == {'refresh_native_models'}:
        import workbuddy_worker
        if body['refresh_native_models'] is not True:
            raise ValueError('读取原生列表需要明确操作')
        return workbuddy_worker.refresh_catalog()
    if endpoint == 'workbuddy' and isinstance(body, dict) and set(body) == {'native_model'}:
        import workbuddy_worker
        model = workbuddy_worker.select_model(body['native_model'])
        return update(endpoint, {'current_model': model['id']}, settings)
    if endpoint == 'workbuddy' and isinstance(body, dict) and set(body) == {'native_setup'}:
        import workbuddy_worker
        return workbuddy_worker.configure(body['native_setup'])
    if endpoint not in IDS or not isinstance(body, dict) or not body or set(body) - {'enabled', 'current_model'}:
        raise ValueError('执行端设置无效')
    if 'enabled' in body and type(body['enabled']) is not bool:
        raise ValueError('执行端开关必须是布尔值')
    if 'current_model' in body:
        selected = body['current_model']
        if selected is not None:
            if not isinstance(selected, str):
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
        info = dict(agents.get(key, {}))
        if key in ('pi', 'dsh'):
            import endpoint_access
            try:
                info['access_mode'] = endpoint_access.saved(key)
            except ValueError as exc:
                info.update(access_mode=None, message=str(exc))
        rows.append({'id': key, 'name': LABELS[key], **pref, 'models': group,
                     'preset': pref.get('preset'),
                     'model': display, 'runtime': info, 'ready': bool(info.get('ready')),
                     'readable': controls['readable'], 'error': controls['error'],
                     'selection_error': ('当前模型已移除或改属其他执行端，请重新选择'
                                         if pref['current_model'] and selected is None else
                                         '当前模型已停用，请启用或重新选择' if selected and not selected.get('enabled', True) else ''),
                     'billing_source': key+'-native-account' if key in ('workbuddy', 'zcode') else 'model-provider',
                     'balance': None,
                     'message': info.get('message', '')})
    return {'rows': rows, 'unassigned_models': [m for m in models if m['id'] not in assigned],
            'readable': controls['readable'], 'error': controls['error']}
