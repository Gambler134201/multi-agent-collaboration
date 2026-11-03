"""Task worktree isolation and controlled integration pre-check.

Task worktrees are disposable execution copies of a Git project.  Records,
acceptance, handovers and notifications stay owned by the trusted original
project; only the executing copy lives under the workspace root next to the
project, never inside it and never inside `.model-desk`.  The user's working
tree is never touched: the baseline is read from HEAD, conflict pre-checks
run through `git merge-tree --write-tree` (no working tree or index change)
and release refuses dirty worktrees instead of deleting uncommitted results.
No repository is ever initialized here: projects without Git, or without a
resolvable baseline, stay serial by design.
"""
import re
import subprocess
from pathlib import Path

import model_registry as registry

TASK_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$')
BRANCH_PREFIX = 'modeldesk-task/'
INTEGRATION_PREFIX = 'integration-'
# Declared scratch directory inside an isolated task copy.  Test scripts and
# temporary material belong here: the native guards allow declared writes and
# literal cleanup inside it, deliverable commits exclude it, and it never
# counts as a deliverable file.  The name deliberately does not contain the
# substring ".model-desk" so the record-policy matchers cannot hit it, and it
# is not one of the protected component names (.zcode/.codex/...).
TASK_TEMP_DIR = '.modeldesk-task-tmp'
COMMIT_EXCLUDED = ('.zcode/', '.model-desk/', TASK_TEMP_DIR + '/')


def _git(repo, *args, timeout=60):
    return subprocess.run(['git', *args], cwd=str(repo), capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _err(result, limit=300):
    return (result.stderr or result.stdout or '').strip().replace('\n', '；')[:limit]


def _name_or_fail(value, label):
    if not isinstance(value, str) or not NAME.match(value):
        raise ValueError(label + '只能包含字母数字点横线下划线，且以字母或数字开头')
    return value


def workspace_root(project):
    """Execution copies live as a sibling of the project, outside its tree."""
    project = Path(project).resolve()
    if project.parent == project:
        raise ValueError('项目不能位于盘符根目录；无法放置任务工作区')
    return project.parent / (project.name + '-task-workspaces')


def inspect(project):
    """Read-only eligibility: repo rooted at this project, HEAD baseline, dirty flag."""
    project = Path(project).resolve()
    if not project.is_dir():
        return {'eligible': False, 'reason': '项目目录不存在', 'baseline': None, 'dirty': None}
    toplevel = _git(project, 'rev-parse', '--show-toplevel')
    if toplevel.returncode != 0:
        return {'eligible': False, 'reason': '项目不是 Git 仓库；按既定边界保持串行，不自动初始化',
                'baseline': None, 'dirty': None}
    if Path(toplevel.stdout.strip()).resolve() != project:
        return {'eligible': False, 'reason': '项目不是仓库根目录；初版只在仓库根上建立任务工作区',
                'baseline': None, 'dirty': None}
    head = _git(project, 'rev-parse', 'HEAD')
    if head.returncode != 0:
        return {'eligible': False, 'reason': '仓库没有可解析的基线提交；保持串行', 'baseline': None, 'dirty': None}
    status = _git(project, 'status', '--porcelain')
    return {'eligible': True, 'reason': '', 'baseline': head.stdout.strip(),
            'dirty': status.returncode == 0 and bool(status.stdout.strip())}


def _status_tree(path):
    """Dirty summary of a worktree: list of 'XY path' entries, capped for reports."""
    status = _git(path, 'status', '--porcelain')
    if status.returncode != 0:
        raise ValueError('工作区状态不可读：' + _err(status))
    rows = [line for line in status.stdout.splitlines() if line.strip()]
    return rows


def prepare(project, task_id, baseline=None):
    """Create an isolated worktree for one task on its own branch at the baseline."""
    if not isinstance(task_id, str) or not TASK_ID.match(task_id):
        raise ValueError('任务标识不合法；不能创建任务工作区')
    info = inspect(project)
    if not info['eligible']:
        raise ValueError('项目不适合工作区隔离：' + info['reason'])
    project = Path(project).resolve()
    baseline = baseline or info['baseline']
    verify = _git(project, 'rev-parse', '--verify', baseline + '^{commit}')
    if verify.returncode != 0:
        raise ValueError('基线提交不可解析：' + _err(verify))
    baseline = verify.stdout.strip()
    root = workspace_root(project)
    if root.exists() and not root.is_dir():
        raise ValueError('工作区根位置被同名文件占用：' + str(root))
    # Concurrent prepares fight over git's own lock files and can hang the
    # failure cleanup; one per-project lock serializes creation and rollback.
    with registry.file_lock(root / 'prepare.lock'):
        path = root / task_id
        if path.exists():
            raise ValueError('该任务的工作区已存在；不重复创建')
        branch = BRANCH_PREFIX + task_id
        if _git(project, 'rev-parse', '--verify', 'refs/heads/' + branch).returncode == 0:
            raise ValueError('任务分支已存在；不复用旧分支，避免混入历史成果')
        added = _git(project, 'worktree', 'add', '-b', branch, str(path), baseline)
        if added.returncode != 0:
            _git(project, 'worktree', 'prune')
            gone = _git(project, 'branch', '-D', branch)
            raise ValueError('任务工作区创建失败：' + _err(added) + ('；已回滚分支' if gone.returncode == 0 else ''))
    return {'path': str(path), 'branch': branch, 'baseline': baseline,
            'root': str(root), 'project': str(project)}


def status(project, task_id):
    """Current worktree state for one task; stale metadata is reported, never auto-pruned."""
    if not isinstance(task_id, str) or not TASK_ID.match(task_id):
        raise ValueError('任务标识不合法')
    path = workspace_root(project) / task_id
    if not path.exists():
        listed = _git(Path(project), 'worktree', 'list', '--porcelain')
        prunable = listed.returncode == 0 and any(
            line.startswith('worktree ') and Path(line[9:]).resolve() == path.resolve()
            for line in listed.stdout.splitlines())
        return {'exists': False, 'path': str(path), 'dirty': None, 'head': None,
                'branch': BRANCH_PREFIX + task_id, 'stale': prunable}
    rows = _status_tree(path)
    head = _git(path, 'rev-parse', 'HEAD')
    branch = _git(path, 'rev-parse', '--abbrev-ref', 'HEAD')
    return {'exists': True, 'path': str(path), 'dirty': bool(rows),
            'dirty_summary': rows[:10], 'head': head.stdout.strip() if head.returncode == 0 else None,
            'branch': branch.stdout.strip() if branch.returncode == 0 else BRANCH_PREFIX + task_id}


def release(project, task_id):
    """Remove a clean task worktree; dirty ones are refused so results survive review.

    The task branch is always kept: it holds the deliverable commits until
    integration acceptance decides otherwise.
    """
    state = status(project, task_id)
    if not state['exists']:
        if state.get('stale'):
            pruned = _git(Path(project), 'worktree', 'prune')
            if pruned.returncode != 0:
                raise ValueError('残留工作区元数据清理失败：' + _err(pruned))
            return {'removed': False, 'pruned': True, 'branch': state['branch']}
        raise ValueError('任务工作区不存在')
    if state['dirty']:
        raise ValueError('工作区有未提交成果，不能删除；请先核对：'
                         + '；'.join(state.get('dirty_summary') or []))
    removed = _git(Path(project), 'worktree', 'remove', state['path'])
    if removed.returncode != 0:
        raise ValueError('任务工作区移除失败：' + _err(removed))
    return {'removed': True, 'pruned': False, 'branch': state['branch']}


def files_dirty(project, files):
    """Porcelain rows limited to the declared files; user WIP there blocks isolation.

    An untracked declared file counts too: the baseline lacks it, so the task
    would create a different file that later collides with the user's own.
    """
    names = [str(name) for name in files or []]
    if not names:
        return []
    result = _git(Path(project), 'status', '--porcelain', '--', *names)
    if result.returncode != 0:
        raise ValueError('Git 状态不可读：' + _err(result))
    return [line for line in result.stdout.splitlines() if line.strip()]


def execution_roots(state):
    """Split record identity from the execution root for one task state.

    Records, sessions, context indexes and notifications stay owned by the
    trusted record project; all file work happens in the execution root,
    which is the task worktree when a workspace plan was created for this
    task.  A missing or identical workspace path is an error, never a
    silent fallback to the user's own tree.
    """
    record = Path(state['project']).resolve()
    plan = state.get('workspace') or {}
    if plan.get('created'):
        execution = Path(plan['path']).resolve()
        if not execution.is_dir():
            raise ValueError('任务工作区缺失；不能在原项目上冒充隔离执行')
        if execution == record:
            raise ValueError('任务工作区路径与原项目相同；拒绝假隔离')
        return record, execution
    return record, record


def execution_files(state, field='files'):
    """Map declared original-project paths into the execution copy.

    Record spellings stay immutable; consumers use the returned relative names
    for tools/hashes. In-project absolute paths must never escape back to the
    user's original tree once a worktree was selected.
    """
    names = list(state.get(field) or [])
    record, execution = execution_roots(state)
    if record == execution:
        return names
    mapped = []
    for name in names:
        original = (record / name).resolve()
        if not original.is_relative_to(record):
            raise ValueError('声明文件越出原项目；不能映射到任务工作区')
        relative = original.relative_to(record)
        if not (execution / relative).resolve().is_relative_to(execution):
            raise ValueError('声明文件在任务工作区中存在项目外别名')
        mapped.append(relative.as_posix())
    return mapped


def test_command_uses_original(project, command):
    """Conservative literal-path check; never rewrite a user's test command."""
    if not command:
        return False
    root = str(Path(project).resolve()).replace('\\', '/').rstrip('/').casefold()
    text = str(command).replace('\\', '/').casefold()
    return bool(re.search(re.escape(root) + r'''(?=$|[/\s"'])''', text))


def native_roots(run, state):
    """Verify both controller record identity and a real task-owned worktree."""
    record, execution = execution_roots(state)
    run = Path(run).resolve()
    if run != record / '.model-desk/runs' / state['id']:
        raise ValueError('任务记录不属于原项目')
    if record != execution:
        plan = state['workspace']
        expected = workspace_root(record) / state['id']
        if (execution != expected or expected.resolve() != expected
                or plan.get('branch') != BRANCH_PREFIX + state['id']):
            raise ValueError('任务工作区路径或分支身份不匹配')
        branch = _git(execution, 'symbolic-ref', '--short', 'HEAD')
        original_common = _git(record, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        execution_common = _git(execution, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        if (branch.returncode or branch.stdout.strip() != plan['branch']
                or original_common.returncode or execution_common.returncode
                or Path(original_common.stdout.strip()).resolve() != Path(execution_common.stdout.strip()).resolve()):
            raise ValueError('任务工作区不是原仓库的专用任务分支')
    return record, execution


def validate_native_job(run, job):
    """A split-root native job must match its authoritative controller record."""
    record = Path(job.get('record_project') or job['project']).resolve()
    execution = Path(job['project']).resolve()
    if Path(run).resolve() != record / '.model-desk/runs' / job['task_id']:
        raise ValueError('原生工具任务记录身份不匹配')
    if record != execution:
        if not any(Path(root).resolve() == record for root in job.get('protected_roots') or []):
            raise ValueError('隔离任务必须保护原项目')
        state = registry.read_task(Path(run))
        bound_record, bound_execution = native_roots(run, state)
        if state.get('id') != job['task_id'] or bound_record != record or bound_execution != execution:
            raise ValueError('原生工具执行工作区与任务记录不匹配')
    return record, execution


def commit_task_results(path, task_id):
    """Commit deliverable changes on the task branch with controller-owned identity.

    Machine-local controller state (`.zcode`, `.model-desk`) and the declared
    task temp directory (`.modeldesk-task-tmp`, scratch/test material by
    convention, never a deliverable) are excluded from the commit; any other
    uncommitted content in the worktree belongs to the deliverable.  A worktree
    holding only excluded changes commits nothing on purpose: release still
    refuses to delete it until a human decides.
    """
    path = Path(path)
    rows = _status_tree(path)
    if not [row for row in rows if not row[3:].startswith(COMMIT_EXCLUDED)]:
        return {'committed': False, 'reason': 'no_relevant_changes'}
    # "git add -A" skips ignored paths silently, but an :(exclude) pathspec makes
    # git refuse the whole add when the excluded directory is itself ignored.
    # Unstaging afterwards keeps controller-local state out of the commit even
    # in projects that do not ignore it.
    added = _git(path, 'add', '-A', '--', '.')
    if added.returncode != 0:
        raise ValueError('任务成果暂存失败：' + _err(added))
    unstaged = _git(path, 'reset', '-q', '--', '.zcode', '.model-desk', TASK_TEMP_DIR)
    if unstaged.returncode != 0:
        raise ValueError('任务成果暂存失败：无法排除控制器本机状态 ' + _err(unstaged))
    commit = _git(path, '-c', 'user.name=modeldesk-workbench', '-c', 'user.email=modeldesk@localhost',
                  'commit', '-m', 'modeldesk task deliverable: ' + str(task_id))
    if commit.returncode != 0:
        raise ValueError('任务成果提交失败：' + _err(commit))
    head = _git(path, 'rev-parse', 'HEAD')
    return {'committed': True, 'commit': head.stdout.strip()}


def untracked_scratch_row(row):
    """Only untracked entries inside the reserved directory, never index edits."""
    return row.startswith('?? ' + TASK_TEMP_DIR + '/')


def deliverable_manifest(project, baseline, commit):
    """Actual committed changes, independent of the planned deliverable list.

    Only Git objects of this task's frozen tree are inspected. Ignored files
    and authorized external paths are not covered; private content is not read.
    """
    import context_manager
    import task_handover
    if any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-fA-F]{40,64}',value)
           for value in (baseline,commit)):
        raise ValueError('任务成果基线或提交标识无效')
    result=_git(project,'diff','--no-renames','--name-only','-z',baseline,commit,'--')
    if result.returncode:
        raise ValueError('任务成果文件清单无法核实：'+_err(result))
    names=[v for v in result.stdout.split('\0') if v]
    if len(names)>10000:
        raise ValueError('任务成果文件清单超出核对上限')
    hashes={};unverified=[]
    for name in names:
        if (Path(name).is_absolute() or '..' in Path(name).parts or
                any(context_manager.is_excluded_dir(part.casefold()) for part in Path(name).parts[:-1]) or
                context_manager.is_sensitive_path(name)):
            unverified.append(name)
            continue
        row=task_handover._digest_commit_files(project,commit,[name])[0]
        hashes[name]=row['sha256']
    return {'changed_files':names,'file_hashes':hashes,
            'artifact_manifest':{'source':'controller_git_diff','baseline':baseline,'commit':commit,
                                 'unverified_files':unverified}}


def task_temp_path(project):
    """Shared scratch-path anchor for both native adapters: alias-free and a real directory.

    prepare() calls this before creating the directory and load() re-runs it,
    so a link swapped in after preparation cannot retarget the declaration.
    The name is fixed by TASK_TEMP_DIR; callers only bind it to a task by
    anchoring it under that task's real execution root.
    """
    temp_path = Path(project) / TASK_TEMP_DIR
    if temp_path.is_symlink() or temp_path.resolve() != temp_path:
        raise ValueError('任务临时目录存在别名，未放行')
    if temp_path.exists() and not temp_path.is_dir():
        raise ValueError('任务临时目录位置被同名文件占用')
    return temp_path


def _plain_scratch_tree(root):
    """Inspect bounded metadata only; links never become scratch evidence."""
    pending=[root];count=0
    while pending:
        parent=pending.pop()
        for child in parent.iterdir():
            count+=1
            if (count>10000 or child.is_symlink() or child.resolve()!=child
                    or (child.is_file() and child.stat().st_nlink>1)):
                return False
            if child.is_dir():pending.append(child)
    return True


def acceptance_dirty_rows(run, task):
    """Business dirt remains fatal; only bound, untracked scratch is retained.

    This does not change release(): leftover material still prevents automatic
    worktree removal. Native job validation is reused rather than trusting a
    directory name or the engine string, and old/other-executor tasks gain no
    exemption: ZCode must match its session index binding, WorkBuddy must
    re-verify its job plus parent-session proof, and both must sit on the real
    isolated worktree with the declared temp directory intact.
    """
    _record,execution=native_roots(run,task)
    dirty=[row for row in _status_tree(execution) if not row.startswith('?? .zcode/')]
    if not any(untracked_scratch_row(row) for row in dirty):return dirty
    engine=task.get('engine')
    if task.get('kind')!='edit' or not (task.get('workspace') or {}).get('created'):return dirty
    try:
        if engine=='zcode':
            import zcode_native_tools as native
            job=native.load(Path(run)/'zcode-native-job.json')
            if (native.task_temp_dir(task)!=TASK_TEMP_DIR or job.get('temp_dir')!=TASK_TEMP_DIR
                    or job.get('task_id')!=task.get('id')
                    or job.get('native_session_id')!=task.get('zcode_session_id')
                    or not _plain_scratch_tree(execution/TASK_TEMP_DIR)):
                return dirty
        elif engine=='workbuddy':
            import workbuddy_native_tools as native
            session_id=task.get('workbuddy_session_id')
            if not isinstance(session_id,str) or not session_id.strip():return dirty
            job_file=Path(run)/'workbuddy-native-job.json'
            job=native.load(job_file)
            if (native.task_temp_dir(task)!=TASK_TEMP_DIR or job.get('temp_dir')!=TASK_TEMP_DIR
                    or job.get('task_id')!=task.get('id')
                    or not _plain_scratch_tree(execution/TASK_TEMP_DIR)):
                return dirty
            # The proof binds job hash, nonce, task/project and the parent
            # session; model text or a bare session id can never replace it.
            native.verify(job_file,session_id)
        else:
            return dirty
    except (OSError,ValueError,KeyError,TypeError,RuntimeError):
        return dirty
    return [row for row in dirty if not untracked_scratch_row(row)]


def branch_exists(project, branch):
    """True when the modeldesk task branch exists on this repo."""
    return _git(project, 'rev-parse', '--verify',
                'refs/heads/' + BRANCH_PREFIX + branch).returncode == 0


def precheck_merge(project, branches):
    """Pairwise `git merge-tree --write-tree` between task branches.

    Reads object storage only: the user's working tree, the index and all
    worktrees stay untouched.  Both branches must exist under the project.
    """
    if not isinstance(branches, list) or not 2 <= len(branches) <= 12:
        raise ValueError('合并预检需要 2 到 12 个任务分支')
    names = [_name_or_fail(str(item), '分支名') for item in branches]
    for branch in names:
        if not branch_exists(project, branch):
            raise ValueError('任务分支不存在：' + BRANCH_PREFIX + branch)
    pairs = []
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            merged = _git(project, 'merge-tree', '--write-tree',
                          BRANCH_PREFIX + left, BRANCH_PREFIX + right)
            if merged.returncode == 0:
                pairs.append({'branches': [left, right], 'clean': True, 'conflicts': []})
            elif merged.returncode == 1:
                conflicts = sorted({line.split('\t', 1)[1] for line in merged.stdout.splitlines()
                                    if re.match(r'^\d{6} [0-9a-f]{40} [1-3]\t', line)})
                pairs.append({'branches': [left, right], 'clean': False, 'conflicts': conflicts})
            else:
                raise ValueError('合并预检执行失败：' + _err(merged))
    return {'pairs': pairs, 'clean': all(pair['clean'] for pair in pairs)}


def integration_workspace(project, name, baseline=None):
    """A detached disposable worktree at the baseline for controlled trial merges."""
    name = _name_or_fail(name, '集成区名称')
    info = inspect(project)
    if not info['eligible']:
        raise ValueError('项目不适合受控集成：' + info['reason'])
    project = Path(project).resolve()
    baseline = baseline or info['baseline']
    verify = _git(project, 'rev-parse', '--verify', baseline + '^{commit}')
    if verify.returncode != 0:
        raise ValueError('集成基线提交不可解析：' + _err(verify))
    baseline = verify.stdout.strip()
    root = workspace_root(project)
    with registry.file_lock(root / 'prepare.lock'):
        path = root / (INTEGRATION_PREFIX + name)
        if path.exists():
            raise ValueError('集成区已存在；不重复创建')
        added = _git(project, 'worktree', 'add', '--detach', str(path), baseline)
        if added.returncode != 0:
            _git(project, 'worktree', 'prune')
            raise ValueError('集成区创建失败：' + _err(added))
    return {'path': str(path), 'baseline': baseline, 'name': name}


def trial_merge(integration_path, branch, *, commit=None):
    """Merge one task branch into the integration copy; conflicts are reported, then aborted.

    The integration copy is workbench-owned and disposable, so a merge is
    committed directly on success; a conflict never leaves the area dirty.
    """
    integration_path = Path(integration_path)
    if not integration_path.exists():
        raise ValueError('集成区不存在')
    full = BRANCH_PREFIX + _name_or_fail(branch, '分支名')
    if commit is not None and not re.fullmatch(r'[0-9a-f]{40,64}', commit):
        raise ValueError('成果提交标识不合法')
    if _git(integration_path, 'status', '--porcelain').stdout.strip():
        raise ValueError('集成区存在未处理的合并状态；请先重置或移除该集成区')
    merged = _git(integration_path, '-c', 'user.name=modeldesk-workbench', '-c', 'user.email=modeldesk@localhost',
                  'merge', '--no-ff', '-m',
                  'modeldesk integration trial: ' + full, commit or full)
    if merged.returncode == 0:
        return {'branch': full, 'merged': True, 'conflicts': []}
    conflicts = _git(integration_path, 'diff', '--name-only', '--diff-filter=U')
    _git(integration_path, 'merge', '--abort')
    if merged.returncode != 1:
        raise ValueError('试合并执行失败：' + _err(merged))
    return {'branch': full, 'merged': False,
            'conflicts': sorted(line for line in conflicts.stdout.splitlines() if line.strip())}


def trial_sequence(project, branches, *, name='trial', keep=False):
    """Sequentially merge task branches into a fresh integration copy at the baseline.

    Returns one result per branch in order; the first conflict stops the
    sequence.  Text may merge while interfaces stay incompatible, so this is
    a necessary pre-check only - real integration acceptance still runs the
    project's own checks on the merged copy.
    """
    names = [_name_or_fail(str(item), '分支名') for item in branches]
    if not names:
        raise ValueError('试合并需要至少一个任务分支')
    area = integration_workspace(project, name)
    steps = []
    for branch in names:
        result = trial_merge(area['path'], branch)
        steps.append(result)
        if not result['merged']:
            break
    removed = None if keep else remove_integration(project, name)
    return {'baseline': area['baseline'], 'steps': steps,
            'clean': all(step['merged'] for step in steps),
            'integration_path': area['path'] if keep else None, 'removed': removed}


def remove_integration(project, name):
    """Remove the integration copy; same dirty protection as task worktrees."""
    name = _name_or_fail(name, '集成区名称')
    path = workspace_root(project) / (INTEGRATION_PREFIX + name)
    if not path.exists():
        raise ValueError('集成区不存在')
    rows = _status_tree(path)
    if rows:
        raise ValueError('集成区存在未提交内容，不能删除；请先核对：' + '；'.join(rows[:10]))
    removed = _git(Path(project), 'worktree', 'remove', str(path))
    if removed.returncode != 0:
        raise ValueError('集成区移除失败：' + _err(removed))
    return {'removed': True, 'path': str(path)}


def release_checked(project, task_id):
    """Explicit recycling with record-level guards; dirty/unclean refusals still apply.

    Only a terminal task whose controller settlement is confirmed and whose
    review has a final decision (accepted / final_accepted / blocked) may be
    recycled.  The task branch always survives: it pins the deliverable commit
    for later integration even after the checkout is gone.
    """
    import model_registry as registry
    import task_finalization
    import delegation_policy as policy
    if not isinstance(task_id, str) or not TASK_ID.match(task_id):
        raise ValueError('任务标识不合法')
    run = registry.task_dir(task_id, project=str(Path(project).resolve()))
    task = registry.read_task(run)
    if task.get('status') not in ('completed', 'failed'):
        raise ValueError('任务尚未结束（当前 ' + str(task.get('status')) + '）；不能回收工作区')
    state = task_finalization.view(task)['state']
    if state not in ('legacy', 'settled'):
        raise ValueError('控制器收尾尚未确认（' + state + '）；不能回收工作区')
    review = (policy.safe_load(run) or (None, ''))[0] or {}
    if review.get('state') not in ('accepted', 'final_accepted', 'blocked'):
        raise ValueError('验收结论未定（当前 ' + str(review.get('state')) + '）；'
                         '不能回收工作区，待独立验收或整改完成后再清理')
    return release(project, task_id)


def stale_worktrees(project):
    """One row per task worktree with its record state and releasability.

    Read-only inventory for the recycling entry; nothing is pruned here.
    """
    import model_registry as registry
    import task_finalization
    import delegation_policy as policy
    root = workspace_root(project)
    ids = sorted(item.name for item in root.iterdir() if item.is_dir()) if root.is_dir() else []
    rows = []
    for task_id in ids:
        row = {'task_id': task_id, 'path': str(root / task_id)}
        try:
            state = status(project, task_id)
        except (OSError, ValueError) as error:
            rows.append({**row, 'readable': False, 'error': str(error)[:200], 'releasable': False})
            continue
        row.update({key: state.get(key) for key in ('exists', 'dirty', 'head', 'branch')})
        try:
            run = registry.task_dir(task_id, project=str(Path(project).resolve()))
            task = registry.read_task(run)
            review = policy.safe_load(run) or {}
        except (OSError, ValueError):
            rows.append({**row, 'task_record': 'missing', 'releasable': False})
            continue
        row['task_status'] = task.get('status')
        row['finalization'] = task_finalization.view(task)['state']
        row['review_state'] = (policy.safe_load(run) or (None, ''))[0]
        row['review_state'] = (row['review_state'] or {}).get('state')
        row['releasable'] = bool(state.get('exists') and not state.get('dirty')
                                 and task.get('status') in ('completed', 'failed')
                                 and task_finalization.view(task)['state'] in ('legacy', 'settled')
                                 and row['review_state'] in ('accepted', 'final_accepted', 'blocked'))
        rows.append(row)
    return rows


def main(argv=None):
    """Operable recycling entry: list worktrees, or release one guarded worktree."""
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(
        description='任务工作区（worktree）清单与显式回收；只读写本地记录，不启动服务或模型')
    parser.add_argument('--project', required=True, help='项目绝对目录')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list', action='store_true', help='列出全部任务工作区与可回收状态')
    mode.add_argument('--release', metavar='TASK_ID', help='回收一个已终态且验收已定的工作区')
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps({'project': str(Path(args.project).resolve()),
                          'worktrees': stale_worktrees(args.project)},
                         ensure_ascii=False, indent=1))
        return 0
    print(json.dumps(release_checked(args.project, args.release), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
