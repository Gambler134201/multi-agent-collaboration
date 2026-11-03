"""Atomic resource leases for limited parallel execution.

The registry is one JSON document guarded by a controller file lock; every
mutation re-reads and re-validates under the lock, so two dispatchers can
never both win the same resource.  A lease is owned by exactly one task:
release by anyone else is refused, so a cancelling, failed or recovered task
can never free another task's occupancy.  Nothing auto-expires: after a
restart the controller decides recovery from task records, and an unknown
lease stays an occupancy until its owner releases it or a human decides.
Passing `path` explicitly keeps every caller (including tests) on isolated
temporary registries; the default lives next to the controller queue.
"""
import os
from pathlib import Path

import model_registry as registry

SCHEMA = 1
KINDS = ('model', 'integration', 'budget')


class LeaseConflict(ValueError):
    """The holder observed under the lease lock, not a later racy snapshot."""
    def __init__(self, resource, lease):
        self.lease = dict(lease)
        super().__init__('资源 ' + resource + ' 已被任务 ' + str(lease.get('task_id'))
                         + ' 占用；不能并行占用')


def _file(path):
    return Path(path) if path else Path(registry.HERE) / 'task-leases.json'


def _load(file):
    if not file.exists():
        return {'schema': SCHEMA, 'leases': {}}
    try:
        data = registry.read_json(file)
    except ValueError as error:
        raise ValueError('租约注册表无效；未自动清理或重置，请人工核对') from error
    if (not isinstance(data, dict) or data.get('schema') != SCHEMA
            or not isinstance(data.get('leases'), dict)
            or not all(isinstance(v, dict) and isinstance(k, str) for k, v in data['leases'].items())):
        raise ValueError('租约注册表无效；未自动清理或重置，请人工核对')
    return data


def key(kind, name):
    """Lease identity: `<kind>:<name>`; the combined value is never parsed back."""
    if kind not in KINDS:
        raise ValueError('租约类型未知：' + str(kind))
    if (not isinstance(name, str) or not name.strip() or name != name.strip()
            or len(name) > 200 or any(c in name for c in '\r\n\t')):
        raise ValueError('租约名称不合法')
    return kind + ':' + name


def acquire(kind, name, task_id, *, project=None, model_id=None, path=None):
    """Atomically take a resource for one task; a different holder is refused."""
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError('租约需要任务标识')
    file = _file(path)
    resource = key(kind, name)
    with registry.file_lock(file.with_suffix('.lock')):
        data = _load(file)
        held = data['leases'].get(resource)
        if held:
            if held.get('task_id') == task_id:
                return {'resource': resource, 'acquired': False, 'lease': dict(held)}
            raise LeaseConflict(resource, held)
        lease = {'task_id': task_id, 'acquired_at': registry.now(), 'pid': os.getpid()}
        if project:
            lease['project'] = str(project)
        if model_id:
            lease['model_id'] = str(model_id)
        data['leases'][resource] = lease
        registry.write_json(file, data)
    return {'resource': resource, 'acquired': True, 'lease': dict(lease)}


def release(kind, name, task_id, *, path=None, expected=None):
    """Release a resource for its owner only; a foreign release is refused.

    Releasing a resource the task does not hold is an idempotent no-op for
    that task (terminal handling may run twice); releasing another task's
    lease always raises.
    Optional ``expected`` compares the complete observed claim under this same
    lock, preventing a release based on a stale snapshot (even a reused owner).
    """
    file = _file(path)
    resource = key(kind, name)
    with registry.file_lock(file.with_suffix('.lock')):
        data = _load(file)
        held = data['leases'].get(resource)
        if not held:
            return {'resource': resource, 'released': False, 'reason': 'not_held'}
        if held.get('task_id') != task_id:
            raise ValueError('资源 ' + resource + ' 属于任务 ' + str(held.get('task_id'))
                             + '；不能释放其他任务的占用')
        if expected is not None and held != expected:
            raise ValueError('资源占用与已核实的原领取快照不一致；未释放')
        del data['leases'][resource]
        registry.write_json(file, data)
    return {'resource': resource, 'released': True, 'reason': 'owner'}


def recover(kind, name, task_id, *, from_task, reason, project=None, model_id=None, path=None):
    """Take over a lease whose holder the caller has verified as terminal.

    The verification lives with the caller (task records decide staleness);
    this function only guarantees the takeover is attributable and matches the
    declared holder.  Lineage is recorded only when a lease actually existed;
    taking over an absent lease is a plain acquisition.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('租约接管必须记录原因')
    if not isinstance(from_task, str) or not from_task.strip():
        raise ValueError('租约接管需要持有者任务标识')
    file = _file(path)
    resource = key(kind, name)
    with registry.file_lock(file.with_suffix('.lock')):
        data = _load(file)
        held = data['leases'].get(resource)
        if held and held.get('task_id') != from_task:
            raise ValueError('资源 ' + resource + ' 属于任务 ' + str(held.get('task_id'))
                             + '；接管声明与持有者不符')
        lease = {'task_id': task_id, 'acquired_at': registry.now(), 'pid': os.getpid()}
        if held:
            lease['recovered_from'] = from_task
            lease['recover_reason'] = reason.strip()[:200]
        if project:
            lease['project'] = str(project)
        if model_id:
            lease['model_id'] = str(model_id)
        data['leases'][resource] = lease
        registry.write_json(file, data)
    return {'resource': resource, 'acquired': True, 'recovered': bool(held), 'lease': dict(lease)}


def holders(path=None):
    """Read-only snapshot of every current lease."""
    file = _file(path)
    with registry.file_lock(file.with_suffix('.lock')):
        data = _load(file)
    return {resource: dict(lease) for resource, lease in data['leases'].items()}


def occupancy(task_id, path=None):
    """Which resources a task currently holds; used before cancel/recovery decisions."""
    return sorted(resource for resource, lease in holders(path).items()
                  if lease.get('task_id') == task_id)
