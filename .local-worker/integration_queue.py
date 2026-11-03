"""Durable integration intents in the existing project integration records.

No second ledger, model scheduler, service startup or import-time work. Only an
explicit enqueue/resume registers work with the caller's existing executor.
Running/unknown work is never resent. A completed claim can be explicitly
settled from durable result + dead-runner evidence, never from lease age.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid

import integration_status
import model_registry as registry
import process_identity
import task_integration as integration
import task_leases

REQUEST = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
ACTIVE = {'preparing', 'merging', 'checking'}
LABELS = {'queued':'等待集成', 'preparing':'准备集成副本', 'merging':'正在合并成果',
          'checking':'正在联合检查', 'awaiting_review':'等待集成验收', 'accepted':'集成已验收',
          'conflict':'集成存在冲突', 'check_failed':'集成检查未通过', 'blocked':'集成准备受阻',
          'recovery_required':'集成结果待核对', 'cancelled':'集成已取消'}
_LOCK = threading.RLock()
_ACTIVE = set()
_AGAIN = set()
_SUBMIT = {}
_AUTHORITY = {}


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _frozen_digest(row):
    return _digest({k:row.get(k) for k in ('baseline','commits','goal_id')})


def _verify_job(row):
    job=row.get('queue')
    if job is None:return
    if not isinstance(job,dict) or type(job.get('schema')) is not int or job['schema']!=1:
        raise ValueError('集成意图版本或结构不正确')
    if (not isinstance(job.get('state'),str) or job['state'] not in LABELS
            or type(job.get('requires_authority')) is not bool):
        raise ValueError('集成意图状态或责任声明无法核对')
    _checks(job.get('check_commands'))
    signature=_digest({'project':str(_root(row['project'])),'task_ids':row.get('task_ids'),
        'checks':job['check_commands'],'reference':row.get('request_reference')})
    if job.get('signature')!=signature or job.get('frozen_sha256')!=_frozen_digest(row):
        raise ValueError('原集成意图或冻结输入发生变化，不能执行')


def _root(project):
    if not isinstance(project, (str, Path)) or not Path(project).is_absolute():
        raise ValueError('集成队列需要明确的项目绝对目录')
    root = Path(project).resolve()
    if not root.is_dir(): raise ValueError('项目目录不存在')
    return root


def _file(project, name):
    if not isinstance(name, str) or not integration.workspace.NAME.fullmatch(name):
        raise ValueError('集成名称不合法')
    root = _root(project)
    folder = root/'.model-desk/integrations'
    if folder.resolve() != folder: raise ValueError('集成记录路径不可核对')
    file = folder/(name+'.json')
    if file.exists() and (file.is_symlink() or file.resolve()!=file or not file.is_file()
                          or file.stat().st_nlink!=1 or file.stat().st_size>1_000_000):
        raise ValueError('集成记录文件不可核对')
    return file


def records(project):
    root = _root(project)
    value = integration_status.read_project(str(root))
    if not value['available']: raise ValueError('集成记录无法完整读取，未重置队列')
    for row in value['records']:_verify_job(row)
    return value['records']


def get(project, name):
    file = _file(project, name)
    if not file.exists(): return None
    row = registry.read_json(file)
    if (not isinstance(row,dict) or type(row.get('schema')) is not int or row.get('schema')!=1 or row.get('kind')!='integration-acceptance'
            or row.get('name')!=name or _root(row.get('project'))!=_root(project)):
        raise ValueError('集成记录归属或结构不正确')
    _verify_job(row)
    return row


def state(row):
    if row.get('verdict')=='passed':
        commit=row.get('integrated_commit')
        if not isinstance(commit,str) or not integration_status.COMMIT.fullmatch(commit):
            return 'recovery_required'
        if row.get('accepted') is True and row.get('accepted_commit')==commit:
            return 'accepted'
        return 'awaiting_review' if row.get('accepted') is False else 'recovery_required'
    if row.get('verdict') in {'merge_conflict','check_failed'}:
        return {'merge_conflict':'conflict','check_failed':'check_failed'}[row['verdict']]
    job = row.get('queue') or {}
    code=job.get('state')
    return code if isinstance(code,str) and code in LABELS else 'recovery_required'


def snapshot(project, *, detail=False):
    if not project: return {'records':[], 'available':False, 'error':'请先选择一个明确项目'}
    try:
        rows = sorted(records(project),key=lambda r:(r.get('queue') or {}).get('enqueued_at') or r.get('created_at',''),reverse=True)
        result=[]
        for row in rows:
            observation=observe(row);code=observation['state'];job=row.get('queue') or {}
            out={k:row.get(k) for k in ('name','goal_id','task_ids','created_at','accepted','integrated_commit')}
            out.update(state=code,label=LABELS[code],updated_at=job.get('updated_at') or row.get('accepted_at') or row.get('created_at'),
                       queued=bool(job),requires_owner=bool(job.get('requires_authority')),
                       detail=observation['detail'],recovery=observation['recovery'],
                       check_count=len(row.get('checks') or []))
            if detail: out['record']=row
            result.append(out)
        return {'records':result,'available':True,'read_only':True}
    except (ValueError,OSError,RuntimeError,TypeError,KeyError) as error:
        return {'records':[],'available':False,'error':str(error),'read_only':True}


def _authorise(project, ids, authority):
    import delegation_policy as policy
    owned=False
    for task_id in ids:
        run=registry.task_dir(task_id,project=str(project));task=registry.read_task(run)
        if task.get('id')!=task_id or _root(task.get('project'))!=_root(project):
            raise ValueError('集成任务不属于当前项目')
        policy._require_brain_authority(task,authority)
        owned=owned or policy._brain_identity(task)[0]
    return owned


def _checks(checks):
    if (not isinstance(checks,list) or not 1<=len(checks)<=8 or any(
            not isinstance(c,list) or not 1<=len(c)<=40 or any(
                not isinstance(v,str) or not v or len(v)>4000 or '\0' in v for v in c) for c in checks)):
        raise ValueError('集成检查需要1到8个命令参数数组')
    if len(json.dumps(checks,ensure_ascii=False).encode())>128_000:
        raise ValueError('集成检查参数过大，请缩小计划')


def enqueue(project, task_ids, checks, *, request_id, authority=None, request_reference=None, name=None):
    root=_root(project)
    if not isinstance(request_id,str) or not REQUEST.fullmatch(request_id):raise ValueError('集成请求标识不合法')
    if (not isinstance(task_ids,list) or not 1<=len(task_ids)<=8 or
            any(not isinstance(i,str) or not integration_status.TASK_ID.fullmatch(i) for i in task_ids)
            or len(set(task_ids))!=len(task_ids)):
        raise ValueError('集成需要1到8个不同任务')
    _checks(checks)
    owned=_authorise(root,task_ids,authority)
    reference=request_reference or 'panel-integration/'+request_id
    name=name or 'queue-'+hashlib.sha256(reference.encode()).hexdigest()[:24]
    file=_file(root,name)
    payload={'project':str(root),'task_ids':task_ids,'checks':checks,'reference':reference}
    signature=_digest(payload)
    with registry.file_lock(file.with_suffix('.lock')):
        old=get(root,name)
        if old is not None:
            if (old.get('queue') or {}).get('signature')!=signature:raise ValueError('原集成请求参数不同，不能覆盖或重建')
            return old
        commits=integration._settled_accepted_commits(root,task_ids,authority=authority)
        goals={registry.read_task(registry.task_dir(i,str(root))).get('goal_id') for i in task_ids}
        if len(goals)!=1:raise ValueError('集成任务不属于同一目标')
        info=integration.workspace.inspect(root)
        if not info['eligible']:raise ValueError('项目不适合受控集成：'+info['reason'])
        now=registry.now()
        row={'schema':1,'kind':'integration-acceptance','project':str(root),'name':name,
             'baseline':info['baseline'],'task_ids':list(task_ids),'commits':commits,'goal_id':goals.pop(),
             'accepted':False,'verdict':None,'checks':[],'created_at':now,'request_reference':reference,
             'queue':{'schema':1,'state':'queued','request_id':request_id,'signature':signature,
                      'check_commands':checks,'enqueued_at':now,'updated_at':now,
                      'requires_authority':owned,'detail':'已保存集成计划，尚未开始；不会覆盖原项目。'}}
        row['queue']['frozen_sha256']=_frozen_digest(row)
        registry.write_json(file,row)
        return row


def cancel(project,name,*,authority=None):
    row=get(project,name)
    if row is None:raise ValueError('集成记录不存在')
    _authorise(project,row.get('task_ids') or [],authority)
    file=_file(project,name)
    with registry.file_lock(file.with_suffix('.lock')):
        row=get(project,name)
        if state(row)=='cancelled':return row
        if state(row)!='queued':raise ValueError('集成已经开始或结束，不能作为未执行排队取消')
        row['queue'].update(state='cancelled',updated_at=registry.now(),detail='排队已取消；任务成果保留，不自动恢复。')
        registry.write_json(file,row)
    return row


def run_one(project,name,*,authority=None):
    root=_root(project);row=get(root,name)
    if row is None:raise ValueError('集成记录不存在')
    _authorise(root,row.get('task_ids') or [],authority)
    if state(row)!='queued':return row
    owner='integration-queue:'+uuid.uuid4().hex
    lease=task_leases.acquire('integration',str(root),owner,project=str(root))
    file=_file(root,name);keep_lease=False;claimed=False
    try:
        with registry.file_lock(file.with_suffix('.lock')):
            row=get(root,name)
            if state(row)!='queued':return row
            current=integration._settled_accepted_commits(root,row['task_ids'],authority=authority)
            if current!=row['commits']:raise ValueError('排队后冻结成果发生变化，不能使用旧计划')
            identity=process_identity.capture(os.getpid())
            if not process_identity.well_formed(identity):raise ValueError('无法核实集成执行进程身份')
            row['queue'].update(state='preparing',runner=identity,lease_owner=owner,
                lease=lease['lease'],
                updated_at=registry.now(),detail='已领取原作业，正在准备隔离集成；不调用模型。')
            registry.write_json(file,row)
            claimed=True
        def progress(code, fields):
            with registry.file_lock(file.with_suffix('.lock')):
                current=get(root,name)
                if current['queue'].get('lease_owner')!=owner:raise ValueError('集成作业身份发生变化')
                current.update(fields)
                current['queue'].update(state=code,updated_at=registry.now(),detail=LABELS[code])
                registry.write_json(file,current)
        return integration.integrate_tasks(root,row['task_ids'],name=name,checks=row['queue']['check_commands'],
            authority=authority,request_reference=row['request_reference'],baseline=row['baseline'],
            _lease_owner=owner,progress=progress,queue_job=row['queue'])
    except BaseException as error:
        keep_lease=claimed
        with registry.file_lock(file.with_suffix('.lock')):
            current=get(root,name)
            if current and current.get('verdict') in {'passed','merge_conflict','check_failed'}:
                keep_lease=False
                return current
            if current and current.get('queue',{}).get('lease_owner')==owner:
                keep_lease=True
                current['queue'].update(state='recovery_required',updated_at=registry.now(),
                    error_kind=type(error).__name__,detail='原集成未取得完整终态，请核对现场；不自动重跑或释放占用。')
            elif current:
                current['queue'].update(state='blocked',updated_at=registry.now(),detail=str(error)[:300])
            if current:registry.write_json(file,current)
        raise
    finally:
        if not keep_lease:
            task_leases.release('integration',str(root),owner)
        # Update an already attached plan after the original job ends. A passed
        # check is still awaiting independent acceptance, never auto-accepted.
        import approved_plan
        approved_plan.wake(str(root))


def _completion_problem(row):
    """Require a complete new receipt; partial/legacy results cannot free a lock."""
    job=row.get('queue') or {};proof=job.get('completion');lease=job.get('lease')
    if (not isinstance(proof,dict) or type(proof.get('schema')) is not int or proof['schema']!=1
            or proof.get('result_sha256')!=integration.completion_digest(row)):
        return 'missing_completion'
    owner=job.get('lease_owner');runner=job.get('runner')
    if (not isinstance(owner,str) or not re.fullmatch(r'integration-queue:[0-9a-f]{32}',owner)
            or not process_identity.well_formed(runner) or not isinstance(lease,dict)
            or lease.get('task_id')!=owner or type(lease.get('pid')) is not int
            or lease['pid']!=runner['pid'] or lease.get('project')!=row['project']
            or not isinstance(lease.get('acquired_at'),str) or not lease['acquired_at']):
        return 'invalid_claim'
    ids=row.get('task_ids');merges=row.get('merges');checks=row.get('checks')
    if (not isinstance(ids,list) or not ids or not isinstance(merges,list)
            or not merges or not isinstance(checks,list) or type(row.get('accepted')) is not bool
            or row.get('branches')!=ids):
        return 'incomplete_result'
    for index,merge in enumerate(merges):
        if (index>=len(ids) or not isinstance(merge,dict)
                or merge.get('branch')!=integration.workspace.BRANCH_PREFIX+ids[index]
                or type(merge.get('merged')) is not bool or not isinstance(merge.get('conflicts'),list)):
            return 'incomplete_result'
    verdict=row.get('verdict')
    if row['accepted'] is True and (verdict!='passed' or row.get('accepted_commit')!=row.get('integrated_commit')):
        return 'incomplete_result'
    if verdict=='merge_conflict':
        if (row.get('merge_passed') is not False or checks or row['accepted'] is not False
                or merges[-1]['merged'] is not False or any(not m['merged'] for m in merges[:-1])):
            return 'incomplete_result'
    elif verdict in {'passed','check_failed'}:
        plan=job['check_commands']
        if (row.get('merge_passed') is not True or len(merges)!=len(ids)
                or any(not m['merged'] for m in merges) or len(checks) not in {len(plan),len(plan)+1}
                or not isinstance(row.get('integrated_commit'),str)
                or not integration_status.COMMIT.fullmatch(row['integrated_commit'])):
            return 'incomplete_result'
        for command,result in zip(plan,checks):
            if (not isinstance(result,dict) or result.get('command')!=command
                    or type(result.get('exit_code')) is not int or type(result.get('ok')) is not bool
                    or result['ok']!=(result['exit_code']==0)):
                return 'unconfirmed_check'
        if verdict=='passed' and (len(checks)!=len(plan) or any(not c['ok'] for c in checks)):
            return 'incomplete_result'
        if verdict=='check_failed' and (row['accepted'] is not False or
                (len(checks)==len(plan) and all(c['ok'] for c in checks))):
            return 'incomplete_result'
        if len(checks)>len(plan) and (not isinstance(checks[-1],dict)
                or checks[-1].get('command')!=[] or checks[-1].get('ok') is not False):
            return 'incomplete_result'
    else:return 'incomplete_result'
    return None


def _held(project):
    # Atomic registry replacement supplies a coherent read. Do not create even
    # a lock file during a page/read-only observation; release rechecks under lock.
    file=task_leases._file(None)
    return task_leases._load(file)['leases'].get(task_leases.key('integration',str(project)))


def recovery_status(row):
    """Read-only, structured next step; never infer completion from Git HEAD."""
    job=row.get('queue') or {}
    if not job:return {'state':'legacy','can_settle':False,'detail':'旧同步记录不自动补造收尾证明。'}
    owner=job.get('lease_owner')
    try:held=_held(row['project'])
    except (ValueError,OSError,RuntimeError):
        return {'state':'unknown','can_settle':False,'detail':'占用记录无法核对，保留现场。'}
    owns=bool(owner and held and held.get('task_id')==owner)
    if not owner:
        return {'state':'not_started','can_settle':False,'detail':'未记录执行领取，不需要释放本作业占用。'}
    if (not owns and isinstance(job.get('completion'),dict)
            and job['completion'].get('result_sha256')==integration.completion_digest(row)):
        return {'state':'settled','can_settle':False,'detail':'本作业收尾已确认；其他作业的占用不受影响。'}
    if not owns:
        return {'state':'not_held','can_settle':False,
                'detail':'原作业当前未持有集成占用；执行结果仍按原记录核对，不据此重跑或验收。'}
    if _completion_problem(row) is not None:
        return {'state':'needs_evidence','can_settle':False,
                'detail':'缺少完整的原执行及检查结束证明；保留现场，不重新合并或运行检查。'}
    if held!=job.get('lease'):
        return {'state':'unknown','can_settle':False,'detail':'占用与原领取快照不一致，不能释放。'}
    running=_runner_state(job.get('runner'))
    if running is False:
        return {'state':'ready','can_settle':True,
                'detail':'完整结果已保存，原执行进程已退出；可只释放原作业占用，不重跑或验收。'}
    return {'state':'running' if running is True else 'unknown','can_settle':False,
            'detail':'原执行进程仍存在，等待其收尾。' if running is True else '原执行进程身份无法确认，保留占用。'}


def _runner_state(identity):
    if not process_identity.well_formed(identity):return None
    import psutil
    try:
        process=psutil.Process(identity['pid'])
        return process_identity.matches(identity,process.pid,process.create_time(),process.cmdline())
    except psutil.NoSuchProcess:return False
    except (psutil.Error,OSError,ValueError):return None


def observe(row):
    """Read-only runtime observation; never change a receipt or free a lease."""
    code=state(row);job=row.get('queue') or {};detail=job.get('detail','')
    if code in ACTIVE:
        verdict=_runner_state(job.get('runner'))
        if verdict is not True:
            code='recovery_required'
            detail='原进程已退出或身份变化；核对原作业，不自动重跑。' if verdict is False else '原进程状态无法确认；核对原作业，不自动重跑。'
    elif code=='queued':
        try:
            held=task_leases._load(task_leases._file(None))['leases'].get(task_leases.key('integration',row['project']))
            if held:detail='本项目集成资源已被占用，原作业尚未开始。'
            elif (row['project'],row['name']) not in _AUTHORITY:
                detail='原主脑需重连并显式继续此作业。' if job.get('requires_authority') else '原作业已保存，等待显式继续队列。'
        except (ValueError,OSError):detail='资源状态暂时无法确认，原作业仍未开始。'
    return {'state':code,'label':LABELS[code],'detail':detail,'recovery':recovery_status(row),'read_only':True}


def reconcile(project,name,*,authority=None,request_reference=None):
    """Settle a proven completed claim only; never replay unfinished effects."""
    row=get(project,name)
    if row is None:raise ValueError('集成记录不存在')
    _authorise(project,row.get('task_ids') or [],authority)
    file=_file(project,name)
    with registry.file_lock(file.with_suffix('.lock')):
        current=get(project,name)
        _authorise(project,current.get('task_ids') or [],authority)
        job=current.get('queue') or {};status=recovery_status(current)
        if status['can_settle']:
            # release rechecks the owner atomically. Holding the record lock
            # prevents a competing reconciliation from rewriting our evidence.
            task_leases.release('integration',str(_root(project)),job['lease_owner'],expected=job['lease'])
            job['settlement']={'state':'settled','reason':'completed_runner_exited','recorded_at':registry.now()}
            status=recovery_status(current)
        if state(current) in ACTIVE|{'recovery_required'} and _runner_state(job.get('runner')) is not True:
            current['queue'].update(state='recovery_required',updated_at=registry.now(),
                detail=status['detail'])
        if job:
            job['reconciliation']={'request_reference':request_reference,'observed_at':registry.now(),
                                   'state':status['state'],'detail':status['detail']}
            job['updated_at']=registry.now()
            registry.write_json(file,current)
    # Recovery itself never schedules even another job. Continue an already
    # approved queued intent explicitly through the existing resume operation.
    return current


def schedule(project,name,submit,*,authority=None):
    """Attach the original authorized intent to the existing service executor."""
    root=str(_root(project));row=get(root,name)
    if row is None:raise ValueError('集成记录不存在')
    _authorise(root,row.get('task_ids') or [],authority)
    with _LOCK:
        _AUTHORITY[(root,name)]=authority;_SUBMIT[root]=submit
    wake(root)
    return row


def wake(project):
    root=str(_root(project))
    with _LOCK:
        if root not in _SUBMIT:return
        if root in _ACTIVE:
            _AGAIN.add(root)
            return
        if not any(state(row)=='queued' and (root,row['name']) in _AUTHORITY for row in records(root)):
            return
        _ACTIVE.add(root);submit=_SUBMIT[root]
        try:submit(_drain,root)
        except Exception:_ACTIVE.discard(root);raise


def _drain(project):
    try:
        while True:
            pending=sorted((r for r in records(project) if state(r)=='queued'),key=lambda r:r['queue']['enqueued_at'])
            ran=False
            for row in pending:
                key=(project,row['name'])
                with _LOCK:
                    registered=key in _AUTHORITY;authority=_AUTHORITY.get(key)
                if not registered:continue  # restart never invents a signed owner authority
                try:run_one(project,row['name'],authority=authority)
                except task_leases.LeaseConflict:return  # holder's durable release wakes this pump
                except (ValueError,OSError,RuntimeError):return
                ran=True;break
            if not ran:return
    finally:
        with _LOCK:
            _ACTIVE.discard(project)
            again=project in _AGAIN
            _AGAIN.discard(project)
        if again:wake(project)
