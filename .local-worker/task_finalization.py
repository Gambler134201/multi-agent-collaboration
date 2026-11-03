"""Controller settlement is separate from executor completion and review."""
import model_registry as registry

FIELD = 'controller_finalization'
TERMINAL = {'completed', 'failed', 'cancelled'}


def view(task):
    value = task.get(FIELD)
    if FIELD not in task:
        return {'state': 'legacy', 'note': '历史记录未保存控制器收尾状态'}
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1 or value.get('state') not in {'pending', 'settled', 'attention'}:
        return {'state': 'attention', 'note': '控制器收尾记录无法核实'}
    return {'state': value['state'], 'note': str(value.get('note') or '')[:500], 'at': value.get('at')}


def pending():
    return {'schema': 1, 'state': 'pending', 'at': registry.now(), 'note': '等待控制器提交成果及释放资源'}


def require_settled(task):
    state = view(task)
    if state['state'] not in {'legacy', 'settled'}:
        raise ValueError('控制器收尾尚未确认，不能验收、整改或创建交接：' + state['note'])


def settle(run, *, note='', allow_recovery=False):
    task = registry.read_task(run)
    if task.get('status') not in TERMINAL and not (allow_recovery and task.get('status')=='recovery_pending'):
        return None
    problem = note or task.get('workspace_commit_error')
    value = {'schema': 1, 'state': 'attention' if problem else 'settled',
             'at': registry.now(), 'note': str(problem or '成果处理与资源释放已结束')[:500]}
    registry.patch_task(run, **{FIELD: value})
    return value


def interrupt(run):
    """A lost terminal finalizer never authorizes replaying the executor."""
    value = {'schema': 1, 'state': 'attention', 'at': registry.now(),
             'note': '后台重启中断了控制器收尾；执行状态以任务记录为准，请核对成果，不自动重发模型请求'}
    registry.patch_task(run, **{FIELD: value})
    return value
