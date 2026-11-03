"""Bind native ZCode tools and hooks; implements no model-callable tools."""
import hashlib
import json
from pathlib import Path
import uuid

import model_registry as registry
import record_protection
import runtime_paths
import task_workspace
import endpoint_access

MODE='zcode-native-v1'
GUARD=Path(__file__).resolve().parent/'agent-runtime/zcode-native-guard.cjs'
SHARED=GUARD.with_name('workbuddy-native-guard.cjs')
POLICY=GUARD.with_name('record-policy.cjs')
READS=('Read','Glob','Grep','WebFetch','WebSearch')
TOOLS=READS+('Write','Edit','Bash')
EVENTS=('SessionStart','PreToolUse','PostToolUse','PostToolUseFailure')


def guard_revision():
    """Concatenated SHA256 of this adapter, the shared guard and the record policy."""
    return hashlib.sha256(GUARD.read_bytes()+SHARED.read_bytes()+POLICY.read_bytes()+GUARD.with_name('endpoint-access.cjs').read_bytes()).hexdigest()


def revision():
    """Bind guard files and the task-visible tool/hook plan; any drift rejects queued tasks."""
    plan=json.dumps([TOOLS,READS,EVENTS,task_workspace.TASK_TEMP_DIR],ensure_ascii=False).encode()
    return hashlib.sha256(GUARD.read_bytes()+SHARED.read_bytes()+POLICY.read_bytes()+GUARD.with_name('endpoint-access.cjs').read_bytes()+plan).hexdigest()


def _sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def install_profile(project, record_project=None):
    """Only add native hook declarations, preserving existing enabled hooks."""
    project=Path(project).resolve();directory=project/'.zcode';config=directory/'config.json'
    record=Path(record_project).resolve() if record_project is not None else project
    if directory.is_symlink() or (directory.exists() and directory.resolve()!=directory):
        raise ValueError('ZCode 项目配置目录存在别名，未写入')
    if config.is_symlink() or (config.exists() and (not config.is_file() or config.stat().st_nlink!=1)):
        raise ValueError('ZCode 项目配置不是普通文件，未写入')
    node=runtime_paths.node_executable()
    if node is None or not Path(node).is_file():raise ValueError('原生 hook 需要已配置的 Node 22+，未自动安装或改用自建工具')
    declaration={'type':'process','command':str(Path(node).resolve()),'args':[str(GUARD),str(record)],'timeoutMs':10000}
    expected={'hooks':[declaration]}
    with registry.file_lock(record/'.model-desk/zcode-native-config.lock'):
        if config.exists() and config.stat().st_size>2*1024*1024:raise ValueError('ZCode 项目配置过大，未写入')
        before=_sha(config) if config.exists() else None
        try:data=json.loads(config.read_text(encoding='utf-8-sig')) if config.exists() else {}
        except (OSError,ValueError):raise ValueError('ZCode 项目配置无法解析，未覆盖') from None
        if not isinstance(data,dict):raise ValueError('ZCode 项目配置结构无效，未覆盖')
        hooks=data.get('hooks',{})
        if not isinstance(hooks,dict) or not isinstance(hooks.get('events',{}),dict):raise ValueError('ZCode 原生 hook 配置结构无效')
        events=hooks.get('events',{})
        if any(not isinstance(rows,list) for rows in events.values()):raise ValueError('ZCode 原生 hook 声明结构无效')
        if hooks.get('enabled') is not True and any(events.values()):
            raise ValueError('项目已有未启用的 hook；未自动启用，请先在 ZCode 确认原设置')
        updated={name:list(rows) for name,rows in events.items()}
        for event in EVENTS:
            entries=updated.setdefault(event,[])
            own=[row for row in entries if isinstance(row,dict) and isinstance(row.get('hooks'),list) and any(
                isinstance(h,dict) and h.get('type')=='process' and h.get('args')==declaration['args']
                for h in row['hooks'])]
            if own and (len(own)!=1 or own[0]!=expected):raise ValueError('已有工作台原生 hook 声明已变化，未覆盖')
            if not own:entries.append(expected)
        merged={**data,'hooks':{**hooks,'enabled':True,'events':updated}}
        if merged!=data:
            if (_sha(config) if config.exists() else None)!=before:raise ValueError('原生配置并发变化，未覆盖')
            registry.write_json(config,merged)
        return {'path':str(config),'sha256':_sha(config),'command':declaration['command'],
                'args':declaration['args'],'events':list(EVENTS),'timeout_ms':10000}


def task_temp_dir(state):
    """Declared scratch directory for an isolated edit task, else ``None``.

    Test scripts and temporary material belong inside the task copy, where the
    native guards allow declared writes; a task that runs directly in the
    user's own tree gets no declared temp directory, so behavior there is
    unchanged.  Read-only (chat) tasks never receive one: they have no write
    authority to begin with.
    """
    record, project = task_workspace.execution_roots(state)
    if record == project or state.get('kind') != 'edit':
        return None
    return task_workspace.TASK_TEMP_DIR


def _validate_temp_path(project):
    """Local name for the shared host-neutral scratch-path check; semantics unchanged."""
    return task_workspace.task_temp_path(project)


def selftest_diagnostic_enabled(state):
    """Opt-in gate: hash-only command diagnostics for declared selftest runs."""
    command=state.get('test_command')
    return isinstance(command,str) and bool(command.strip())


def selftest_diagnostic_summary(events,expected):
    """Hash-only taxonomy for a missed exact selftest match.

    The event stream records command hashes precisely so the acceptance check
    never loosens; this summary classifies a miss (format difference, compound
    group, unparsable compound, or a genuinely different command) using only
    the diagnostic hashes and lengths.  Raw command text is never present.
    """
    rows=[e for e in events if e.get('tool')=='Bash' and isinstance(e.get('command_diag'),dict)]
    if not rows:return {'bash_diag_events':0}
    diags=[e['command_diag'] for e in rows]
    return {'bash_diag_events':len(rows),
            'trimmed_match':any(expected==d.get('trimmed') for d in diags),
            'line_endings_match':any(expected==d.get('line_endings_normalized') for d in diags),
            'group_match':any(expected in (d.get('groups') or []) for d in diags),
            'unparsed_groups':any(d.get('groups') is None for d in diags),
            'typed_len':diags[-1].get('len')}


def native_result_command_hashes(result):
    """``{hash,len}`` pairs for command strings in the native session snapshot.

    The readSession projection shape is version-bound, so this walks mappings
    depth-first and keeps every ``command`` string's sha256 and length - never
    the text - capped and deduplicated.  Empty when the shape yields nothing;
    a hit whose hash equals the expected command while the hook saw a
    different one would point at parameter mapping rather than model rewrite.
    """
    found=[]
    def walk(value,depth=0):
        if len(found)>=50 or depth>12:return
        if isinstance(value,dict):
            for key,item in value.items():
                if key=='command' and isinstance(item,str) and item.strip():
                    found.append({'hash':hashlib.sha256(item.encode()).hexdigest(),'len':len(item)})
                else:walk(item,depth+1)
        elif isinstance(value,list):
            for item in value:walk(item,depth+1)
    walk(result)
    unique=[]
    for row in found:
        if not any(row['hash']==u['hash'] for u in unique):unique.append(row)
    return unique[:50]


def prepare(run,state,protected_roots,session_id):
    run=Path(run).resolve();record,project=task_workspace.native_roots(run,state)
    if not session_id.startswith('modeldesk-zcode-'):
        raise ValueError('ZCode 原生工具必须绑定当前任务和专用会话')
    if state['kind'] not in ('edit','chat') or state.get('zcode_access_mode') not in ('project','full'):
        raise ValueError('ZCode 原生工具任务权限无效')
    files,readonly=task_workspace.execution_files(state),task_workspace.execution_files(state,'readonly_files')
    record_protection.validate_files(files+readonly,project)
    if any(not (project/name).resolve().is_relative_to(project) for name in files+readonly):
        raise ValueError('ZCode 声明文件越出项目')
    profile=install_profile(project,record_project=record)
    job={'schema':1,'mode':MODE,'task_id':state['id'],'project':str(project),'kind':state['kind'],
         'files':files,'readonly_files':readonly,'access_mode':state['zcode_access_mode'],
         'protected_roots':[str(Path(p).resolve()) for p in protected_roots],
         'native_session_id':session_id,'nonce':uuid.uuid4().hex,'revision':revision(),
         'guard_revision':guard_revision(),
         'tools':list(TOOLS if state['kind']=='edit' else READS),'profile':profile}
    contract=endpoint_access.task_policy(state)
    if contract is not None:job[endpoint_access.POLICY_FIELD]=contract
    temp_dir=task_temp_dir(state)
    if temp_dir:
        temp_path=_validate_temp_path(project)
        temp_path.mkdir(exist_ok=True)
        _validate_temp_path(project)
        job['temp_dir']=temp_dir
    if selftest_diagnostic_enabled(state):
        job['selftest_diagnostic']=True
    if record!=project:
        job['record_project']=str(record)
        job['protected_roots'].append(str(record))
    path=run/'zcode-native-job.json'
    with path.open('x',encoding='utf-8') as stream:json.dump(job,stream,ensure_ascii=False)
    index=record/'.model-desk/zcode-native-sessions.json'
    with registry.file_lock(index.with_suffix('.lock')):
        data=registry.read_json(index) if index.exists() else {'schema':1,'sessions':{}}
        if data.get('schema')!=1 or not isinstance(data.get('sessions'),dict):raise ValueError('原生会话索引无效')
        if session_id in data['sessions']:raise ValueError('原生会话不能重复绑定')
        data['sessions'][session_id]={'task_id':state['id'],'job_hash':_sha(path)}
        registry.write_json(index,data)
    return path


def load(file):
    file=Path(file).resolve();data=json.loads(file.read_text(encoding='utf-8'))
    project=Path(data['project']).resolve()
    record,project=task_workspace.validate_native_job(file.parent,data)
    state=registry.read_task(file.parent) if (file.parent/'task.json').exists() else {}
    if endpoint_access.task_policy(data)!=endpoint_access.task_policy(state):
        raise ValueError('原生工具写入范围与任务快照不符')
    if (file.parent!=record/'.model-desk/runs'/data['task_id'] or data.get('schema')!=1 or
            data.get('mode')!=MODE or data.get('revision')!=revision() or data.get('guard_revision')!=guard_revision() or
            data.get('kind') not in ('edit','chat') or
            data.get('access_mode') not in ('project','full') or data.get('tools')!=list(TOOLS if data['kind']=='edit' else READS) or
            ('temp_dir' in data and data['temp_dir']!=task_workspace.TASK_TEMP_DIR) or
            type(data.get('selftest_diagnostic',False)) is not bool):
        raise ValueError('ZCode 原生任务身份或工具版本变化，未自动升级')
    if 'temp_dir' in data:
        if not _validate_temp_path(project).is_dir():
            raise ValueError('任务临时目录缺失，未自动重建')
    if Path(data['profile']['path']).resolve()!=project/'.zcode/config.json' or _sha(data['profile']['path'])!=data['profile']['sha256']:
        raise ValueError('ZCode 原生 hook 配置变化，未自动重试')
    index=registry.read_json(record/'.model-desk/zcode-native-sessions.json')
    if (index.get('schema')!=1 or index.get('sessions',{}).get(data.get('native_session_id'))!=
            {'task_id':data['task_id'],'job_hash':_sha(file)}):raise ValueError('ZCode 原生会话索引不匹配')
    return data


def verify(file,session_id):
    data=load(file);run=Path(file).parent
    if data.get('native_session_id')!=session_id:raise ValueError('ZCode 原生会话不属于本任务')
    proof=registry.read_json(run/'zcode-native-proof.json')
    expected={key:data[key] for key in ('task_id','project','nonce','revision','mode','access_mode')}
    if 'record_project' in data:expected['record_project']=data['record_project']
    expected.update(source='zcode-native-hooks',job_hash=_sha(file),session_id=session_id,probe='ok')
    if any(proof.get(k)!=v for k,v in expected.items()):raise ValueError('ZCode 原生 hook 未绑定本任务会话')
    path=run/'zcode-native-events.jsonl'
    if path.exists() and path.stat().st_size>8*1024*1024:raise ValueError('原生工具事件超过核对上限')
    events=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    if len(events)>10000 or any(e.get('session_id')!=session_id or e.get('nonce')!=data['nonce'] for e in events):
        raise ValueError('ZCode 原生工具事件不属于当前任务')
    return {**proof,'events':events,'writes_paused':(run/'zcode-native-denied.flag').exists()}


def public_calls(proof):
    calls={}
    for number,e in enumerate(proof.get('events',[])):
        if not e.get('tool'):continue
        key=(e.get('call_id') or str(number),e['tool'])
        status=('failed' if not e.get('allowed') or e.get('event')=='PostToolUseFailure' or
                (e.get('event')=='PostToolUse' and not e.get('ok')) else
                'completed' if e.get('event')=='PostToolUse' else 'running')
        calls[key]={'title':e['tool'],'status':status,'input_hash':e.get('input_hash'),
                    'exit_code':e.get('exit_code'),'denial_rule':e.get('denial_rule')}
    return list(calls.values())
