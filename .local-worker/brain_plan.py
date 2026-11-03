"""Fixed plan operations for an immutable, signed main-brain context.

Drafting is not operator approval. Only the owning brain can attach an already
approved version; no operation here changes approval/permissions or adopts tasks.
"""
import json
import re
import sqlite3

import approved_plan as plans
import brain_context
import brain_requests
import model_registry as registry
import stage_store as stages

IDENTIFIER=re.compile(r'^[A-Za-z0-9_-]{1,120}$')
READ_ACTIONS=('status','preview')
WRITE_ACTIONS=('draft','prepare','resume','handover')


class PlanRefused(ValueError):
    """Validation completed before any plan operation effect."""


def identifier(value):
    if not isinstance(value,str) or not IDENTIFIER.fullmatch(value):raise ValueError('需要明确的目标或阶段标识')
    return value


def owner(context):
    brain_context.require_operation(context,'dispatch')
    return {'owner':context.owner,'binding_key':context.binding_key,'host':context.host,'ready':True}


def require_owner(context,goal,*,approved_only=False):
    identifier(goal);expected=owner(context)
    row,plan=plans._read(context.project,goal)
    actual=row['data'].get('plan_brain') or {}
    if any(actual.get(k)!=expected[k] for k in ('owner','binding_key','host')) or actual.get('ready') is not True:
        raise ValueError('计划不属于当前受信主脑；不接管其他聊天或本地控制器任务')
    if approved_only:
        if plan.get('status')!='approved':raise ValueError('计划尚未批准、已暂停、撤销或结束；主脑不能自行批准或解除暂停')
        responsibility=plan['document']['definition'].get('responsibility') or {}
        if responsibility!={'mode':'brain',**{k:expected[k] for k in ('owner','binding_key','host')}}:
            raise ValueError('批准版本没有授权当前主脑责任会话')
    return row,plan


def _keys(payload,allowed,required=()):
    if set(payload)-set(allowed) or any(k not in payload for k in required):raise ValueError('计划操作包含未知参数或缺少必填参数')


def _text(value,label,limit):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:raise ValueError(label+'需要非空文本且不超过'+str(limit)+'字符')
    return value.strip()


def draft_spec(payload):
    _keys(payload,('request_id','title','objective','stages','checks'),('request_id','title','objective','stages'))
    title=_text(payload['title'],'标题',200);objective=_text(payload['objective'],'目标',2000)
    raw=payload['stages']
    if not isinstance(raw,list) or not 1<=len(raw)<=20:raise ValueError('草案需要1到20个明确阶段')
    parsed={};order=[]
    for item in raw:
        if not isinstance(item,dict):raise ValueError('阶段需要对象')
        _keys(item,('key','title','objective','kind','model_id','depends_on','acceptance'),('key','title','objective','model_id','acceptance'))
        key=identifier(item['key'])
        if key in parsed:raise ValueError('阶段key重复')
        kind=item.get('kind','edit')
        if kind not in ('edit','chat'):raise ValueError('阶段类型只能为edit/chat')
        deps=item.get('depends_on',[]);criteria=item['acceptance']
        if not isinstance(deps,list) or len(deps)>20 or any(not isinstance(d,str) or not IDENTIFIER.fullmatch(d) for d in deps):raise ValueError('前置需要阶段key数组')
        if not isinstance(criteria,list) or not 1<=len(criteria)<=20:raise ValueError('每阶段需要1到20条验收条件')
        parsed[key]={'key':key,'title':_text(item['title'],'阶段标题',200),'objective':_text(item['objective'],'阶段目标',2000),
            'kind':kind,'model_id':_text(item['model_id'],'模型标识',80),'depends_on':list(dict.fromkeys(deps)),
            'acceptance':[_text(c,'验收条件',500) for c in criteria]}
    visiting=set();done=set()
    def visit(key):
        if key not in parsed:raise ValueError('前置阶段key不存在')
        if key in visiting:raise ValueError('草案依赖存在环路')
        if key in done:return
        visiting.add(key)
        for dep in parsed[key]['depends_on']:visit(dep)
        visiting.remove(key);done.add(key);order.append(parsed[key])
    for key in parsed:visit(key)
    result={'title':title,'objective':objective,'stages':order}
    if 'checks' in payload:
        import integration_queue
        if payload['checks']!=[]:integration_queue._checks(payload['checks'])
        elif not isinstance(payload['checks'],list):raise ValueError('检查计划需要数组')
        if payload['checks'] and any(len(s['depends_on'])>8 for s in order):raise ValueError('自动集成每组最多8个前置阶段')
        result['checks']=payload['checks']
    return result


def _record(goal,context,request_id,receipt,*,responsibility=None):
    with stages.connection(context.project) as conn,stages._transaction(conn):
        row=plans._goal(conn,goal);data=dict(row['data'])
        if responsibility is not None:data['plan_brain']=responsibility
        requests=dict(data.get('plan_requests') or {})
        requests[brain_requests.reference(context.binding_key,request_id)]=receipt
        data['plan_requests']=requests
        conn.execute('UPDATE goals SET data=?,updated_at=? WHERE id=?',
            (json.dumps(data,ensure_ascii=False),registry.now(),goal))


def recover(context,request_id):
    reference=brain_requests.reference(context.binding_key,request_id);matches=[];task_ids=[]
    import project_storage
    path=project_storage.records_db(context.project)
    if not path.is_file():return None
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='goals' AND type='table'").fetchone() is None:return None
        rows=conn.execute('SELECT id,data FROM goals').fetchall()
        for row in rows:
            data=json.loads(row['data']);identity=data.get('plan_brain') or {}
            if identity.get('owner')!=context.owner or identity.get('binding_key')!=context.binding_key:continue
            receipt=(data.get('plan_requests') or {}).get(reference)
            if receipt:matches.append(receipt)
            if not receipt:
                for stage in conn.execute('SELECT task_ids FROM stages WHERE goal_id=?',(row['id'],)):
                    task_ids.extend(json.loads(stage['task_ids']))
    finally:conn.close()
    if not matches:
        for task_id in dict.fromkeys(task_ids):
            found=_handover_receipt(context,request_id,task_id)
            if found:matches.append(found)
    return matches[0] if len(matches)==1 else None


def _handover_receipt(context,request_id,task_id):
    import brain_gateway
    import task_handover
    try:
        run,task=brain_gateway._task(context,task_id)
        handover=task_handover.load(run)
    except (ValueError,OSError):return None
    if not isinstance(handover,dict):return None
    reference=brain_requests.reference(context.binding_key,request_id)
    if (handover.get('request_reference')==reference and handover.get('task_id')==task_id
            and handover.get('goal_id')==task.get('goal_id') and handover.get('stage_id')==task.get('stage_id')):
        return _receipt(context,'handover',request_id,task['goal_id'],task_id=task_id,handover_created=True)
    return None


def _receipt(context,action,request_id,goal,**fields):
    return {'action':action,'goal_id':goal,'request_reference':brain_requests.reference(context.binding_key,request_id),**fields}


def _draft(context,spec,request_id):
    identity=owner(context)
    if 'checks' in spec:identity={**identity,'integration_checks':spec['checks']}
    goal=stages.create_goal(spec['title'],spec['objective'],context.project,actor='brain:'+context.owner,store='project')['id']
    # A partial draft is visible but cannot be approved. No tasks are submitted.
    with stages.connection(context.project) as conn,stages._transaction(conn):
        row=plans._goal(conn,goal);data=dict(row['data']);data['plan_brain']={**identity,'ready':False}
        conn.execute('UPDATE goals SET data=? WHERE id=?',(json.dumps(data),goal))
    ids={}
    for item in spec['stages']:
        stage=stages.add_stage(goal,item['title'],item['objective'],depends_on=[ids[k] for k in item['depends_on']],
            acceptance=item['acceptance'],fixed_model=item['model_id'],kind=item['kind'],project=context.project,
            files=[],actor='brain:'+context.owner)
        ids[item['key']]=stage['id']
    receipt=_receipt(context,'draft',request_id,goal,stage_ids=ids,draft_complete=True)
    _record(goal,context,request_id,receipt,responsibility=identity)
    return receipt


def _prepare(context,goal,request_id):
    with plans.locked(context.project,goal):
        row,plan=plans._read(context.project,goal)
        current=row['data'].get('plan_brain')
        if current and current.get('ready') is not True:raise PlanRefused('原草案尚未完整保存，按原请求核查，不能用prepare补造完成')
        if current and any(current.get(k)!=v for k,v in owner(context).items() if k!='ready'):
            raise PlanRefused('已有其他主脑责任声明，不能自动转移')
        if row['status']!=stages.GOAL_ACTIVE or plan.get('attempts') or plan.get('status') in ('approved','paused','completed'):
            raise PlanRefused('目标已有批准/派发历史，不能自动收养')
        if any(s['task_ids'] for s in stages.list_stages(goal,context.project)):
            raise PlanRefused('阶段已有原任务，不能更换主脑归属')
        receipt=_receipt(context,'prepare',request_id,goal)
        _record(goal,context,request_id,receipt,responsibility=owner(context))
        return receipt


def status(context,goal):
    identifier(goal)
    result=plans.view(goal,context.project);plan=result.get('plan')
    if plan:
        result['plan']={k:plan[k] for k in ('schema','status','revision','digest','approved_at','observations','last_problem') if k in plan}
        result['plan']['attempts']={sid:{k:v for k,v in attempt.items() if k!='token'} for sid,attempt in plan['attempts'].items()}
        result['plan']['integrations']=plan.get('integrations',{})
    result['stages']=[{k:s[k] for k in ('id','title','status','task_ids','active_task_id')} for s in stages.list_stages(goal,context.project)]
    return result


def _planned_dispatch(context,stage,choice,proof):
    import brain_gateway
    import panel_server as panel
    request_id=plans.dispatch_request_id(stage['id'],proof)
    require_owner(context,stage['goal_id'],approved_only=True)
    payload={'goal_id':stage['goal_id'],'stage_id':stage['id'],'model_id':choice['model_id'],
             'approval_revision':proof['revision'],'approval_digest':proof['digest'],'intent_id':proof['token']}
    def effect():
        try:
            row=panel.dispatch_stage(stage['id'],{
                'prompt':stage['objective']+'\n\n验收条件：\n'+'\n'.join(stage['acceptance']),
                'model_id':choice['model_id'],'route_mode':'auto','plan_dispatch':proof,'source':'brain'},
                project=context.project,trusted=context,authority=brain_context.issue_authority(context),request_id=request_id)
        except ValueError:
            current=stages.get_stage(stage['id'],context.project)
            if current['task_ids']==stage['task_ids'] and current['active_task_id']==stage['active_task_id']:
                raise panel.DispatchRefused('阶段派发前置检查拒绝，原子领取未发生') from None
            raise
        return brain_gateway._task_receipt(row['task'])
    try:
        row=brain_requests.run_once(context.project,context.binding_key,'dispatch',request_id,payload,effect,
            recover=lambda:brain_gateway._find_task_receipt(context,request_id),reject_on=(panel.DispatchRefused,))
    except brain_requests.RequestRejected as error:raise plans.NotSubmitted(str(error)) from error
    receipt=row.get('receipt') or {}
    return {'task':{'id':receipt.get('task_id')},'request_id':request_id}


def _attach(context,goal,expected):
    import panel_server as panel
    with plans.locked(context.project,goal):
        _,plan=require_owner(context,goal,approved_only=True)
        if plan['digest']!=expected:raise ValueError('原批准版本已变化，不续接其他版本')
        fresh=plans.preview(goal,context.project)
        if fresh['digest']!=expected:raise ValueError('模型/权限/基线已变化，需用户重新批准')
        import brain_gateway
        plans.attach(goal,context.project,panel.EXECUTOR.submit,
            lambda stage,choice,proof:_planned_dispatch(context,stage,choice,proof),
            lambda ids,checks,request_id:brain_gateway._integrate(context,
                {'task_ids':ids,'checks':checks,'request_id':request_id,'enqueue':True}))


def execute(context,action,payload):
    if context.role!='brain':raise ValueError('只有主脑可以读取或操作计划')
    if action not in READ_ACTIONS+WRITE_ACTIONS:raise ValueError('未知计划动作')
    if action in READ_ACTIONS:
        _keys(payload,('goal_id','checks') if action=='preview' else ('goal_id',),('goal_id',));goal=identifier(payload['goal_id'])
        result=status(context,goal) if action=='status' else plans.preview(goal,context.project,payload.get('checks'))
        return {'operation':'plan_'+action,'goal_id':goal,'result':result,'read_only':True}
    owner(context)
    request_id=brain_context.clean_request_id(payload.get('request_id'))
    if action=='draft':spec=draft_spec(payload);journal={'action':action,**spec}
    elif action in ('prepare','resume'):
        allowed=('request_id','goal_id')+(('digest',) if action=='resume' else ())
        _keys(payload,allowed,allowed);goal=identifier(payload['goal_id']);journal={'action':action,**{k:v for k,v in payload.items() if k!='request_id'}}
        if action=='resume':
            _,plan=require_owner(context,goal,approved_only=True)
            if plan['digest']!=payload['digest']:raise ValueError('需要原批准版本摘要')
    else:
        _keys(payload,('request_id','task_id','summary','interfaces','decisions','remaining_issues'),('request_id','task_id'))
        import brain_gateway
        run,task=brain_gateway._task(context,payload['task_id'])
        goal=task.get('goal_id');require_owner(context,goal)
        import task_finalization
        import delegation_policy
        task_finalization.require_settled(task)
        review=delegation_policy.load(run)
        if review is None or delegation_policy.acceptance_problems(review):
            raise ValueError('交接必须在原任务独立验收通过后创建；未写入或保留变更请求')
        delegation_policy.require_result_binding(task,review)
        if 'summary' in payload and (not isinstance(payload['summary'],str) or len(payload['summary'])>2000):raise ValueError('交接摘要需要最多2000字符文本')
        for key in ('interfaces','decisions','remaining_issues'):
            if key in payload and (not isinstance(payload[key],list) or len(payload[key])>40
                    or any(not isinstance(v,str) or not v.strip() or len(v)>500 for v in payload[key])):
                raise ValueError('交接说明需要最多40条非空文本')
        journal={'action':action,**{k:v for k,v in payload.items() if k!='request_id'}}
    def effect():
        if action=='draft':return _draft(context,spec,request_id)
        if action=='prepare':return _prepare(context,goal,request_id)
        if action=='resume':
            with plans.locked(context.project,goal):
                _,plan=require_owner(context,goal,approved_only=True)
                if plan['digest']!=payload['digest']:raise ValueError('原批准版本已变化')
                receipt=_receipt(context,action,request_id,goal,revision=plan['revision'],digest=plan['digest'],continuation_requested=True)
                _record(goal,context,request_id,receipt)
                return receipt
        import task_handover
        review=delegation_policy.load(run) or {}
        evidence=[entry for criterion in review.get('criteria',[]) for entry in criterion.get('evidence',[])]
        task_handover.create(run,summary=payload.get('summary',''),interfaces=payload.get('interfaces',()),
            decisions=payload.get('decisions',()),remaining_issues=payload.get('remaining_issues',()),actor='brain:'+context.owner,
            test_evidence=evidence,
            request_reference=brain_requests.reference(context.binding_key,request_id))
        receipt=_receipt(context,action,request_id,goal,task_id=task['id'],handover_created=True)
        _record(goal,context,request_id,receipt)
        return receipt
    row=brain_requests.run_once(context.project,context.binding_key,'plan',request_id,journal,effect,
                               recover=lambda:recover(context,request_id),reject_on=(PlanRefused,))
    receipt=row.get('receipt') or {}
    if action=='resume':_attach(context,goal,payload['digest'])
    return {'operation':'plan_'+action,'request_id':request_id,'owner':context.owner,'project':context.project,
            'reused':bool(row.get('reused')),'receipt':receipt}
