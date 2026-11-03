"""Operator-approved stage progression over the existing queue and stage DB.

No provider tools, second scheduler, independent acceptance or restart replay.
Approval is a local controller declaration, not proof of a human UI click.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import threading
import uuid

import model_registry as registry
import stage_store as stages

KEY='approved_plan'
FIELDS=('id','goal_id','title','objective','kind','project','files','acceptance',
        'candidates','fixed_model','depends_on','session_policy')
POLICY_FIELDS=('model_scope','paid_allowed','enforce_paid_budget','enforce_model_call_limit',
               'task_budget_cny','daily_budget_cny','max_model_calls','max_parallel_tasks')
MODEL_FIELDS=('id','name','provider','model','api_base','enabled','default_agent',
              'max_input_tokens','max_output_tokens','thinking_level','native_selection')
_LOCK=threading.RLock()
_BINDINGS={}
_ACTIVE=set()
_AGAIN=set()


class NotSubmitted(ValueError):
    """Controller proved the atomic stage claim never happened."""


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def project_root(project):
    if not isinstance(project,str) or not Path(project).is_absolute() or not Path(project).is_dir():
        raise ValueError('批准计划需要明确、存在的绝对项目目录')
    return str(Path(project).resolve())


def _goal(conn,goal):
    row=conn.execute('SELECT * FROM goals WHERE id=?',(goal,)).fetchone()
    if row is None:raise ValueError('目标不存在或不属于当前项目')
    return stages._row_goal(row)


def _definition(conn,goal):
    rows=[stages._row_stage(r) for r in conn.execute('SELECT * FROM stages WHERE goal_id=? ORDER BY ordinal',(goal['id'],))]
    if not rows:raise ValueError('计划需要至少一个阶段')
    by_id={r['id']:r for r in rows};visiting=set();done=set()
    def visit(sid):
        if sid in visiting:raise ValueError('阶段依赖存在环路')
        if sid in done:return
        if sid not in by_id:raise ValueError('前置阶段不属于本目标')
        visiting.add(sid)
        for dep in by_id[sid]['depends_on']:visit(dep)
        visiting.remove(sid);done.add(sid)
    for row in rows:
        visit(row['id'])
        if Path(row['project']).resolve()!=Path(goal['project']).resolve():raise ValueError('阶段项目与目标不一致')
        if row['session_policy']!=stages.SESSION_NEW:raise ValueError('自动计划只支持新会话，不自动复用旧会话')
    responsibility=goal['data'].get('plan_brain')
    if responsibility is not None and (not isinstance(responsibility,dict) or responsibility.get('ready') is not True):
        raise ValueError('主脑草案尚未完整保存，不能批准或派发')
    return {'goal':{k:goal[k] for k in ('id','title','objective','project')},
            'responsibility':({'mode':'brain',**{k:responsibility[k] for k in ('owner','binding_key','host')}}
                              if responsibility else {'mode':'controller'}),
            'stages':[{k:r[k] for k in FIELDS} for r in rows]}


def _context(definition):
    import execution_endpoints
    import execution_engines
    import endpoint_access
    import workflow_settings
    import workbuddy_native_tools
    import zcode_worker
    settings=workflow_settings.snapshot()
    if settings.get('readable') is not True:raise ValueError('协作设置不可读')
    controls=execution_endpoints.snapshot()
    if controls.get('readable') is not True:raise ValueError('执行端设置不可读')
    if not registry.REGISTRY.is_file():raise ValueError('模型登记尚未配置，不自动初始化')
    models={m['id']:m for m in registry.read_registry()['models']}
    selected={}
    for stage in definition['stages']:
        choices=stage['candidates'];model_id=stage['fixed_model'] or (choices[0] if len(choices)==1 else '')
        if not model_id:raise ValueError('自动计划需为每阶段固定一个模型或仅保留一个候选：'+stage['title'])
        model=models.get(model_id)
        if not model or not model.get('enabled',True):raise ValueError('计划模型不存在或已停用：'+model_id)
        engine,_=execution_engines.resolution({},settings,model=model)
        if engine not in execution_endpoints.IDS:raise ValueError('该执行端不支持自动阶段计划')
        mode=(endpoint_access.saved(engine) if engine in ('pi','dsh') else
              workbuddy_native_tools.access_mode() if engine=='workbuddy' else zcode_worker.configuration()['access_mode'])
        selected[stage['id']]={'model_id':model_id,'model_name':model.get('name',model_id),'engine':engine,
                              'access_mode':mode,'model_digest':digest({k:model.get(k) for k in MODEL_FIELDS}),
                              'endpoint_digest':digest({k:v for k,v in controls['endpoints'][engine].items() if k!='enabled'})}
    from task_workspace import _git
    root=definition['goal']['project']
    head=_git(root,'rev-parse','HEAD')
    baseline=head.stdout.strip() if head is not None and head.returncode==0 else None
    if any(s['kind']=='edit' for s in definition['stages']) and not baseline:
        raise ValueError('首版自动修改计划需要已有Git基线；不会初始化Git')
    return {'models':selected,'policy':{k:settings.get(k) for k in POLICY_FIELDS},'baseline':baseline}


def preview(goal,project,checks=None):
    project=project_root(project)
    with stages.connection(project) as conn:
        current=_goal(conn,goal);definition=_definition(conn,current)
        if Path(current['project']).resolve()!=Path(project):raise ValueError('目标不属于当前项目')
        if checks is None:
            checks=(current['data'].get(KEY) or {}).get('document',{}).get('integration_checks',
                (current['data'].get('plan_brain') or {}).get('integration_checks',[]))
    if checks != [] and checks != ():
        import integration_queue
        integration_queue._checks(checks)
        if any(len(s['depends_on'])>8 for s in definition['stages']):
            raise ValueError('自动集成每组最多8个前置阶段，请先拆分计划')
    elif not isinstance(checks,(list,tuple)):
        raise ValueError('联合检查需要命令参数数组')
    context=_context(definition)
    document={'schema':1,'definition':definition,'context':context,'integration_checks':list(checks),
              'delivery':'accepted_isolated_results','automatic_rework':False}
    return {'document':document,'digest':digest(document),'read_only':True,
            'note':'批准只覆盖此版本与已保存权限；不自动验收、发布、更新原树或重发未知请求。'}


@contextmanager
def locked(project,goal):
    # The database must already contain this goal. Reads alone create no lock.
    with stages.connection(project) as conn:
        if Path(_goal(conn,goal)['project']).resolve()!=Path(project):raise ValueError('目标不属于当前项目')
    with registry.file_lock(Path(project)/'.model-desk'/('plan-'+digest(goal)[:20]+'.lock')):yield


def _save(conn,goal,plan,action):
    data=dict(goal['data']);data[KEY]=plan
    conn.execute('UPDATE goals SET data=?,updated_at=? WHERE id=?',
                 (json.dumps(data,ensure_ascii=False),registry.now(),goal['id']))
    stages._log(conn,action='plan_'+action,actor='operator',goal_id=goal['id'],detail='revision='+str(plan['revision']))


def _read(project,goal):
    with stages.connection(project) as conn:
        row=_goal(conn,goal)
        if Path(row['project']).resolve()!=Path(project):raise ValueError('目标不属于当前项目')
        plan=row['data'].get(KEY) or {}
        if plan and (not isinstance(plan,dict) or plan.get('schema')!=1 or
                     digest(plan.get('document'))!=plan.get('digest')):
            raise ValueError('批准计划内容与原摘要不符，保留原记录，不推进')
        return row,plan


def view(goal,project):
    project=project_root(project);row,plan=_read(project,goal)
    key=(project,goal)
    with _LOCK:attached=key in _BINDINGS
    return {'goal_id':goal,'project':project,'plan':plan or None,'attached':attached,'read_only':True,
            'responsibility':row['data'].get('plan_brain'),
            'note':'重启后需显式续接原批准计划；读取不派发，不推断旧请求未执行。'}


def change(goal,project,action,*,expected,checks=None):
    project=project_root(project)
    if action not in ('approve','pause','resume','revoke'):raise ValueError('计划操作不正确')
    with locked(project,goal):
        if action in ('approve','resume'):
            fresh=preview(goal,project,checks)
            if fresh['digest']!=expected:raise ValueError('计划或模型/权限/基线已变化，请重新预览并批准')
        current,existing=_read(project,goal)
        if current['status']!=stages.GOAL_ACTIVE:raise ValueError('目标已结束，不能推进计划')
        if action=='approve' and existing.get('status')=='approved' and existing.get('digest')==expected:
            return view(goal,project)
        with stages.connection(project) as conn,stages._transaction(conn):
            row=_goal(conn,goal);old=row['data'].get(KEY) or {}
            if row['status']!=stages.GOAL_ACTIVE:raise ValueError('目标已结束，不能推进计划')
            if action=='approve':
                # Every attempt survives a new approval; no new revision can
                # erase an uncertain submission and silently send it again.
                plan={'schema':1,'revision':old.get('revision',0)+1,'digest':expected,
                      'document':fresh['document'],'status':'approved','approved_at':registry.now(),
                      'attempts':old.get('attempts',{}),
                      'integrations':old.get('integrations',{}),
                      'attempt_history':old.get('attempt_history',[]),
                      'revisions':old.get('revisions',[])+([{
                          k:old[k] for k in ('revision','digest','document','approved_at')} ] if old else []),
                      'observations':{},'history':(old.get('history',[])+[{'action':'approve','at':registry.now(),'digest':expected}])[-100:]}
            else:
                if not old or old.get('digest')!=expected:raise ValueError('需要当前批准计划的原摘要')
                if action=='resume' and (old['status']=='revoked' or fresh['document']!=old['document']):
                    raise ValueError('已撤销或计划变化，不能续接；需要重新批准')
                plan=dict(old);plan['status']='approved' if action=='resume' else 'paused' if action=='pause' else 'revoked'
                plan['history']=(old.get('history',[])+[{'action':action,'at':registry.now(),'digest':expected}])[-100:]
            if action in ('approve','resume'):
                for sid,attempt in list(plan['attempts'].items()):
                    stage=stages._stage(conn,sid)
                    if attempt.get('state')=='not_submitted' and not stage['task_ids'] and not stage['active_task_id']:
                        plan.setdefault('attempt_history',[]).append({'stage_id':sid,**attempt})
                        del plan['attempts'][sid]
            _save(conn,row,plan,action)
    if action in ('pause','revoke'):
        with _LOCK:_BINDINGS.pop((project,goal),None)
    return view(goal,project)


def _mutate(project,goal,function):
    with stages.connection(project) as conn,stages._transaction(conn):
        row=_goal(conn,goal);plan=row['data'].get(KEY)
        if not plan:raise ValueError('没有批准计划')
        function(plan)
        data=dict(row['data']);data[KEY]=plan
        conn.execute('UPDATE goals SET data=? WHERE id=?',(json.dumps(data,ensure_ascii=False),goal))


def validate_claim(conn,stage,proof,*,model_id,engine,brain=None,authority=None):
    """Inside the original atomic stage claim, immediately before task creation."""
    if not isinstance(proof,dict) or set(proof)!={'revision','digest','token'}:raise ValueError('批准计划派发证明无效')
    goal=_goal(conn,stage['goal_id']);plan=goal['data'].get(KEY) or {}
    if digest(plan.get('document'))!=plan.get('digest'):raise ValueError('批准内容摘要不符')
    attempt=plan.get('attempts',{}).get(stage['id'],{})
    if (plan.get('status')!='approved' or goal['status']!=stages.GOAL_ACTIVE or
        proof['revision']!=plan.get('revision') or proof['digest']!=plan.get('digest') or
        attempt.get('token')!=proof['token'] or attempt.get('state')!='reserved'):
        raise ValueError('计划未批准、已暂停或原派发意图不匹配')
    definition=_definition(conn,goal)
    if definition!=plan['document']['definition'] or _context(definition)!=plan['document']['context']:
        raise ValueError('计划、执行端、模型、权限或基线已变化，不能派发')
    choice=plan['document']['context']['models'][stage['id']]
    if choice['model_id']!=model_id or choice['engine']!=engine:raise ValueError('实际模型/执行端与批准计划不符')
    responsibility=definition.get('responsibility',{'mode':'controller'})
    if responsibility['mode']=='brain':
        import brain_context
        if (brain is None or brain.owner!=responsibility['owner'] or brain.binding_key!=responsibility['binding_key'] or brain.host!=responsibility['host'] or
            not brain_context.authority_matches(authority,brain.owner,stage['project'],brain.binding_key)):
            raise ValueError('主脑计划需要原受信会话的控制器授权，普通面板不能代派')
    elif brain is not None:raise ValueError('不能自动接管本地控制器计划')
    if stage['task_ids'] or stage['active_task_id']:raise ValueError('此阶段已有原任务，批准意图不能用于再次领取')


def dispatch_request_id(stage_id,proof):
    return 'stage-'+digest([stage_id,proof['revision'],proof['digest'],proof['token']])[:40]


def _integration_step(goal,project,plan,spec,handovers,integrate):
    """One original fan-in intent; the queue alone runs merges/checks.

    A missing/lost reply is observed by its deterministic name, never resent.
    An existing queued job may be reattached through the original callback.
    """
    checks=plan['document']['integration_checks']
    if not checks or integrate is None or len(spec['depends_on'])<2:return 'waiting_integration'
    ids=[h.get('task_id') for h in handovers]
    if len(ids)!=len(spec['depends_on']) or any(not isinstance(i,str) or not i for i in ids):
        return 'integration_needs_attention'
    sid=spec['id'];old=plan.get('integrations',{}).get(sid)
    existed=old is not None
    if old and (old['task_ids']!=ids or old['checks']!=checks):return 'integration_needs_attention'
    import integration_queue as queue
    if old:
        job=queue.get(project,old['name'])
        if not job:return 'integration_unknown'
    else:
        request_id='plan-merge-'+digest([goal,sid,plan['revision'],plan['digest']])[:40]
        responsibility=plan['document']['definition'].get('responsibility',{})
        brain=responsibility.get('mode')=='brain'
        reference=(responsibility['binding_key']+'/' if brain else 'panel-integration/')+request_id
        name=('batch-' if brain else 'queue-')+hashlib.sha256(reference.encode()).hexdigest()[:24]
        old={'state':'reserved','request_id':request_id,'request_reference':reference,'name':name,
             'task_ids':ids,'checks':checks,'at':registry.now()}
        _mutate(project,goal,lambda p:p.setdefault('integrations',{}).update({sid:old}))
        try:integrate(ids,checks,request_id)
        except Exception:
            # An effect may already exist. Observe only; no second submission.
            _mutate(project,goal,lambda p:p['integrations'][sid].update(state='unknown'))
        job=queue.get(project,name)
        if not job:return 'integration_unknown'
    if (job.get('request_reference')!=old['request_reference'] or job.get('goal_id')!=goal or
        job.get('task_ids')!=ids or job.get('queue',{}).get('check_commands')!=checks):
        return 'integration_needs_attention'
    state=queue.state(job)
    # Reattach an original queued job, including after an explicit brain resume.
    # Never call back for preparing/running/unknown/terminal jobs.
    if state=='queued' and existed:
        try:integrate(ids,checks,old['request_id'])
        except Exception:return 'integration_unknown'
    _mutate(project,goal,lambda p:p['integrations'][sid].update(state=state))
    return ('integration_accepted' if state=='accepted' else
            'integration_awaiting_review' if state=='awaiting_review' else
            'integration_running' if state in ('queued','preparing','merging','checking') else
            'integration_needs_attention')


def advance(goal,project,dispatch,integrate=None):
    """One bounded pass. Persist intent before calling any external effect."""
    project=project_root(project)
    with locked(project,goal):
        stages.sync_from_tasks(goal,project=project)
        row,plan=_read(project,goal)
        if not plan or plan['status']!='approved' or row['status']!=stages.GOAL_ACTIVE:
            with _LOCK:_BINDINGS.pop((project,goal),None)
            return view(goal,project)
        document=plan['document']
        try:fresh=preview(goal,project,document['integration_checks'])
        except (ValueError,OSError):fresh=None
        if not fresh or fresh['digest']!=plan['digest']:
            _mutate(project,goal,lambda p:p.update(status='needs_approval'))
            with _LOCK:_BINDINGS.pop((project,goal),None)
            return view(goal,project)
        import workflow_settings
        settings=workflow_settings.snapshot()
        if settings.get('enabled') is not True:return view(goal,project)
        observations={}
        import task_handover
        for spec in document['definition']['stages']:
            sid=spec['id'];stage=stages.get_stage(sid,project)
            if stage['status'] in stages.ACCEPTED_STATES:observations[sid]='accepted';continue
            if sid in plan['attempts']:
                attempt=plan['attempts'][sid]
                observations[sid]=('awaiting_review' if stage['status']==stages.EXECUTOR_COMPLETED else
                    'manual_attention' if stage['status'] in (stages.FAILED,stages.CANCELLED,stages.RETURNED,stages.BLOCKED,stages.RECOVERY_PENDING) else
                    'unknown' if attempt['state'] in ('unknown','reserved') else
                    'queued' if stage['status']==stages.DISPATCHED else 'running' if stage['status']==stages.RUNNING else
                    'not_submitted' if attempt['state']=='not_submitted' else 'original_attempt_only')
                continue
            if stage['task_ids'] or stage['status'] not in (stages.READY,stages.PENDING):
                observations[sid]='manual_attention';continue
            if not stages.dispatch_precheck(sid,project=project)['ready']:
                observations[sid]='waiting_prerequisites';continue
            if spec['depends_on']:
                try:
                    handovers=task_handover.prerequisite_handovers(sid,project=project)
                except (ValueError,OSError):
                    observations[sid]='waiting_handover';continue
                try:task_handover.prerequisite_baseline(handovers,project)
                except task_handover.HandoverError:
                    state=_integration_step(goal,project,plan,spec,handovers,integrate)
                    observations[sid]=state if state!='integration_accepted' else 'integration_needs_attention'
                    continue
                except (ValueError,OSError):
                    # Ambiguous/corrupt baseline evidence is not a request to
                    # create another merge that might mask the original issue.
                    observations[sid]='integration_needs_attention'
                    continue
                # Once this plan reserved a merge, do not bypass an unknown,
                # failed or unreviewed original job through another baseline.
                if sid in plan.get('integrations',{}):
                    outcome=_integration_step(goal,project,plan,spec,handovers,integrate)
                    if outcome!='integration_accepted':observations[sid]=outcome;continue
            # Authoritative queue checks still decide capacity, budgets and
            # native readiness. An exception after reservation is unknown,
            # never a reason to try a new task/request ID.
            token=uuid.uuid4().hex
            proof={'revision':plan['revision'],'digest':plan['digest'],'token':token}
            intent={'token':token,'approval_revision':plan['revision'],'approval_digest':plan['digest'],'at':registry.now()}
            intent['request_id']=dispatch_request_id(sid,proof)
            _mutate(project,goal,lambda p:p['attempts'].update({sid:{**intent,'state':'reserved'}}))
            try:
                result=dispatch(stage,document['context']['models'][sid],proof)
                task=(result or {}).get('task') or {};task_id=task.get('id')
                if not isinstance(task_id,str) or not task_id:raise ValueError('缺少原任务回执')
                entry={**intent,'state':'created','task_id':task_id}
            except NotSubmitted:
                entry={**intent,'state':'not_submitted'}
            except Exception:
                entry={**intent,'state':'unknown'}
            _mutate(project,goal,lambda p:p['attempts'].update({sid:entry}))
            observations[sid]=entry['state']
        _mutate(project,goal,lambda p:p.update(observations=observations))
        if observations and all(v=='accepted' for v in observations.values()):
            _mutate(project,goal,lambda p:p.update(status='completed'))
            with _LOCK:_BINDINGS.pop((project,goal),None)
    return view(goal,project)


def attach(goal,project,submit,dispatch,integrate=None):
    key=(project_root(project),goal)
    with _LOCK:_BINDINGS[key]=(submit,dispatch,integrate)
    wake(*key)


def wake(project,goal=None):
    project=str(Path(project).resolve())
    with _LOCK:
        keys=[k for k in _BINDINGS if k[0]==project and (goal is None or k[1]==goal)]
        for key in keys:
            if key in _ACTIVE:_AGAIN.add(key);continue
            _ACTIVE.add(key)
            try:_BINDINGS[key][0](_pump,key)
            except Exception:_ACTIVE.discard(key)


def _pump(key):
    try:
        with _LOCK:binding=_BINDINGS.get(key)
        if binding:advance(key[1],key[0],binding[1],binding[2])
    except Exception:
        try:_mutate(key[0],key[1],lambda p:p.update(last_problem='推进检查未完成，请核对原阶段和派发意图；不会自动重发。'))
        except (OSError,ValueError):pass
    finally:
        with _LOCK:
            _ACTIVE.discard(key);again=key in _AGAIN;_AGAIN.discard(key)
        if again:wake(*key)


def notify_task(run):
    try:
        task=registry.read_task(run)
        if not task.get('project') or not task.get('goal_id'):return
        key=(str(Path(task['project']).resolve()),task['goal_id'])
        with _LOCK:local=key in _BINDINGS
        if local:wake(*key)
        else:_external_notify(task)
    except (OSError,ValueError):pass


def signal(goal,project,task_id):
    """Authenticated nudge of an existing live binding, never an authority grant."""
    import re
    project=project_root(project)
    if not isinstance(task_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',task_id):raise ValueError('需要原任务标识')
    row,plan=_read(project,goal)
    task=registry.read_task(registry.task_dir(task_id,project))
    # Do not use the legacy/global locator for an explicit project signal.
    with stages.connection(project) as conn:
        record=conn.execute('SELECT * FROM stages WHERE id=?',(task.get('stage_id'),)).fetchone()
        stage=stages._row_stage(record) if record else None
    if (task.get('goal_id')!=goal or Path(task.get('project','')).resolve()!=Path(project)
        or not stage or stage['goal_id']!=goal or task_id not in stage['task_ids']):
        raise ValueError('通知任务不属于原项目、目标和阶段')
    key=(project,goal)
    with _LOCK:attached=key in _BINDINGS
    allowed=bool(plan and plan.get('status')=='approved' and row['status']==stages.GOAL_ACTIVE)
    if allowed and attached:wake(*key)
    return {'signal_accepted':allowed and attached,'requires_resume':allowed and not attached,
            'goal_id':goal,'task_id':task_id,'note':'仅通知既有绑定重读事实，不批准、不附着、不重发原请求。'}


def _external_notify(task):
    """One best-effort CLI/worker signal; no retries, bootstrap or auth files.

    Only a persisted active approval permits contacting a matching installation.
    The existing panel session credential stays in memory on the same origin.
    """
    import base64
    import os
    import re
    import sqlite3
    import urllib.request
    import project_storage
    import brain_transport
    try:
        project=project_root(task.get('project'));goal=task.get('goal_id')
        if not isinstance(goal,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',goal):return False
        file=project_storage.records_db(project)
        if not file.is_file():return False
        conn=sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
        try:
            conn.execute('PRAGMA query_only=ON')
            row=conn.execute('SELECT status,data FROM goals WHERE id=?',(goal,)).fetchone()
            if not row or row[0]!=stages.GOAL_ACTIVE:return False
            plan=json.loads(row[1]).get(KEY) or {}
            if plan.get('status')!='approved' or digest(plan.get('document'))!=plan.get('digest'):return False
            if task.get('stage_id') not in {s['id'] for s in plan['document']['definition']['stages']}:return False
        finally:conn.close()
        url=brain_transport.normalize_base_url(os.environ.get('MODEL_DESK_PANEL_URL') or brain_transport.DEFAULT_BASE_URL)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),brain_transport._NoRedirect())
        def request(path,body=None,headers=None):
            req=urllib.request.Request(url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or {})
            with opener.open(req,timeout=1) as response:
                raw=response.read(65537)
                if len(raw)>65536:raise ValueError('oversized signal response')
                return json.loads(raw)
        ready=request('/api/ready')
        if (ready.get('service')!='model-desk' or ready.get('ready') is not True
            or type(ready.get('approved_plan_notifications')) is not int or ready['approved_plan_notifications']!=1
            or Path(ready.get('workspace','')).resolve()!=Path(__file__).resolve().parents[1]):return False
        credential=request('/api/session').get('token')
        if not isinstance(credential,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,256}',credential):return False
        header=base64.urlsafe_b64encode(json.dumps({'mode':'project','project':project}).encode()).decode()
        result=request('/api/goals/'+goal+'/plan/signal',{'task_id':task['id']},
            {'Content-Type':'application/json','X-Panel-Token':credential,'X-Model-Desk-View':header})
        return result.get('signal_accepted') is True
    except Exception:
        # Notification delivery cannot change an already persisted review,
        # handover or executor outcome. Explicit resume remains the fallback.
        return False
