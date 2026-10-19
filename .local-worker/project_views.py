"""Request-local project views. Global execution and accounting stay global."""
from collections import Counter
import re

import model_registry as registry
import project_storage
import stage_store
import delegation_policy
import project_scope as scope

HEADER = 'X-Model-Desk-View'

def scope_of(view):
    """('project', path) | ('all', None) | ('unbound', None).

    ``view=None`` means an internal/global call and keeps the historical
    aggregate; it never becomes a silent project fallback.  An explicit project
    view stays exactly that one directory.
    """
    if view is None:
        return scope.MODE_ALL,None
    parsed=scope.parse_view(view)
    return parsed['mode'],parsed['project']

def explicit_project(view):
    """The one explicit project a request is scoped to, or None."""
    mode,project=scope_of(view)
    return project if mode==scope.MODE_PROJECT else None

def require_record(record, view):
    if view is not None and not scope.belongs_to_project(record or {}, view):
        raise PermissionError('该记录不属于当前项目；请明确切换项目或选择全部项目')

def task_record(task_id, project=None):
    try:
        return registry.read_task(registry.task_dir(task_id, project))
    except FileNotFoundError:
        return {}  # the detail handler owns the explicit all-view 404

# Request views use the same plural segment names as the API paths.
_KIND = {'tasks': 'task', 'goals': 'goal', 'stages': 'stage'}


def reassert_owner(kind, record_id, project):
    """Restore one locator entry for a record proven to be in this project.

    The global index is derived data, not the authority.  When an explicit
    project request has really opened the record from that project's own store
    (never from the legacy fallback), the mapping is proven and may be restored
    so later by-id operations keep resolving.  A record that is not physically
    in this store, or a different owner, is left untouched.
    """
    if not project:
        return False
    singular = _KIND.get(kind, kind)
    try:
        if singular == 'task':
            owned = project_storage.task_dir(project, record_id).is_dir()
        else:
            owned = stage_store.record_store(singular, record_id, project) == project_storage.normalize(project)
    except (ValueError, OSError, stage_store.StageError):
        return False
    if not owned:
        return False
    try:
        return project_storage.assert_owner(singular, record_id, project)
    except (ValueError, OSError):
        return False


def check_request(path, view, body=None):
    """Validate the actual record identity before any read or mutation."""
    if view is None:
        return
    project=explicit_project(view)
    match = re.fullmatch(r'/api/(tasks|goals|stages)/([A-Za-z0-9_-]+)(?:/[^/]*)?', path)
    loaders = {'tasks':task_record, 'goals':stage_store.get_goal, 'stages':stage_store.get_stage}
    if match:
        if view['mode']==scope.MODE_UNBOUND:
            raise PermissionError('尚未绑定项目；请明确选择项目或全部项目')
        kind, record_id = match.groups()
        require_record(loaders[kind](record_id, project),view)
        reassert_owner(kind, record_id, project)
    if not isinstance(body,dict):
        return
    for key, kind in [('continue_task','tasks'),('rework_of','tasks'),('source_task','tasks'),
                      ('goal_id','goals'),('stage_id','stages'),('source_stage','stages'),('consumer_stage','stages')]:
        if body.get(key):
            require_record(loaders[kind](body[key], project),view)
            reassert_owner(kind, body[key], project)
    if path=='/api/stages':
        # A project view supplies the goal's project as the stage's project, so
        # the stage store does not need the global locator to find the goal.
        if not body.get('project') and view['mode']==scope.MODE_PROJECT:
            body['project']=view['project']
        if body.get('project'):
            goal=stage_store.get_goal(body.get('goal_id'), project) or {}
            if scope.project_key(body['project'])!=scope.project_key(goal.get('project')):
                raise PermissionError('新阶段必须使用目标所属项目')
    if path in {'/api/tasks','/api/goals','/api/route-preview'}:
        if view['mode']=='unbound':
            raise PermissionError('尚未绑定项目，请先选择明确的项目目录')
        # Continuations inherit the original project's identity in submit_task.
        if path=='/api/tasks' and (body.get('continue_task') or body.get('rework_of')):
            source=task_record(body.get('continue_task') or body.get('rework_of'), project)
            if body.get('project') and scope.project_key(body['project'])!=scope.project_key(source['project']):
                raise PermissionError('续接不能更换原任务项目')
            return
        if not body.get('project') and view['mode']=='project':
            body['project']=view['project']
        project=scope.normalize_project(body.get('project'))
        require_record({'project':project},view)
        body['project']=project

def project_stats(view):
    mode,project=scope_of(view)
    if mode==scope.MODE_UNBOUND:
        goals,stages=[],[]
    else:
        goals=scope.filter_records(stage_store.list_goals(project),view)
        stages=scope.filter_records(stage_store.list_stages(project=project),view)
    counts=dict(Counter(row['status'] for row in stages))
    return {'goals':len(goals),'stages':len(stages),'by_status':counts,
            'ready':counts.get('ready',0),
            'accepted':sum(counts.get(key,0) for key in delegation_policy.ACCEPTED_STATES),
            'recovery_pending':counts.get('recovery_pending',0),'note':stage_store.STAGES_NOTE}

def recovery_rows(rows, view):
    if view is None:
        return rows
    project=explicit_project(view)
    return [row for row in rows if scope.belongs_to_project(stage_store.get_stage(row['stage_id'], project) or {},view)]

def review_stats(tasks):
    states=[(row.get('review') or {}).get('state') for row in tasks]
    result={key:states.count(key) for key in ['dispatched','executor_completed','executor_failed',
             'accepted','final_accepted','returned','blocked']}
    result.update(tasks=len(tasks),delegated=sum(bool(row.get('delegation')) for row in tasks),
                  model_calls=sum(int(row.get('model_calls') or 0) for row in tasks),
                  note=delegation_policy.DELEGATION_NOTE)
    return result

def project_catalog(tasks):
    rows=list(tasks)+stage_store.list_goals()+stage_store.list_stages()
    found={}
    for row in rows:
        try:
            project=scope.normalize_project(row.get('project'))
            found[scope.project_key(project)]={'path':project,'label':project}
        except (ValueError, OSError, TypeError):
            continue
    return sorted(found.values(),key=lambda row:row['path'].casefold())

def project_bookmarks():
    path=registry.HERE/'project-bookmarks.json'
    if not path.exists():return []
    rows=registry.read_json(path).get('projects',[])
    result=[]
    for row in rows if isinstance(rows,list) else []:
        try:
            project=scope.normalize_project(row.get('path'))
            result.append({'path':project,'last_selected':row.get('last_selected')})
        except (ValueError,OSError,TypeError):continue
    return result[:8]

def remember_project(view):
    view=scope.parse_view(view)
    if view['mode']!='project':raise ValueError('只能记忆明确绑定的项目目录')
    with registry.file_lock(registry.HERE/'project-bookmarks.lock'):
        rows=[row for row in project_bookmarks() if scope.project_key(row['path'])!=scope.project_key(view['project'])]
        rows=[{'path':view['project'],'last_selected':registry.now()},*rows][:8]
        registry.write_json(registry.HERE/'project-bookmarks.json',{'projects':rows})
    return {'projects':rows}

def project_response(path,data,view):
    """Shape scoped aggregates; no recursive rewrites of arbitrary task content."""
    if view is None or not isinstance(data,dict) or data.get('error'):
        return data
    if path.startswith('/api/brain/') or path in {
            '/api/session','/api/workflow','/api/codex-usage',
            '/api/host-account','/api/host-account/snapshot'}:
        return data  # global/controller protocols retain their exact shape
    data={**data,'view':view}
    stats=project_stats(view) if path in {'/api/state','/api/tree','/api/goals','/api/stages','/api/recovery'} else None
    if path=='/api/state':
        activity=data.get('execution_activity')
        if isinstance(activity,dict) and view['mode']!=scope.MODE_ALL:
            active=activity.get('tasks') or []
            data['execution_activity']={**activity,
                'running':sum(row.get('status')=='running' for row in active),
                'model_counts':dict(Counter(row.get('model_id') for row in active if row.get('model_id'))),
                'running_models':sorted({row.get('model_id') for row in active if row.get('model_id') and row.get('status')=='running'}),
                'tasks':scope.filter_records(active,view), 'details_scope':view['mode']}
        data['delegation']=review_stats(data['tasks'])
        data['stages']={'stats':stats,'recovery':recovery_rows(stage_store.pending_recovery(explicit_project(view)),view)}
        data['workflow']={**data['workflow'],'daily_scope':'global'}
        data['current_project']=view['project'] if view['mode']=='project' else None
    elif path in {'/api/tree','/api/goals'}:
        data['goals']=scope.filter_records(data.get('goals',[]),view)
        data['stats']=stats
        data['recovery']=recovery_rows(data.get('recovery',[]),view)
    elif path=='/api/stages':
        data['stages']=scope.filter_records(data.get('stages',[]),view)
        data['stats']=stats
    elif path=='/api/recovery':
        for key in ('recovery','stages','pending'):
            if isinstance(data.get(key),list):
                data[key]=recovery_rows(data[key],view)
        if 'goals' in data:
            data['goals']=scope.filter_records(data['goals'],view)
        data['stats']=stats
    elif path=='/api/reviews':
        project=explicit_project(view)
        data['reviews']=[r for r in data.get('reviews',[]) if scope.belongs_to_project(task_record(r['task_id'],project),view)]
        data['stats']={'shown':len(data['reviews']),'scope':view['mode']}
    elif path in {'/api/db/space','/api/db/compact'}:
        # These paths act on real databases, so the scope is made explicit in the
        # response instead of being silently global.  ``unbound`` may read the
        # legacy store but never compacts anything.
        data['scope']=view['mode']
        data['scoped_project']=view['project'] if view['mode']=='project' else None
    return data
