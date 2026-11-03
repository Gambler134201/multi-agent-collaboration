"""Controlled integration acceptance: merge accepted task branches, run checks.

The integration copy is a disposable worktree at the recorded baseline; the
user's working tree is never read for merging and never written at all.  Each
check command runs with the merged copy as its working directory and its real
exit code is recorded - a clean textual merge says nothing about interface
compatibility, so the verdict comes from these checks, never from merge
success alone.  A failed integration keeps the merged copy for inspection and
writes the record; nothing is retried, nothing overwrites the user's project.

The written record is controller workflow evidence under the record project's
`.model-desk`; it supports - and never replaces - the main brain's independent
acceptance of the individual tasks and of the integrated result.
"""
import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

import model_registry as registry
import task_leases
import task_workspace as workspace

OUTPUT_TAIL = 4000


def completion_digest(record):
    """Bind the synchronous result to its original queue claim, not acceptance.

    Only written after run_integration has returned. It is crash-recovery
    evidence, not a signature against arbitrary local record modification.
    """
    fields = ('project', 'name', 'baseline', 'branches', 'task_ids', 'commits',
              'goal_id', 'request_reference', 'merges', 'merge_passed', 'checks',
              'verdict', 'integrated_commit', 'integration_copy')
    value = {key: record.get(key) for key in fields}
    job = record.get('queue') or {}
    value['claim'] = {key: job.get(key) for key in ('runner', 'lease_owner', 'lease')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _run_check(copy, command, timeout):
    started = time.monotonic()
    argv = [str(part) for part in command]
    try:
        result = subprocess.run(argv, cwd=str(copy), capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as expired:
        return {'command': argv, 'exit_code': None, 'ok': False,
                'duration_s': round(time.monotonic() - started, 3),
                'output_tail': str(expired)[:OUTPUT_TAIL]}
    except OSError as error:
        # A whole shell string passed as one argv element resolves to a missing
        # executable; say so instead of leaving a bare WinError 2.
        hint = ('；检查命令是参数向量且不经 shell 执行，'
                '整串命令请拆分为 [可执行文件, 参数, ...]') if len(argv) == 1 and ' ' in argv[0] else ''
        return {'command': argv,'exit_code':None,'ok':False,
                'duration_s':round(time.monotonic()-started,3),'output_tail':(str(error)+hint)[-OUTPUT_TAIL:]}
    output = (result.stdout or '') + (result.stderr or '')
    return {'command': argv, 'exit_code': result.returncode,
            'ok': result.returncode == 0, 'duration_s': round(time.monotonic() - started, 3),
            'output_tail': output.strip()[-OUTPUT_TAIL:]}


def run_integration(project, branches, *, name, baseline=None, checks=(), timeout=600,
                    record=True, expected_commits=None, keep=False, _lease_owner=None,
                    progress=None):
    """Merge the task branches in order and run the checks on the merged copy.

    One integration at a time per project, guarded by the controller lease
    registry.  Verdicts: `passed` (all merges clean, every check exit 0),
    `merge_conflict` (sequence stopped, checks skipped, copy kept), or
    `check_failed` (all checks ran; copy kept for review).  On success the
    disposable copy is removed - the deliverables stay on their branches.
    """
    if not isinstance(branches, list) or not branches:
        raise ValueError('集成验收需要至少一个任务分支')
    if not isinstance(name, str) or not workspace.NAME.match(name):
        raise ValueError('集成名称不合法')
    names = [workspace._name_or_fail(str(item), '分支名') for item in branches]
    project = Path(project).resolve()
    info = workspace.inspect(project)
    if not info['eligible']:
        raise ValueError('项目不适合受控集成：' + info['reason'])
    baseline = baseline or info['baseline']
    for branch in names:
        if not workspace.branch_exists(project, branch):
            raise ValueError('任务分支不存在：' + workspace.BRANCH_PREFIX + branch)
    if not isinstance(checks, list) or not checks or any(not isinstance(c, list) or not c for c in checks):
        raise ValueError('集成验收至少需要一个检查命令参数向量；仅试合并请用 task_workspace.trial_sequence')
    lease_owner = _lease_owner or 'integration-run:' + uuid.uuid4().hex
    lease = task_leases.acquire('integration', str(project), lease_owner,
                                project=str(project))
    copy = None
    try:
        if progress: progress('preparing', {})
        area = workspace.integration_workspace(project, name, baseline=baseline)
        copy = Path(area['path'])
        if progress: progress('merging', {'integration_copy': {'path': str(copy), 'disposition': 'in_progress'}})
        merges = []
        verdict = 'passed'
        for branch in names:
            merged = workspace.trial_merge(copy, branch, commit=(expected_commits or {}).get(branch))
            merges.append(merged)
            if progress: progress('merging', {'merges': list(merges)})
            if not merged['merged']:
                verdict = 'merge_conflict'
                break
        checks_result = []
        integrated_head=workspace._git(copy,'rev-parse','HEAD')
        integrated_commit=integrated_head.stdout.strip() if integrated_head.returncode == 0 else None
        if verdict == 'passed':
            for index, command in enumerate(checks):
                if progress: progress('checking', {'check_index': index, 'checks': list(checks_result),
                                                  'integrated_commit': integrated_commit})
                outcome = _run_check(copy, command, timeout)
                checks_result.append(outcome)
                if progress: progress('checking', {'check_index': index, 'checks': list(checks_result)})
                if not outcome['ok']:
                    verdict = 'check_failed'
            dirty=workspace._git(copy,'diff','--quiet','HEAD','--')
            staged=workspace._git(copy,'diff','--cached','--quiet','HEAD','--')
            after=workspace._git(copy,'rev-parse','HEAD')
            if dirty.returncode or staged.returncode or after.returncode or after.stdout.strip()!=integrated_commit:
                verdict='check_failed'
                checks_result.append({'command':[],'exit_code':None,'ok':False,
                    'output_tail':'集成检查修改了受版本管理的成果或 HEAD；不能把原提交当作已验证结果'})
        if verdict == 'passed' and not keep:
            try:
                workspace.remove_integration(project, name)
                copy = None
                disposition = 'removed'
            except ValueError:
                # Check commands may leave build artifacts; the dirty-copy
                # protection wins over tidiness and the record says so.
                disposition = 'kept_check_artifacts'
        else:
            disposition = 'kept_for_review'
        result_record = {'schema': 1, 'kind': 'integration-acceptance', 'name': name,
                  'project': str(project), 'baseline': area['baseline'], 'branches': names,
                  'merges': merges, 'merge_passed': all(item.get('merged') for item in merges),
                  'checks': checks_result, 'verdict': verdict,
                  'integrated_commit':integrated_commit,
                  'integration_copy': {'path': str(copy) if copy else None,
                                       'disposition': disposition},
                  'created_at': registry.now()}
        if record:
            registry.write_json(project / '.model-desk' / 'integrations' / (name + '.json'), result_record)
        return result_record
    finally:
        if lease['acquired']:
            task_leases.release('integration', str(project), lease_owner)
            _wake_queue(project)


def _wake_queue(project):
    # No imports, service starts or new scheduler on a normal core call.
    import sys
    queued = sys.modules.get('integration_queue')
    if queued is not None:
        try:
            queued.wake(str(project))
        except (ValueError, OSError, RuntimeError):
            pass  # The durable queued intent remains queryable; never undo a result.


def latest(project, name):
    """Read one integration record; missing records stay missing."""
    workspace._name_or_fail(name, '集成名称')
    file = Path(project) / '.model-desk' / 'integrations' / (name + '.json')
    if not file.exists():
        return None
    return registry.read_json(file)


def _settled_accepted_commits(project, task_ids, *, authority=None):
    """Per-task frozen-commit verification shared by integrate and acceptance."""
    import delegation_policy as policy
    import task_finalization
    project=Path(project).resolve()
    commits={}
    for task_id in task_ids:
        run=registry.task_dir(task_id,project=str(project))
        task=registry.read_task(run)
        if task.get('id') != task_id or Path(task.get('project') or '').resolve() != project:
            raise ValueError('集成任务不属于当前项目')
        policy._require_brain_authority(task,authority)
        task_finalization.require_settled(task)
        review=policy.load(run)
        if review is None or policy.acceptance_problems(review):
            raise ValueError('集成只接受逐条独立验收通过的任务')
        policy.require_result_binding(task,review)
        plan=task.get('workspace') or {}
        if not plan.get('created') or not plan.get('commit'):
            raise ValueError('集成任务没有控制器冻结的独立成果提交')
        branch='refs/heads/'+str(plan.get('branch') or (workspace.BRANCH_PREFIX+task_id))
        tip=workspace._git(project,'rev-parse','--verify',branch)
        if Path(str(plan.get('path') or '')).is_dir():
            workspace.native_roots(run,task)
            head=workspace._git(plan['path'],'rev-parse','HEAD')
            if head.returncode or head.stdout.strip() != plan['commit']:
                raise ValueError('成果 HEAD 与验收任务提交不一致')
            # Post-acceptance working-copy edits must not be silently ignored.
            dirty=[row for row in workspace._status_tree(plan['path'])
                   if not row.startswith('?? .zcode/')]
            if dirty:raise ValueError('已验收工作区又有未提交改动；不能沿用旧验收')
        elif tip.returncode == 0:
            # The worktree was explicitly recycled after acceptance; the branch
            # ref still pins the deliverable commit.
            if tip.stdout.strip() != plan['commit']:
                raise ValueError('任务分支指针与验收提交不一致；不能沿用旧验收')
        else:
            raise ValueError('任务副本与分支均不可读；不能沿用旧验收')
        if not re.fullmatch(r'[0-9a-f]{40,64}',plan['commit']):
            raise ValueError('成果提交标识无效')
        commits[task_id]=plan['commit']
    return commits


def accept_integration(project, name, evidence, *, actor='main', authority=None,
                       request_reference=None):
    """Serialize acceptance with the existing project integration lease.

    Python and broker callers share the same lock and frozen-result checks.
    A persisted request reference identifies an effect after a lost reply; it
    never makes a different request or a prior Python acceptance replayable.
    """
    project = Path(project).resolve()
    workspace._name_or_fail(name, '集成名称')
    # A lease owner identifies this invocation, not the integration name. Two
    # independent reviewers of the same name must not appear as one reentrant
    # holder and both pass the accepted=False check.
    owner = 'integration-review:' + uuid.uuid4().hex
    task_leases.acquire('integration', str(project), owner, project=str(project))
    accepted_result=None
    try:
        # Reconciliation may release a dead runner's lease while recording its
        # receipt. Serialize that write with acceptance too, so neither can
        # overwrite the other's just-persisted result after lease handoff.
        file=project/'.model-desk/integrations'/(name+'.json')
        with registry.file_lock(file.with_suffix('.lock')):
            accepted_result=_accept_integration_locked(project, name, evidence, actor=actor,
                                              authority=authority,
                                              request_reference=request_reference)
            return accepted_result
    finally:
        task_leases.release('integration', str(project), owner)
        if accepted_result is not None:
            import approved_plan
            approved_plan.wake(str(project))
        _wake_queue(project)


def _accept_integration_locked(project, name, evidence, *, actor='main', authority=None,
                               request_reference=None):
    """Main-brain independent acceptance of one kept integration result.

    Re-verifies every referenced task's frozen commit at accept time, requires
    the kept integration copy to still match the pinned integrated commit, and
    records the verdict only with per-criterion main-brain evidence.  The
    accepted integrated commit becomes eligible as a prerequisite baseline for
    later stages (task_handover); the user's working tree is never touched.
    """
    import delegation_policy as policy
    project=Path(project).resolve()
    workspace._name_or_fail(name,'集成名称')
    record=latest(project,name)
    if not isinstance(record,dict) or record.get('kind')!='integration-acceptance':
        raise ValueError('集成记录不存在：'+name)
    if record.get('verdict')!='passed':
        raise ValueError('只有通过全部检查的集成才能验收；当前判定 '+str(record.get('verdict')))
    if record.get('accepted'):
        raise ValueError('该集成已独立验收；已验收结果不能重复验收或改写')
    task_ids=record.get('task_ids') or []
    if not isinstance(task_ids,list) or not task_ids:
        raise ValueError('集成记录缺少任务清单')
    commits=_settled_accepted_commits(project,task_ids,authority=authority)
    if commits!=(record.get('commits') or {}):
        raise ValueError('任务冻结提交与集成记录不一致；集成已过期，请重新受控集成')
    integrated=str(record.get('integrated_commit') or '')
    if not re.fullmatch(r'[0-9a-f]{40,64}',integrated):
        raise ValueError('集成提交标识无效')
    for task_id,commit in commits.items():
        if workspace._git(project,'merge-base','--is-ancestor',commit,integrated).returncode:
            raise ValueError('集成提交未包含任务 '+task_id+' 的成果提交')
    copy=(record.get('integration_copy') or {}).get('path')
    if not copy or not Path(copy).is_dir():
        raise ValueError('集成副本缺失，无法核对当前状态；请重新受控集成')
    head=workspace._git(copy,'rev-parse','HEAD')
    if head.returncode or head.stdout.strip()!=integrated:
        raise ValueError('集成副本 HEAD 与记录的集成提交不一致；不能据此验收')
    dirty=[row for row in workspace._status_tree(copy) if not row.startswith('?? .zcode/')]
    if dirty:raise ValueError('集成副本存在未提交改动；不能据此验收')
    rows=policy._decision_rows(evidence)
    if not all(row['status']=='pass' for row in rows):
        raise ValueError('存在未通过的验收条件，不能验收该集成')
    for row in rows:
        if not any(entry['source'] in policy.INDEPENDENT_SOURCES for entry in row['evidence']):
            raise ValueError('每条集成验收条件都需要主脑独立证据：'+row['key'])
    record.update(accepted=True,accepted_at=registry.now(),actor=str(actor)[:40],
                  accepted_commit=integrated,evidence=rows,
                  note='主脑已独立验收该集成提交；可作为后续阶段的前置基线；未覆盖或改写用户原树')
    if isinstance(record.get('queue'),dict):
        record['queue'].update(state='accepted',updated_at=record['accepted_at'],
                               detail='集成提交已独立验收，原项目未自动更新。')
    if request_reference is not None:
        record['acceptance_request_reference'] = request_reference
    registry.write_json(project/'.model-desk/integrations'/(name+'.json'),record)
    return record


def integration_baseline(project, commits):
    """One accepted integrated commit covering exactly these prerequisite commits.

    Reads only accepted integration records and re-checks ancestry in the real
    repository, so a stale or fabricated record can never become a baseline.
    Returns None when no accepted integration covers this exact commit set.
    """
    if not commits:return None
    project=Path(project).resolve()
    wanted={str(commit) for commit in commits}
    folder=project/'.model-desk'/'integrations'
    found={}
    if folder.is_dir():
        for file in sorted(folder.glob('*.json')):
            try:record=registry.read_json(file)
            except (OSError,ValueError):continue
            if not isinstance(record,dict) or not record.get('accepted') or record.get('verdict')!='passed':
                continue
            covered=set((record.get('commits') or {}).values())
            integrated=str(record.get('integrated_commit') or '')
            if not covered or covered!=wanted:continue
            if not re.fullmatch(r'[0-9a-f]{40,64}',integrated):continue
            if any(workspace._git(project,'merge-base','--is-ancestor',commit,integrated).returncode
                   for commit in wanted):continue
            found[str(record.get('name') or file.stem)]=integrated
    if not found:return None
    unique=set(found.values())
    if len(unique)!=1:
        raise ValueError('多个已验收集成覆盖同一组前置提交且结果不同；请人工裁决后清理旧记录')
    return unique.pop()


def integrate_tasks(project, task_ids, *, name, checks, authority=None, request_reference=None,
                    baseline=None, _lease_owner=None, progress=None, queue_job=None):
    """Integrate proven accepted task results, never arbitrary branch names.

    Pin commit IDs rather than mutable branch refs. A passing check result is
    evidence for the main brain, not automatic acceptance or application to the
    user's working tree. Both brain adapters use this same core operation.
    """
    if (not isinstance(task_ids,list) or not 1 <= len(task_ids) <= 8
            or any(not isinstance(value,str) for value in task_ids) or len(set(task_ids)) != len(task_ids)):
        raise ValueError('集成需要 1 到 8 个不同的已验收任务')
    project=Path(project).resolve()
    goal_marker=object()
    goal=goal_marker
    for task_id in task_ids:
        current_goal=(registry.read_task(registry.task_dir(task_id,project=str(project))).get('goal_id'))
        if goal is goal_marker:goal=current_goal
        elif current_goal != goal:raise ValueError('集成任务不属于同一目标')
    commits=_settled_accepted_commits(project,task_ids,authority=authority)
    owner = _lease_owner or 'integration-run:' + uuid.uuid4().hex
    lease = task_leases.acquire('integration', str(project), owner, project=str(project))
    try:
        # Keep the project lock through the durable receipt, not only Git/checks.
        result=run_integration(project,list(task_ids),name=name,baseline=baseline,checks=checks,
            record=False,expected_commits=commits,keep=True,_lease_owner=owner,progress=progress)
        result.update(task_ids=list(task_ids),commits=commits,goal_id=goal,accepted=False,
                      request_reference=request_reference,
                      note='集成检查结果待主脑独立验收；未覆盖原项目，未自动应用或验收')
        if queue_job is not None:
            code={'passed':'awaiting_review','merge_conflict':'conflict','check_failed':'check_failed'}[result['verdict']]
            result['queue'] = {**queue_job, 'state':code,'updated_at':registry.now(),
                'detail':{'awaiting_review':'合并与联合检查已结束，等待主脑独立验收。',
                          'conflict':'隔离副本存在合并冲突，成果保留。',
                          'check_failed':'联合检查未通过，集成成果未被接受。'}[code]}
            result['queue']['completion'] = {'schema': 1,
                'result_sha256': completion_digest(result), 'recorded_at': registry.now()}
        registry.write_json(project/'.model-desk/integrations'/(name+'.json'),result)
        return result
    finally:
        if lease['acquired']:
            task_leases.release('integration', str(project), owner)
            _wake_queue(project)
