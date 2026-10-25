"""Explicit user-ordered models for NEW automatic tasks; never resends a task."""
import json
from pathlib import Path
import model_registry as registry

CONFIG = 'routing-priority.json'


def snapshot():
    path = Path(registry.HERE) / CONFIG
    if not path.exists():
        return {'enabled': False, 'slots': [], 'readable': True, 'error': ''}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or set(data) != {'schema', 'enabled', 'slots'} or
                data['schema'] != 1 or type(data['enabled']) is not bool or
                not isinstance(data['slots'], list) or len(data['slots']) > 20):
            raise ValueError()
        seen = set()
        for row in data['slots']:
            if (not isinstance(row, dict) or set(row) != {'label', 'model_id', 'unavailable_reason'} or
                    not isinstance(row['label'], str) or not row['label'].strip() or len(row['label']) > 160 or
                    row['model_id'] is not None and (not isinstance(row['model_id'], str) or not row['model_id']) or
                    not isinstance(row['unavailable_reason'], str) or len(row['unavailable_reason']) > 600):
                raise ValueError()
            if row['model_id']:
                if row['model_id'] in seen:
                    raise ValueError()
                seen.add(row['model_id'])
            elif not row['unavailable_reason'].strip():
                raise ValueError()
        return {**data, 'readable': True, 'error': ''}
    except (OSError, ValueError, KeyError, TypeError):
        return {'enabled': True, 'slots': [], 'readable': False,
                'error': '模型使用顺序配置无法核实；已阻止自动路由，不恢复费用排序'}


def validate(body,model_ids):
    if not isinstance(body, dict) or set(body) not in ({'enabled', 'slots'},{'enabled','slots','expected'}):
        raise ValueError('模型使用顺序需单独保存 enabled 与 slots')
    expected=body.get('expected')
    if 'expected' in body and (not isinstance(expected,dict) or set(expected)!={'enabled','slots'} or
            type(expected.get('enabled')) is not bool or not isinstance(expected.get('slots'),list) or len(expected['slots'])>20):
        raise ValueError('模型顺序的原始快照无效，请重新加载')
    data = {'schema': 1, 'enabled':body['enabled'],'slots':body['slots']}
    # Validate before replacing the live configuration. No credential is
    # needed to verify that a saved model id exists.
    if type(data['enabled']) is not bool or not isinstance(data['slots'], list) or len(data['slots']) > 20:
        raise ValueError('模型顺序参数无效')
    ids = set()
    for row in data['slots']:
        if (not isinstance(row, dict) or set(row) != {'label', 'model_id', 'unavailable_reason'} or
                not isinstance(row['label'], str) or not 1 <= len(row['label'].strip()) <= 160 or
                not isinstance(row['unavailable_reason'], str) or len(row['unavailable_reason']) > 600 or
                row['model_id'] is not None and (not isinstance(row['model_id'], str) or not row['model_id'])):
            raise ValueError('每个顺位需有效 label、model_id 或明确缺失原因')
        if row['model_id']:
            if row['model_id'] in ids:
                raise ValueError('同一模型不能重复占位')
            if row['model_id'] not in model_ids and not (expected and row in expected['slots']):
                raise ValueError('模型顺序引用了未保存的模型')
            ids.add(row['model_id'])
        elif not row['unavailable_reason'].strip():
            raise ValueError('缺失模型必须说明原因')
    return data


def check_expected(expected):
    if expected is not None:
        current=snapshot()
        if not current['readable'] or {'enabled':current['enabled'],'slots':current['slots']}!=expected:
            raise ValueError('模型使用顺序已经变化，请重新加载后再保存')


def save(body):
    if isinstance(body,dict) and set(body)=={'enabled','slots','expected','selections'}:
        import model_picker
        return model_picker.save_priority(body)
    data=validate(body,{model['id'] for model in registry.list_models()['models']})
    with registry.file_lock(Path(registry.HERE) / 'routing-priority.lock'):
        check_expected(body.get('expected'))
        registry.write_json(Path(registry.HERE) / CONFIG, data)
    return {'ok': True, **snapshot()}


def rank(model_id, policy=None):
    policy = snapshot() if policy is None else policy
    if not policy['readable']:
        raise ValueError(policy['error'])
    if not policy['enabled']:
        return None
    return next((i for i, row in enumerate(policy['slots']) if row['model_id'] == model_id), None)
