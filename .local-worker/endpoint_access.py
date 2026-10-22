"""Saved Pi/DSH access policy; task snapshots, never live model requests."""
from pathlib import Path

import model_registry as registry

MODES = ('project', 'full')
CONFIG = 'endpoint-access.json'


def read():
    import json
    path = Path(registry.HERE) / CONFIG
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or set(data) != {'schema', 'endpoints'} or
                data['schema'] != 1 or not isinstance(data['endpoints'], dict) or
                set(data['endpoints']) - {'pi', 'dsh'} or
                any(v not in MODES for v in data['endpoints'].values())):
            raise ValueError()
        return data['endpoints']
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('执行端访问权限配置无效；已阻止新派发') from None


def saved(engine):
    if engine not in ('pi', 'dsh'):
        raise ValueError('访问权限执行端无效')
    return read().get(engine, 'full')  # preserve existing Pi/DSH installations


def update(engine, value):
    if engine not in ('pi', 'dsh') or value not in MODES:
        raise ValueError('执行端访问权限仅支持 project / full')
    with registry.file_lock(Path(registry.HERE) / 'endpoint-access.lock'):
        values = read()
        values[engine] = value
        registry.write_json(Path(registry.HERE) / CONFIG, {'schema': 1, 'endpoints': values})
    return {'ok': True, 'endpoint': engine, 'access_mode': value}


def task_mode(state):
    value = state.get('execution_access_mode', 'full')
    if value not in MODES:
        raise ValueError('任务访问权限快照无效')
    return value


def for_dispatch(engine, parent=None):
    return task_mode(parent) if parent is not None else saved(engine)


def sandbox(kind, mode='full'):
    if mode not in MODES:
        raise ValueError('任务访问权限无效')
    return 'read-only' if kind != 'edit' else 'workspace-write' if mode == 'project' else 'danger-full-access'
