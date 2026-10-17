"""Project-scoped permanent history deletion with preview, dependency guards and crash recovery."""
from contextlib import contextmanager, ExitStack
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import time

import model_registry as registry
import project_scope as scope
import project_storage
import project_views
import stage_store

LOCK = threading.RLock()
PREVIEWS = {}
TERMINAL = {'completed', 'failed', 'cancelled'}
ID = re.compile(r'[A-Za-z0-9_-]{1,80}')


def serialized(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        with LOCK:
            return function(*args, **kwargs)
    return wrapper


def require_project(view):
    view = scope.parse_view(view)
    if view['mode'] != 'project':
        raise ValueError('请先选择具体项目，再管理任务记录')
    return view


def records(view=None):
    """Live task records, optionally limited to one explicit project's store.

    A project request enumerates the real directories itself, so the global
    locator is not a precondition for listing or cleaning that project's history.
    """
    out = {}
    for entry in project_storage.iter_task_dirs(view):
        folder = entry['run']
        if not ID.fullmatch(folder.name):
            continue
        file = folder / 'task.json'
        if not file.exists():
            if (folder / 'result.json').exists():
                row = registry.read_json(folder / 'result.json')
                out[folder.name] = {'id':folder.name, 'project':row.get('project'),
                    'status':'completed' if row.get('exit_code') == 0 else 'failed',
                    'title':'旧版任务 '+folder.name, '_legacy':True}
            continue
        row = registry.read_task(folder)
        if not isinstance(row, dict) or row.get('id', folder.name) != folder.name:
            raise ValueError('任务记录不可读或身份不一致，无法安全清理')
        out[folder.name] = {**row, 'id':folder.name}
    return out


def record_roots(view=None):
    """Live task id -> the runs root that owns it (project store or legacy)."""
    return {entry['task_id']: entry['root'] for entry in project_storage.iter_task_dirs(view)}


def analysis_rows(view=None):
    """Every live record for the safety analysis, plus the view's own records.

    The extra union keeps a project request readable when the global locator is
    missing: a record physically inside the requested project is still analysed
    even though the aggregate enumeration cannot see that store without an index.
    """
    rows = records()
    if view is not None:
        for key, row in records(view).items():
            rows.setdefault(key, row)
    return rows


def _connections(project):
    """Open every store that can hold this project's goals/stages.

    A project keeps pre-migration rows in the legacy global DB and newer rows in
    its own records.db; deletion/listing must see both without duplicating ids.
    """
    stack = ExitStack()
    try:
        conns = [stack.enter_context(stage_store.connection(project)),
                 stack.enter_context(stage_store.connection())]
    except Exception:
        stack.close()
        raise
    return stack, conns


def database_rows_multi(conns):
    goals, stages = {}, {}
    for conn in conns:
        conn_goals, conn_stages = database_rows(conn)
        for key, value in conn_goals.items():
            goals.setdefault(key, value)
        for key, value in conn_stages.items():
            stages.setdefault(key, value)
    return goals, stages


def database_rows(conn):
    goals = {row['id']:dict(row) for row in conn.execute('SELECT * FROM goals')}
    stages = {row['id']:stage_store._row_stage(row) for row in conn.execute('SELECT * FROM stages')}
    return goals, stages


def references(row):
    result = set()
    for key in ('continue_task','continued_from','source_task','source_task_id','rework_of','parent_task_id'):
        if isinstance(row.get(key), str) and row[key]: result.add(row[key])
    for nested in ('delegation','route'):
        if isinstance(row.get(nested),dict): result.update(references(row[nested]))
    return result


def stage_links(rows, stages):
    links = {key:set() for key in rows}
    for stage in stages.values():
        for key in [*stage['task_ids'], stage.get('active_task_id')]:
            if key in links: links[key].add(stage['goal_id'])
    for key, row in rows.items():
        if row.get('goal_id'): links[key].add(row['goal_id'])
        if row.get('stage_id'):
            stage = stages.get(row['stage_id'])
            links[key].add(stage['goal_id'] if stage else '__missing_goal__')
    return links


def safe_directory(path, parent):
    path, parent = Path(path), Path(parent).resolve()
    if path.is_symlink() or os.path.isjunction(path) or not path.resolve().is_relative_to(parent) or path.resolve() == parent:
        raise ValueError('记录目录为外部链接或越界路径，拒绝删除')
    # Do not follow a reparse point anywhere inside a run, even during cleanup.
    def visit(directory):
        for entry in os.scandir(directory):
            if entry.is_symlink() or os.path.isjunction(entry.path):
                raise ValueError('记录中包含链接目录，拒绝递归删除')
            if entry.is_dir(follow_symlinks=False): visit(entry.path)
    if path.exists(): visit(path)
    return path


def candidate_reason(key, rows, links, goals, all_projects, roots=None):
    row = rows[key]
    if row.get('status') not in TERMINAL:
        return '运行中、排队或状态未知的任务不能删除'
    for goal_id in links[key]:
        goal = goals.get(goal_id)
        if not goal or goal['status'] not in {'completed','cancelled'}:
            return '关联目标仍在进行或身份缺失，需保留阶段交接'
        if scope.project_key(goal['project']) != scope.project_key(row.get('project')):
            return '关联目标项目不一致，拒绝删除'
    # The owning runs root comes from the real enumeration (or the locator when
    # no scope was supplied), so the path guard never has to guess.
    root = (roots or {}).get(key) or project_storage.owning_root(key)
    run = Path(root) / key
    try:
        safe_directory(run, root)
        for project in all_projects:
            if Path(project).resolve().is_relative_to(run.resolve()):
                return '记录目录包含项目文件，不能整体删除'
    except (ValueError,OSError) as exc:
        return str(exc)
    return ''


def plan(view, requested, conn, _snapshot=None, _fingerprint=True):
    view = require_project(view)
    if not isinstance(requested,list) or not requested or len(requested)>2000 or any(not isinstance(key,str) or not ID.fullmatch(key) for key in requested):
        raise ValueError('请选择有效任务记录，单次最多2000条')
    roots=record_roots(view)
    if _snapshot is None:
        # Preview/commit always analyse the *global* record set for surviving
        # references and nested project folders: a cross-project reference or a
        # project directory living inside a run makes the deletion unsafe even
        # when the target itself is inside the current view.  The view's own
        # records are added so a missing locator cannot hide a real target.
        rows=analysis_rows(view);goals,stages=database_rows_multi(conn)
    else:rows,goals,stages=_snapshot
    links = stage_links(rows,stages)
    selected=set(requested)
    for key in selected:
        if key not in rows or not scope.belongs_to_project(rows[key],view):
            raise ValueError('任务不存在或不属于当前项目')
    # A finished goal's handovers/attempts are one dependency group. Never silently
    # delete just its accepted prerequisite while leaving downstream stages behind.
    chosen_goals=set()
    while True:
        before=len(selected)
        chosen_goals.update(g for key in selected for g in links[key])
        for key,row in rows.items():
            if links[key] & chosen_goals: selected.add(key)
            elif references(row) & selected: selected.add(key)
        if len(selected)==before: break
    projects={str(Path(project).resolve()) for project in
              {row.get('project') or row.get('path') for row in
               [*rows.values(),*goals.values(),*project_views.project_bookmarks()]
               if row.get('project') or row.get('path')}}
    for key in selected:
        if not scope.belongs_to_project(rows[key],view):
            raise ValueError('关联记录跨项目，不能联动删除')
        reason=candidate_reason(key,rows,links,goals,projects,roots)
        if reason: raise ValueError(f'「{rows[key].get("title",key)}」：{reason}')
    for goal_id in chosen_goals:
        goal=goals.get(goal_id)
        if not goal or goal['status'] not in {'completed','cancelled'}: raise ValueError('关联目标尚未结束')
        if not scope.belongs_to_project(goal,view): raise ValueError('关联目标跨项目')
    chosen_stages=[row for row in stages.values() if row['goal_id'] in chosen_goals]
    if any(row['status'] in {'dispatched','running','recovery_pending'} for row in chosen_stages):
        raise ValueError('关联阶段尚未停止或待恢复，不能删除')
    # Preview fingerprint includes all reference/status metadata and targeted files.
    digest=hashlib.sha256()
    if _fingerprint:
        digest.update(json.dumps({'rows':rows,'goals':goals,'stages':stages},sort_keys=True,ensure_ascii=False).encode())
        for key in sorted(selected):
            run=Path(roots.get(key) or project_storage.owning_root(key))/key
            for path in sorted(run.rglob('*')):
                if path.is_file():
                    info=path.stat();digest.update(f'{key}/{path.relative_to(run)}:{info.st_size}:{info.st_mtime_ns}'.encode())
    return {'project':view['project'],'tasks':[{'id':key,'title':rows[key].get('title',key),'status':rows[key]['status']} for key in sorted(selected)],
            'requested_ids':sorted(set(requested)),'added_ids':sorted(selected-set(requested)),
            'goals':[{'id':key,'title':goals[key]['title']} for key in sorted(chosen_goals)],
            'stage_ids':sorted(row['id'] for row in chosen_stages),'stage_count':len(chosen_stages),'_fingerprint':digest.hexdigest(),
            'warnings':['永久删除工作台记录、提示词、结果、日志、验收和交接，无法撤销',
                        '关联的已结束目标/阶段及后续引用记录将一并删除，请检查最终清单',
                        '保留项目文件、模型配置和已发生费用总账；DSH自身保存的会话不在此次清理范围']}


@serialized
def listing(view):
    view=require_project(view)
    stack,conns=_connections(view['project'])
    try:
        # Displayed rows come from the project's own directories; the safety
        # analysis still sees every live record, so the ``deletable`` hint can
        # never claim a record is safe when a cross-project reference says not.
        rows=records(view);safety=analysis_rows(view);goals,stages=database_rows_multi(conns)
        result=[]
        for key,row in rows.items():
            try:
                evaluated=plan(view,[key],conns,_snapshot=(safety,goals,stages),_fingerprint=False); reason=''
                group_title='、'.join(g['title'] for g in evaluated['goals'])
            except ValueError as exc:reason=str(exc);group_title=''
            result.append({k:row.get(k,'') for k in ('id','title','status','created_at','ended_at','model_name','goal_id')}
                          | {'deletable':not reason,'blocked_reason':reason,'group_title':group_title})
    finally:
        stack.close()
    result.sort(key=lambda row:row['created_at'],reverse=True)
    return {'project':view['project'],'tasks':result,'total':len(result)}


@serialized
def preview(view, ids):
    view=require_project(view)
    stack,conns=_connections(view['project'])
    try:
        result=plan(view,ids,conns)
    finally:
        stack.close()
    now=time.monotonic()
    for key in list(PREVIEWS):
        if now-PREVIEWS[key]['_created']>300: PREVIEWS.pop(key)
    token=secrets.token_urlsafe(32)
    PREVIEWS[token]={**result,'_created':now}
    return {k:v for k,v in result.items() if not k.startswith('_') and k!='stage_ids'} | {'token':token,'expires_in':300}


def staging_root():
    root=Path(registry.HERE)/'task-deletions'
    if root.is_symlink() or os.path.isjunction(root):raise ValueError('清理暂存目录为链接，拒绝使用')
    return root


def journal_schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS task_deletion_log (id TEXT PRIMARY KEY,project TEXT NOT NULL,task_count INTEGER,goal_count INTEGER,stage_count INTEGER,phase TEXT NOT NULL,at TEXT NOT NULL)')


def purge(directory):
    safe_directory(directory,staging_root())
    shutil.rmtree(directory)


def restore(directory,ids,roots=None):
    roots = roots if isinstance(roots,dict) else {}
    for key in ids:
        if not isinstance(key,str) or not ID.fullmatch(key):raise ValueError('清理恢复清单无效')
        source=directory/key
        if source.exists():
            safe_directory(source,directory)
            parent=Path(roots[key]) if roots.get(key) else project_storage.owning_root(key)
            destination=parent/key
            if destination.exists():raise ValueError('任务目录已存在，无法恢复清理事务')
            destination.parent.mkdir(parents=True,exist_ok=True)
            os.rename(source,destination)
            project=registry.project_of_run(destination)
            if project:
                # The aborted transaction never committed the index cleanup, but
                # re-register defensively so a restored project record is findable.
                try:project_storage.register('task',key,project)
                except (OSError,ValueError):pass
    purge(directory)


@serialized
def recover():
    root=staging_root()
    if not root.exists():return
    with stage_store.connection() as conn:
        journal_schema(conn)
        for directory in root.iterdir():
            if not re.fullmatch(r'[0-9a-f]{32}',directory.name):continue
            safe_directory(directory,root)
            entry=conn.execute('SELECT phase FROM task_deletion_log WHERE id=?',(directory.name,)).fetchone()
            if entry:
                purge(directory)
                conn.execute('UPDATE task_deletion_log SET phase="purged" WHERE id=?',(directory.name,))
            elif not (directory/'manifest.json').exists() and all(
                    item.is_file() and item.name.startswith('manifest.json.') and item.name.endswith('.tmp')
                    for item in directory.iterdir()):
                # Crash before write_json finished: no run could have moved yet.
                purge(directory)
            else:
                manifest=registry.read_json(directory/'manifest.json')
                restore(directory,manifest['ids'],manifest.get('roots'))


@serialized
def delete(view,token,idle_guard=None):
    view=require_project(view)
    selected=PREVIEWS.get(token) if isinstance(token,str) else None
    if not selected or time.monotonic()-selected['_created']>300:
        raise ValueError('删除预览已过期，请重新选择并预览')
    if scope.project_key(selected['project'])!=scope.project_key(view['project']):
        raise ValueError('项目已切换，请重新预览删除清单')
    recover()
    stack,conns=_connections(view['project'])
    journal_conn=conns[-1]
    try:
        journal_schema(journal_conn)
        with stage_store._transaction(journal_conn):
            current=plan(view,selected['requested_ids'],conns)
            if current['_fingerprint']!=selected['_fingerprint']:
                raise ValueError('记录或依赖发生变化，请重新预览')
            if any(row.get('status') not in TERMINAL for row in records().values()):
                raise ValueError('请等待工作台执行队列空闲后再永久删除记录')
            if idle_guard is not None:idle_guard()
            ids=[row['id'] for row in current['tasks']]
            roots={key:str(root) for key,root in record_roots(view).items() if key in set(ids)}
            job_id=secrets.token_hex(16);directory=staging_root()/job_id
            directory.mkdir(parents=True)
            registry.write_json(directory/'manifest.json',{'ids':ids,'roots':roots})
            try:
                for key in ids:
                    source=safe_directory(Path(roots[key])/key,Path(roots[key]))
                    os.rename(source,directory/key)
                goal_ids=[goal['id'] for goal in current['goals']]
                for conn in conns:
                    for goal_id in goal_ids:
                        conn.execute('DELETE FROM stage_log WHERE goal_id=?',(goal_id,))
                        conn.execute('DELETE FROM stages WHERE goal_id=?',(goal_id,))
                        conn.execute('DELETE FROM goals WHERE id=?',(goal_id,))
                    for key in ids:conn.execute('DELETE FROM stage_log WHERE task_id=?',(key,))
                journal_conn.execute('INSERT INTO task_deletion_log VALUES (?,?,?,?,?,?,?)',
                    (job_id,view['project'],len(ids),len(goal_ids),current['stage_count'],'committed',registry.now()))
                for key in ids:
                    project_storage.forget_task(key)
            except Exception:
                restore(directory,ids,roots)
                raise
    finally:
        stack.close()
    # DB commit precedes irreversible erase. Restart recovery completes a committed
    # purge, or restores uncommitted renamed directories; no user-facing recycle bin.
    PREVIEWS.pop(token,None)
    try:
        purge(directory)
        with stage_store.connection() as conn:
            conn.execute('UPDATE task_deletion_log SET phase="purged" WHERE id=?',(job_id,))
        pending=False
    except OSError:pending=True
    return {'deleted_tasks':len(ids),'deleted_goals':len(goal_ids),'deleted_stages':current['stage_count'],
            'cleanup_pending':pending,'note':'删除已提交；暂存物理清理待恢复' if pending else '记录已永久删除，费用总账保留'}
