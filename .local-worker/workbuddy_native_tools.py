"""Task-owned native tool profile and hook evidence; never reads native auth."""
import hashlib
import json
from pathlib import Path
import re
import uuid

import record_protection
import model_registry as registry

MODE = 'native-development-v1'
LEGACY = 'scoped-files-v1'
GUARD = Path(__file__).resolve().parent / 'agent-runtime/workbuddy-native-guard.cjs'
POLICY = GUARD.with_name('record-policy.cjs')
READS = ('Read', 'Glob', 'Grep', 'NotebookRead')
TOOLS = READS + ('Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'Bash', 'PowerShell',
                 'WebFetch', 'WebSearch')
ACCESS_CONFIG = 'workbuddy-access.json'
ACCESS_MODES = ('project', 'full')
DENIAL_LABELS = {
    'outside_project': '目标位于本任务项目外',
    'protected_data': '工作台记录、认证数据或其他会话受到保护',
    'undeclared_write': '目标未列入本任务可写范围',
    'readonly_file': '任务只读上下文不可修改',
    'prior_denial': '此前工具已被拒绝，本轮工具已暂停',
    'tool_not_allowed': '本任务未授权此工具',
    'guard_denied': '原生工具范围或权限保护拒绝',
    'unknown': '旧事件未记录拒绝分类'}
DENIAL_RULES = {
    'native_profile': '触及 WorkBuddy 原生配置目录',
    'shell_protected_reference': '命令文本提及工作台记录或认证数据',
    'record_ancestor': '删除或修改范围覆盖工作台记录的上级目录',
    'record_target': '工具目标位于工作台记录目录内',
    'protected_file': '文件目标属于记录、Git 元数据或环境配置',
    'credential_store': '文件目标属于认证存储',
    'runtime_config': '文件目标属于工作台运行配置',
    'other_task': '文件目标属于其他任务或会话材料',
    'hardlink': '目标存在硬链接别名',
    'readonly_file': '尝试修改本任务只读上下文',
    'outside_project': '文件目标位于项目范围外',
    'undeclared_write': '文件未列入任务可写范围',
    'git_mutation': '命令涉及 Git 历史或远端发布',
    'background_job': '命令请求在后台运行',
    'sandbox_override': '命令请求绕过原生沙箱',
    'unverified_recursive_cleanup': '递归清理使用了未核实的变量或通配符；请保留临时文件并报告主脑',
    'profile_search': '检索目录覆盖原生配置',
    'protected_search': '搜索表达式涉及受保护目录',
    'tool_not_allowed': '本任务未授权此工具',
    'prior_denial': '此前调用已被拒绝，本轮工具已暂停',
    'unclassified': '未细分的保护规则',
    'unknown': '旧事件未记录具体规则；不能还原命中路径'}


def denial_detail(event):
    rule = event.get('denial_rule')
    rule = rule if isinstance(rule, str) and rule in DENIAL_RULES else 'unknown'
    return {'rule': rule, 'detail': DENIAL_RULES[rule]}


def access_mode():
    path = Path(registry.HERE) / ACCESS_CONFIG
    if not path.exists():
        return 'project'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if set(data) != {'schema', 'mode'} or data['schema'] != 1 or data['mode'] not in ACCESS_MODES:
            raise ValueError()
        return data['mode']
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('WorkBuddy 访问权限配置无效；已阻止新派发') from None


def set_access_mode(value):
    if value not in ACCESS_MODES:
        raise ValueError('WorkBuddy 访问权限仅支持 project / full')
    registry.write_json(Path(registry.HERE) / ACCESS_CONFIG, {'schema': 1, 'mode': value})
    return {'ok': True, 'endpoint': 'workbuddy', 'access_mode': value}


def revision():
    digest = hashlib.sha256()
    for file in (GUARD, POLICY):
        digest.update(file.read_bytes())
    return digest.hexdigest()


def mode(state):
    value = state.get('workbuddy_tool_mode', LEGACY)
    if value not in (MODE, LEGACY):
        raise ValueError('WorkBuddy 工具配置版本无效；不会自动放宽权限')
    return value


def prepare(run, project, task_id, files, readonly_files, kind, *, access='project', protected_roots=()):
    run, project = Path(run).resolve(), Path(project).resolve()
    if run.parent != project / '.model-desk/runs' or run.name != task_id or kind not in ('chat', 'edit'):
        raise ValueError('WorkBuddy 原生工具必须绑定当前项目与任务')
    if access not in ACCESS_MODES:
        raise ValueError('WorkBuddy 任务访问权限无效')
    record_protection.validate_files(files + readonly_files, project)
    for name in files + readonly_files:
        # Dispatch accepts both project-relative and in-project absolute files.
        # Resolve the actual target before checking containment (including aliases),
        # keeping the original spellings so task/delivery scope stays unchanged.
        if not (project / Path(name)).resolve().is_relative_to(project):
            raise ValueError('WorkBuddy 文件范围越出当前项目')
    data = {'schema': 1, 'mode': MODE, 'task_id': task_id, 'project': str(project),
            'kind': kind, 'files': list(files), 'readonly_files': list(readonly_files),
            'access_mode': access, 'protected_roots': [str(Path(p).resolve()) for p in protected_roots],
            'nonce': uuid.uuid4().hex, 'revision': revision(),
            'tools': list(TOOLS if kind == 'edit' else READS + ('WebFetch', 'WebSearch'))}
    run.mkdir(parents=True, exist_ok=True)
    job = run / 'workbuddy-native-job.json'
    with job.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False)
    return job


def load(job):
    path = Path(job)
    data = json.loads(path.read_text(encoding='utf-8'))
    project = Path(data['project']).resolve()
    if (not path.is_absolute() or path.resolve().parent != project / '.model-desk/runs' / data['task_id'] or
            data.get('schema') != 1 or data.get('mode') != MODE or data.get('revision') != revision() or
            data.get('kind') not in ('edit', 'chat') or
            data.get('access_mode') not in ACCESS_MODES or
            not isinstance(data.get('protected_roots'), list) or
            any(not isinstance(p, str) or not Path(p).is_absolute() for p in data['protected_roots']) or
            data.get('tools') != list(TOOLS if data.get('kind') == 'edit' else READS + ('WebFetch', 'WebSearch'))):
        raise ValueError('WorkBuddy 原生工具身份或代码已变化；拒绝继续')
    return data


def settings(job, executable):
    data = load(job)
    # A command hook is executed by the native shell. Use quoted forward-slash
    # absolute paths and reject expansion syntax rather than guessing escaping.
    args = [str(Path(executable).resolve()), str(GUARD), str(Path(job).resolve())]
    if any(re.search(r'["\r\n`$%!]', arg) for arg in args):
        raise ValueError('WorkBuddy hook 路径包含 shell 展开字符')
    command = ' '.join('"' + arg.replace('\\', '/') + '"' for arg in args)
    hook = {'hooks': [{'type': 'command', 'command': command, 'timeout': 10}]}
    return {'hooks': {name: [hook] for name in ('SessionStart', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure')},
            'allowUntrustedFrontmatterHooks': False}, data['tools']


def verify(job, session_id):
    data = load(job)
    run = Path(job).parent
    proof = json.loads((run / 'workbuddy-native-proof.json').read_text(encoding='utf-8'))
    expected = {key: data[key] for key in ('task_id', 'project', 'nonce', 'revision', 'mode')}
    expected.update(access_mode=data['access_mode'], job_hash=hashlib.sha256(Path(job).read_bytes()).hexdigest())
    if (any(proof.get(key) != value for key, value in expected.items()) or
            proof.get('session_id') != session_id or proof.get('probe') != 'ok'):
        raise ValueError('WorkBuddy 原生工具保护没有绑定本任务父会话')
    events_file = run / 'workbuddy-native-events.jsonl'
    events = [json.loads(line) for line in events_file.read_text(encoding='utf-8').splitlines()] if events_file.exists() else []
    if len(events) > 10000 or any(e.get('session_id') != session_id or e.get('nonce') != data['nonce'] for e in events):
        raise ValueError('WorkBuddy 原生工具事件不属于本任务')
    return {**proof, 'events': events, 'writes_paused': (run / 'workbuddy-native-denied.flag').exists()}


def permission(job, session_id, call):
    proof = verify(job, session_id)
    name = (call.get('_meta') or {}).get('codebuddy.ai/toolName')
    args = call.get('rawInput')
    if not isinstance(args, dict) or proof['writes_paused']:
        return False
    # Node/Python preserve JSON key order here. Hash sorted JSON in both runtimes.
    blob = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    digest = hashlib.sha256(blob).hexdigest()
    return any(e.get('event') == 'PreToolUse' and e.get('call_id') == call.get('toolCallId') and
               e.get('tool') == name and e.get('input_hash') == digest and e.get('allowed') is True
               for e in proof['events'])


def public_calls(proof):
    calls = {}
    for index, event in enumerate((proof or {}).get('events', [])):
        key = str(event.get('call_id') or index)
        calls[key] = {'id': key, 'title': event.get('tool'),
                      'status': ('completed' if event.get('ok') else
                                 'unknown' if event.get('event') == 'PreToolUse' and event.get('allowed') else 'failed'),
                      'phase': event.get('event'), 'input_hash': event.get('input_hash'),
                      'exit_code': event.get('exit_code'),
                      'denial_reason': DENIAL_LABELS.get(event.get('reason_code')) if event.get('allowed') is False else None,
                      'denial_detail': denial_detail(event) if event.get('allowed') is False else None}
    return list(calls.values())


def denial_summary(proof):
    event = next((e for e in (proof or {}).get('events', [])
                  if e.get('event') == 'PreToolUse' and e.get('allowed') is False), None)
    if event is None:
        return None
    code = event.get('reason_code')
    code = code if isinstance(code, str) and code in DENIAL_LABELS else 'unknown'
    tool = event.get('tool') if event.get('tool') in TOOLS else 'Tool'
    return {'tool': tool, 'reason_code': code, 'message': DENIAL_LABELS[code], **denial_detail(event)}
