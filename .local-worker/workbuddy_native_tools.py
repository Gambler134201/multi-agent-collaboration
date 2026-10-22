"""Task-owned native tool profile and hook evidence; never reads native auth."""
import hashlib
import json
from pathlib import Path
import re
import uuid

import record_protection

MODE = 'native-development-v1'
LEGACY = 'scoped-files-v1'
GUARD = Path(__file__).resolve().parent / 'agent-runtime/workbuddy-native-guard.cjs'
POLICY = GUARD.with_name('record-policy.cjs')
READS = ('Read', 'Glob', 'Grep', 'NotebookRead')
TOOLS = READS + ('Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'Bash', 'PowerShell',
                 'WebFetch', 'WebSearch')


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


def prepare(run, project, task_id, files, readonly_files, kind):
    run, project = Path(run).resolve(), Path(project).resolve()
    if run.parent != project / '.model-desk/runs' or run.name != task_id or kind not in ('chat', 'edit'):
        raise ValueError('WorkBuddy 原生工具必须绑定当前项目与任务')
    record_protection.validate_files(files + readonly_files, project)
    for name in files + readonly_files:
        # Dispatch accepts both project-relative and in-project absolute files.
        # Resolve the actual target before checking containment (including aliases),
        # keeping the original spellings so task/delivery scope stays unchanged.
        if not (project / Path(name)).resolve().is_relative_to(project):
            raise ValueError('WorkBuddy 文件范围越出当前项目')
    data = {'schema': 1, 'mode': MODE, 'task_id': task_id, 'project': str(project),
            'kind': kind, 'files': list(files), 'readonly_files': list(readonly_files),
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
                      'exit_code': event.get('exit_code')}
    return list(calls.values())
