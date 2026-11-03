"""Saved Pi/DSH access policy; task snapshots, never live model requests."""
from pathlib import Path
import os

import model_registry as registry

MODES = ('project', 'full')
CONFIG = 'endpoint-access.json'
POLICY_FIELD = 'task_policy'
WRITE_SCOPES = ('workspace', 'declared_files', 'read_only')
PRIVATE_HOME_DIRS = ('.ssh','.aws','.azure','.gnupg','.kube','.dsh','.workbuddy','.codebuddy','.zcode')
CODEX_PRIVATE_PATHS = ('auth.json','config.toml','sessions','archived_sessions','log','history.jsonl',
                       'state_5.sqlite','state_5.sqlite-wal','state_5.sqlite-shm')
PRIVATE_RUNTIME_PATHS = ('models.json','settings.json','workflow.json','execution-endpoints.json',
                         'endpoint-access.json','panel-runtime.json','workbuddy.json','workbuddy-access.json',
                         'workbuddy-catalog.json','zcode.json','zcode-catalog.json','zcode-usage.json',
                         'project-index.json','budget-ledger.json','tasks','logs')


def _new_policy(scope):
    """Declare locations, never open credentials or infer secrets from content."""
    home=Path.home().resolve()
    roots=[home/name for name in PRIVATE_HOME_DIRS]
    # Codex stores managed source worktrees beside private state. Protect its
    # known private locations, not the parent that contains those worktrees.
    codex_home=Path(os.environ.get('CODEX_HOME') or home/'.codex')
    for base in dict.fromkeys([home/'.codex',codex_home]):
        if base.is_absolute():roots.extend(base/name for name in CODEX_PRIVATE_PATHS)
    roots.extend(Path(registry.HERE).resolve()/name for name in PRIVATE_RUNTIME_PATHS)
    return {'schema':3,'write_scope':scope,'home':str(home),
            'deny_paths':list(dict.fromkeys(str(p) for p in roots))}


def task_policy(state):
    """An optional frozen contract. Absence means legacy, never a new default."""
    if POLICY_FIELD not in state:
        return None
    value = state[POLICY_FIELD]
    if (state.get('kind') not in ('edit','chat') or
            not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] not in (1,2,3) or
            set(value) != ({'schema','write_scope'} if value['schema']==1 else {'schema','write_scope','home','deny_paths'}) or
            value.get('write_scope') not in WRITE_SCOPES or
            (state.get('kind') == 'chat') != (value['write_scope'] == 'read_only')):
        raise ValueError('任务写入范围快照无效；不会回退或放宽权限')
    if value['schema']>=2 and (not isinstance(value['home'],str) or not Path(value['home']).is_absolute() or
            not isinstance(value['deny_paths'],list) or len(value['deny_paths'])>128 or
            any(not isinstance(p,str) or not Path(p).is_absolute() for p in value['deny_paths'])):
        raise ValueError('任务受保护路径声明无效')
    return dict(value)


def policy_for_dispatch(kind, requested=None, *, parent=None, stage_limited=False):
    """New task-level authority, independent of the expected deliverable list."""
    if requested is not None and requested not in ('workspace', 'declared_files'):
        raise ValueError('write_scope 仅支持 workspace / declared_files')
    if kind not in ('edit', 'chat') or (kind == 'chat' and requested is not None):
        raise ValueError('只读任务不能指定写入范围')
    if parent is not None:
        old = task_policy(parent)
        if stage_limited and old is not None and old['write_scope']=='workspace':
            raise ValueError('不能把原自由执行任务续接到明确限定文件的阶段')
        if requested is not None and (old is None or requested != old['write_scope']):
            raise ValueError('续接或整改保持原任务写入范围；变更范围请新建任务')
        return old
    if stage_limited and requested == 'workspace':
        raise ValueError('阶段已明确限定文件范围；不能自动改成项目自由写入')
    return _new_policy('read_only' if kind == 'chat' else
                       requested or ('declared_files' if stage_limited else 'workspace'))


def scope_prompt(state, files, readonly_files=()):
    """One common task contract, not another model-callable tool."""
    import json
    contract = task_policy(state)
    if contract is None:
        return None
    scope = contract['write_scope']
    if scope == 'read_only':
        return '\n这是只读任务，不得修改文件或运行终端。'
    text = ('\n预期交付文件（用于目标说明，不是逐文件写入白名单）：' if scope == 'workspace' else
            '\n用户明确限定的可修改文件：') + json.dumps(list(files), ensure_ascii=False)
    text += ('\n在已保存的访问权限与任务工作目录边界内，可自行创建或修改必要源码、测试、脚本、交付说明及辅助文件；不因文件未列在预期清单内而禁止操作，如实报告所有业务改动。'
             if scope == 'workspace' else '\n只修改明确列出的文件；需要扩展范围时报告主脑。')
    if readonly_files:
        text += '\n明确只读文件（即使同时出现在交付清单，也不得修改）：' + json.dumps(list(readonly_files), ensure_ascii=False)
    if (state.get('workspace') or {}).get('created'):
        text += ('\n隔离任务职责：执行端负责实现、运行原生自测并报告实际成果；'
                 'Git暂存、提交、成果冻结、分支集成及独立验收由工作台控制器和主脑办理。'
                 '验收条件中的冻结提交或资源收尾是控制器责任，不是要求执行端自行提交。'
                 '完成文件与测试后直接报告，不为收尾执行git add、commit、切换分支或发布。')
    if contract['schema']==3:
        text += ('\n终端使用原生工具自主执行；工作台仅核对结构化路径与有限直接命令的明确目标，'
                 '不推断任意脚本 IO，不能替代系统隔离。不得读取或修改工作台记录、认证配置、'
                 '其他任务/会话或隔离副本之外的原项目；保持已声明的文件范围与只读约束。')
    return text + '\n普通工具错误可在本任务内纠正；真实权限/保护拒绝后停止并报告，不换工具绕过。'


def policy_view(state):
    """Read-only views must report corrupt metadata, not claim authority."""
    try:
        value=task_policy(state)
    except ValueError:
        return {'state':'invalid','write_scope':None}
    return {'state':'legacy','write_scope':None} if value is None else {
        'state':'valid','schema':value['schema'],'write_scope':value['write_scope']}


def scratch_prompt(state, temp_dir):
    """Durable regression tests are deliverables; scratch is for throwaway work."""
    if not temp_dir:
        return ''
    prefix='\n任务临时目录：'+temp_dir+'（位于执行副本内）；'
    contract=task_policy(state)
    if contract and contract['write_scope']=='workspace':
        return (prefix+'仅一次性诊断脚本、运行产物与临时材料放这里。'
                '长期维护的回归测试属于业务成果，应放在正常源码或测试目录，随实际业务改动交付；'
                '用户指定的交付路径优先，不要把需交付的源码或测试放进临时目录。'
                '临时材料不进入业务提交，可以保留，不必为收尾执行清理。')
    if contract and contract['schema']==3 and contract['write_scope']=='declared_files':
        return (prefix+'本任务明确限定了可写文件，临时目录名称不自动扩大写入权限。'
                '不要为交付额外写报告或预览；交付说明直接在最终回复中返回。'
                '如必须新增文件，先报告主脑确认范围。')
    return (prefix+'测试脚本、运行产物与临时材料放这里，不需要逐个声明；'
            '业务文件仍遵守已保存的文件范围；临时材料不进入业务提交，可以保留。')


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
