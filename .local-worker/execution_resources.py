"""Atomic whole-task reservations for finite concurrency; no provider calls.

Shared project readers/isolated
worktrees conflict with original-tree writers; backend/account aliases conflict
even when model IDs differ. Unknown backend identities conservatively serialize.
"""
import hashlib
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import model_registry as registry


class ResourceBusy(ValueError):
    def __init__(self, holders, *, capacity=False):
        self.holders = sorted(set(holders))
        self.capacity = capacity
        super().__init__('等待执行资源；占用任务：' + '、'.join(self.holders))


def _path(value):
    path = Path(str(value or ''))
    if not value or not path.is_absolute():
        raise ValueError('执行资源需要明确的项目或安装目录')
    return os.path.normcase(str(path.resolve()))


def _key(kind, identity):
    return kind + ':' + hashlib.sha256(identity.encode('utf-8')).hexdigest()


def backend_identity(state, model):
    """Only public connection fields; never reads keys, tokens or auth files."""
    engine = state.get('engine')
    if engine == 'workbuddy':
        config = state.get('workbuddy_connection') or {}
        return 'workbuddy:' + _path(config['profile']) if config.get('profile') else 'unresolved'
    if engine == 'zcode':
        config = state.get('zcode_connection') or {}
        # One desktop installation owns the native channel, regardless of its
        # currently selected port or model. No account secrets are inspected.
        return 'zcode:' + _path(config['home']) if config.get('home') else 'unresolved'
    try:
        url = urlsplit(str(model.get('api_base') or ''))
        if url.username is not None or url.password is not None:
            raise ValueError('执行端地址不能包含认证文本')
        if url.scheme not in {'http', 'https'} or not url.hostname:
            return 'unresolved'
        host = url.hostname.rstrip('.').casefold()
        try:
            address = ipaddress.ip_address(host)
            host = 'loopback' if address.is_loopback else str(address)
        except ValueError:
            if host == 'localhost':
                host = 'loopback'
        port = url.port or (443 if url.scheme == 'https' else 80)
        return 'network:' + host + ':' + str(port)
    except ValueError:
        # Missing/invalid public identity must not create independent capacity.
        return 'unresolved'


def requests_for(state, model, settings):
    task_id = state.get('id')
    if not isinstance(task_id, str) or not task_id:
        raise ValueError('执行资源需要任务标识')
    # Historical unbound consultations keep an explicit global serial slot;
    # this sentinel is not a guessed project and is never used as a file path.
    project = _path(state['project']) if state.get('project') else 'unbound'
    plan = state.get('workspace') or {}
    isolated = plan.get('eligible') is True and state.get('engine') in {'dsh', 'pi', 'workbuddy', 'zcode'}
    if state.get('engine') == 'workbuddy':
        isolated = isolated and state.get('workbuddy_tool_mode') == 'native-development-v1'
    if state.get('engine') == 'zcode':
        isolated = isolated and state.get('zcode_tool_mode') == 'zcode-native-v1'
    identity = backend_identity(state, model)
    rows = [
        {'resource': _key('model', str(state.get('model_id') or 'unknown')), 'mode': 'exclusive'},
        {'resource': _key('backend', identity), 'mode': 'exclusive'},
        # Unknown public identity might alias any known native/backend account.
        {'resource': _key('backend_pool', 'all'),
         'mode': 'exclusive' if identity == 'unresolved' else 'shared'},
        {'resource': _key('project', project),
         'mode': 'shared' if project != 'unbound' and (isolated or state.get('kind') == 'chat') else 'exclusive'},
    ]
    group = model.get('resource_group')
    if isinstance(group, str) and group.strip():
        rows.append({'resource': _key('group', group.strip()), 'mode': 'exclusive'})
    if settings.get('enforce_paid_budget', True) and model.get('provider') != 'local':
        rows.append({'resource': _key('budget', 'paid'), 'mode': 'exclusive'})
    return _requests(rows)


def _requests(rows):
    if not isinstance(rows, list) or not rows:
        raise ValueError('执行资源清单为空或无效')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'resource', 'mode'}:
            raise ValueError('执行资源请求格式无效')
        resource, mode = row['resource'], row['mode']
        if (not isinstance(resource, str) or not re.fullmatch(r'(?:model|backend|backend_pool|group|project|budget):[0-9a-f]{64}', resource)
                or mode not in {'shared', 'exclusive'}
                or (mode == 'shared' and not resource.startswith(('project:', 'backend_pool:')))):
            raise ValueError('执行资源请求格式无效')
        if resource in result and result[resource] != mode:
            raise ValueError('执行资源清单包含互相冲突的模式')
        result[resource] = mode
    return [{'resource': key, 'mode': result[key]} for key in sorted(result)]


def _bundle(project, requests):
    kinds = [row['resource'].split(':', 1)[0] for row in requests]
    if any(kinds.count(kind) != 1 for kind in ('model', 'backend', 'project', 'backend_pool')):
        raise ValueError('执行资源缺少必需的完整占用')
    if any(kinds.count(kind) > 1 for kind in ('budget', 'group')):
        raise ValueError('执行资源包含重复占用')
    project_key = _key('project', project if project else 'unbound')
    if not any(row['resource'] == project_key and (project is not None or row['mode'] == 'exclusive') for row in requests):
        raise ValueError('执行资源项目身份不匹配')
    modes = {row['resource']: row['mode'] for row in requests}
    expected = 'exclusive' if _key('backend', 'unresolved') in modes else 'shared'
    if modes.get(_key('backend_pool', 'all')) != expected:
        raise ValueError('执行资源后端身份屏障不匹配')


def _file(path):
    return Path(path) if path is not None else Path(registry.HERE) / 'task-reservations.json'


def _load(path):
    if not path.exists():
        return {'schema': 1, 'tasks': {}}
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError()
        data = registry.read_json(path)
        if type(data.get('schema')) is not int or data['schema'] != 1 or not isinstance(data.get('tasks'), dict):
            raise ValueError()
        for task_id, row in data['tasks'].items():
            if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', task_id) or not isinstance(row, dict):
                raise ValueError()
            if ('project' not in row or row.get('requests') != _requests(row.get('requests')) or
                    (row.get('project') is not None and _path(row.get('project')) != row.get('project')) or
                    type(row.get('pid')) is not int or row['pid'] < 1 or
                    not isinstance(row.get('created_at'), str) or not row['created_at']):
                raise ValueError()
            _bundle(row['project'], row['requests'])
        return data
    except (ValueError, TypeError, AttributeError):
        raise ValueError('执行资源注册表无效；未自动清空或重置') from None


def acquire(task_id, project, requests, *, capacity=1, path=None):
    """Take all resources or none under one lock; no partial-lease deadlocks."""
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', task_id):
        raise ValueError('执行资源需要任务标识')
    if type(capacity) is not int or not 1 <= capacity <= 8:
        raise ValueError('并发上限须为 1 至 8')
    project, requests, file = _path(project) if project else None, _requests(requests), _file(path)
    _bundle(project, requests)
    with registry.file_lock(file.with_suffix('.lock')):
        data = _load(file)
        old = data['tasks'].get(task_id)
        if old:
            if old['project'] != project or old['requests'] != requests:
                raise ValueError('已有任务的资源或项目不能静默替换')
            return {'acquired': False, 'reservation': dict(old)}
        conflicts = []
        wanted = {row['resource']: row['mode'] for row in requests}
        for holder, value in data['tasks'].items():
            if any(row['resource'] in wanted and (row['mode'] == 'exclusive' or wanted[row['resource']] == 'exclusive')
                   for row in value['requests']):
                conflicts.append(holder)
        full = len(data['tasks']) >= capacity
        if conflicts or full:
            raise ResourceBusy(conflicts or list(data['tasks']), capacity=full)
        value = {'project': project, 'requests': requests, 'created_at': registry.now(), 'pid': os.getpid()}
        data['tasks'][task_id] = value
        registry.write_json(file, data)
    return {'acquired': True, 'reservation': value}


def release(task_id, *, path=None, expected=None):
    """Release only this task's entire bundle; every other owner is retained."""
    file = _file(path)
    with registry.file_lock(file.with_suffix('.lock')):
        data = _load(file)
        if task_id not in data['tasks']:
            return False
        if expected is not None and data['tasks'][task_id] != expected:
            raise ValueError('执行资源已变化；拒绝释放过时快照')
        del data['tasks'][task_id]
        registry.write_json(file, data)
    return True


def snapshot(path=None):
    file = _file(path)
    if not file.exists():
        return {}
    with registry.file_lock(file.with_suffix('.lock')):
        return _load(file)['tasks']
