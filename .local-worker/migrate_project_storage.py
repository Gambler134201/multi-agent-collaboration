"""Migrate legacy global workbench records into real per-project stores.

Modes (all local, no model call, no network):

    --mode preview    report what would migrate, what is unassigned/conflicting
    --mode dry-run    full validation and plan, but write nothing
    --mode apply      copy legacy records into <project>/.model-desk (originals kept)
    --mode verify     compare every migrated record against its legacy original
    --mode rollback   undo one apply from its journal, never touching originals

Safety rules enforced here:

* Only a real ``project`` field that resolves to an existing local directory is
  migrated.  A missing, invalid or conflicting owner stays in the legacy store
  and is reported; the installation directory is never assumed as a project.
* ``apply`` refuses while any task or stage is active, and refuses when a live
  worker process references a workbench runs root (metadata + process evidence).
* Legacy originals are copied, never moved or deleted, so recovery never depends
  on this tool.  The plan's own backup directory additionally records a manifest
  and a copy of the legacy stage database.
* ``task.json`` in the destination is the single authority; ``records.db.tasks``
  is a derived association row.  Internal absolute paths are rewritten only in
  the copy.

Run with the workspace venv Python:

    .local-worker\\venv\\Scripts\\python.exe .local-worker\\migrate_project_storage.py --mode preview
"""
import argparse
import hashlib
import json
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
from uuid import uuid4

import model_registry as registry
import project_storage
import stage_store

TASK_ID = re.compile(r'[A-Za-z0-9_-]{1,80}')
MIGRATION_MARKER = 'legacy_origin'
MANIFEST_NAME = 'manifest.json'
JOURNAL_NAME = 'journal.json'
STAGING_TOKEN = '.migrating-'


class MigrationError(ValueError):
    """A user-facing refusal/migration failure."""


# ------------------------------------------------------------------ discovery

def legacy_runs():
    return Path(registry.RUNS)


def legacy_db():
    return Path(registry.HERE) / stage_store.DB_NAME


def backups_root():
    return Path(registry.HERE) / 'project-migration-backups'


def _read_task(path):
    path = Path(path)
    if path.name == 'task.json' and not path.is_file() and (path.parent / 'result.json').is_file():
        result = _read_task(path.parent / 'result.json')
        if result is None:
            return None
        task_text = path.parent / 'task.txt'
        lines = task_text.read_text(encoding='utf-8').splitlines() if task_text.is_file() else []
        stamp = datetime.fromtimestamp(path.parent.stat().st_mtime).astimezone().isoformat()
        # Match the pre-migration panel's legacy projection; retain the original
        # result.json as an exact artifact and preserve its additional fields.
        return {**result, 'id':path.parent.name, 'title':lines[0][:80] if lines else path.parent.name,
                'kind':'edit', 'source':'codex', 'model_id':'home', 'model_name':'家里的 Qwen',
                'status':'completed' if result.get('exit_code') == 0 else 'failed',
                'phase':'历史任务', 'project':result.get('project',''),
                'changed_files':result.get('changed_selected_files',[]),
                'summary':result.get('log_summary',result.get('log_tail',''))[-1800:],
                'created_at':stamp, 'ended_at':stamp}
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _inside(candidate, parent):
    """True when ``candidate`` really lives inside ``parent`` (both resolved)."""
    try:
        return Path(candidate).resolve().is_relative_to(Path(parent).resolve())
    except (OSError, ValueError):
        return False


def _row_digest(row):
    """Content fingerprint of one database row, for change detection."""
    return hashlib.sha256(
        json.dumps(dict(row), sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')
    ).hexdigest()


LOG_KEYS = ('at', 'goal_id', 'stage_id', 'task_id', 'action', 'actor', 'detail')


def _log_fingerprint(row):
    """Stable identity of one stage_log row (the table has no natural key)."""
    payload = {key: (row.get(key) or '') for key in LOG_KEYS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')
    ).hexdigest()


def backup_sqlite(source, dest):
    """Copy a possibly-WAL-backed SQLite database with the online backup API.

    ``shutil.copy2`` silently misses everything still living in the ``-wal``
    file, so a crash right after apply would lose the freshest legacy rows.  The
    backup API reads the live database (WAL included), and the copy is verified
    with ``integrity_check`` before it is trusted as the recovery artifact.
    """
    source, dest = Path(source), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    origin = sqlite3.connect(str(source), timeout=15)
    try:
        copy = sqlite3.connect(str(dest), timeout=15)
        try:
            origin.backup(copy)
            copy.execute('PRAGMA journal_mode=DELETE')
            copy.commit()
        finally:
            copy.close()
    finally:
        origin.close()
    check = sqlite3.connect(str(dest), timeout=15)
    try:
        verdict = str(check.execute('PRAGMA integrity_check').fetchone()[0] or '')
    finally:
        check.close()
    if verdict.lower() != 'ok':
        raise MigrationError('旧阶段库备份完整性校验失败：' + verdict)
    return project_storage.digest_file(dest)


def _legacy_goals():
    path = legacy_db()
    if not path.is_file():
        return [], [], []
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        goals = [dict(row) for row in conn.execute('SELECT * FROM goals')]
        stages = [dict(row) for row in conn.execute('SELECT * FROM stages')]
        logs = [dict(row) for row in conn.execute('SELECT * FROM stage_log')]
    finally:
        conn.close()
    return goals, stages, logs


def discover():
    """Classify every legacy run and goal by its real, resolved project."""
    index = project_storage.index()
    runs = legacy_runs()
    tasks = {'assigned': [], 'unassigned': [], 'invalid': [], 'conflict': [], 'already': [],
             'unsafe': []}
    seen = set()
    if runs.is_dir():
        for entry in sorted(runs.iterdir(), key=lambda item: item.name, reverse=True):
            if not entry.is_dir() or not TASK_ID.fullmatch(entry.name):
                continue
            seen.add(entry.name)
            task_file = entry / 'task.json'
            record = _read_task(task_file) if task_file.is_file() else None
            if record is None and not (entry / 'result.json').exists():
                tasks['invalid'].append({'task_id': entry.name, 'reason': '缺少可读的 task.json / result.json'})
                continue
            if record is None:
                record = _read_task(entry / 'result.json') or {}
            raw_project = record.get('project')
            project = project_storage.usable(raw_project)
            indexed = project_storage.locate('task', entry.name)
            if indexed:
                if project and project_storage.key(indexed) != project_storage.key(project):
                    tasks['conflict'].append({'task_id': entry.name, 'reason': '索引项目与记录 project 不一致',
                                              'indexed_project': indexed, 'record_project': project})
                else:
                    tasks['already'].append({'task_id': entry.name, 'project': indexed})
                continue
            if project is None:
                tasks['unassigned'].append({'task_id': entry.name, 'project': raw_project or '',
                                            'reason': '归属缺失或目录无效/不存在，保留在旧未归属存储'})
                continue
            dest = project_storage.task_dir(project, entry.name)
            if _inside(dest, entry) or _inside(entry, dest):
                # Copying a run into its own descendant would recurse into the
                # copy being written; the original pair stays untouched instead.
                tasks['unsafe'].append({
                    'task_id': entry.name, 'project': project, 'run': str(entry), 'dest': str(dest),
                    'reason': '目标记录位置与源记录目录互相嵌套，递归复制不安全：已显式跳过，'
                              '旧原件与项目目录都未改动'})
                continue
            tasks['assigned'].append({'task_id': entry.name, 'project': project,
                                      'run': str(entry), 'goal_id': record.get('goal_id') or '',
                                      'stage_id': record.get('stage_id') or ''})

    goals, stages, logs = _legacy_goals()
    goal_rows = {'assigned': [], 'unassigned': []}
    stage_by_goal = {}
    for stage in stages:
        stage_by_goal.setdefault(stage.get('goal_id') or '', []).append(stage)
    log_by_goal = {}
    for row in logs:
        log_by_goal.setdefault(row.get('goal_id') or '', []).append(row)
    for goal in goals:
        project = project_storage.usable(goal.get('project'))
        if project is None:
            goal_rows['unassigned'].append({'goal_id': goal.get('id'), 'project': goal.get('project') or '',
                                            'reason': '归属缺失或目录无效/不存在，保留在旧未归属存储'})
            continue
        indexed = project_storage.locate('goal', goal.get('id'))
        if indexed and project_storage.key(indexed) != project_storage.key(project):
            tasks['conflict'].append({'goal_id': goal.get('id'), 'reason': '索引项目与目标 project 不一致'})
            continue
        goal_rows['assigned'].append({
            'goal_id': goal.get('id'), 'project': project, 'status': goal.get('status'),
            'stages': len(stage_by_goal.get(goal.get('id') or '', [])),
            'logs': len(log_by_goal.get(goal.get('id') or '', []))})

    projects = {}
    for row in tasks['assigned']:
        projects.setdefault(project_storage.key(row['project']), {'project': row['project'],
                                                                  'tasks': 0, 'goals': 0, 'stages': 0})
        projects[project_storage.key(row['project'])]['tasks'] += 1
    for row in goal_rows['assigned']:
        bucket = projects.setdefault(project_storage.key(row['project']),
                                     {'project': row['project'], 'tasks': 0, 'goals': 0, 'stages': 0})
        bucket['goals'] += 1
        bucket['stages'] += row['stages']

    return {'legacy_runs': str(runs), 'legacy_db': str(legacy_db()),
            'projects': sorted(projects.values(), key=lambda row: row['project'].casefold()),
            'tasks': tasks, 'goals': {'assigned': goal_rows['assigned'], 'unassigned': goal_rows['unassigned']},
            'counts': {'assigned_tasks': len(tasks['assigned']), 'assigned_goals': len(goal_rows['assigned']),
                       'already_migrated': len(tasks['already']), 'unassigned_tasks': len(tasks['unassigned']),
                       'unassigned_goals': len(goal_rows['unassigned']), 'invalid': len(tasks['invalid']),
                       'unsafe': len(tasks['unsafe']), 'conflicts': len(tasks['conflict'])},
            'note': ('只有能解析为真实存在目录的 project 才迁移；未归属/无效/冲突/嵌套不安全一律原样保留在旧存储，'
                     '不假定安装目录。')}


# --------------------------------------------------------------------- rewrite

def rewrite_record(record, source, dest):
    """Deterministic legacy->project rewrite; verify applies the same function."""
    source, dest = Path(source), Path(dest)

    def walk(value):
        if isinstance(value, str):
            return (value.replace(str(source), str(dest))
                         .replace(str(source).replace('\\', '/'), str(dest).replace('\\', '/')))
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    updated = walk(dict(record))
    updated.setdefault(MIGRATION_MARKER, str(source))
    return updated


def _stage_dir(dest):
    """A recognisable partial-copy directory that is never read as a record.

    The token contains a dot, which ``project_storage``'s task-id pattern
    rejects, so a crashed half-copy can never be enumerated as a real task.
    """
    dest = Path(dest)
    return dest.with_name(dest.name + STAGING_TOKEN + uuid4().hex)


def _staging_dirs(project):
    runs = project_storage.runs_dir(project)
    if not runs.is_dir():
        return []
    out = []
    for item in runs.iterdir():
        if not item.is_dir():
            continue
        base = item.name.split(STAGING_TOKEN)[0]
        if STAGING_TOKEN in item.name and TASK_ID.fullmatch(base):
            out.append(item)
    return out


def sweep_staging(projects):
    """Remove only this tool's leftover partial copies; never a real record.

    Called before an apply so a crash between ``mkdir`` and the atomic rename
    cannot leave an unrecognisable half-copy that later blocks the real record.
    """
    removed = []
    for project in projects:
        for item in _staging_dirs(project):
            shutil.rmtree(item, ignore_errors=True)
            removed.append(str(item))
    return removed


def _copy_run(source, dest, record):
    """Copy one run into place through a staging directory plus an atomic rename.

    The original is only ever read.  A failure (or a crash) leaves either the
    untouched original or a directory explicitly named as a partial copy, never
    a half-populated record that looks real.
    """
    source, dest = Path(source), Path(dest)
    if dest.exists():
        raise MigrationError('目标记录目录已存在，拒绝覆盖：' + str(dest))
    if _inside(dest, source):
        raise MigrationError('目标记录目录在源目录内部，拒绝递归复制：' + str(dest))
    staging = _stage_dir(dest)
    is_junction = getattr(os.path, 'isjunction', lambda path: False)
    try:
        staging.mkdir(parents=True)
        for item in source.rglob('*'):
            if item.is_symlink() or is_junction(item):
                raise MigrationError('记录中包含链接路径，拒绝迁移：' + str(item))
            if item.is_dir():
                continue
            target = staging / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        # ``task.json`` is written last so the atomic rename publishes a record
        # whose single authority is already complete.
        registry.write_json(staging / 'task.json', rewrite_record(record, source, dest))
        staging.rename(dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _same_task(source, dest, record):
    if not (dest / 'task.json').is_file():
        return False
    try:
        current = json.loads((dest / 'task.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return current == rewrite_record(record, source, dest)


# ------------------------------------------------------------------ migration

def _active_metadata(projects=None):
    reasons = []
    rows = list(project_storage.iter_task_dirs())
    seen = {row['task_id'] for row in rows}
    # The stores this plan/journal really touches are checked directly, so a
    # missing locator index cannot hide an active task in one of them.
    for project in (projects or []):
        try:
            extra = project_storage.iter_task_dirs({'mode': 'project', 'project': project})
        except (OSError, ValueError):
            continue
        for row in extra:
            if row['task_id'] not in seen:
                seen.add(row['task_id'])
                rows.append(row)
    for entry in rows:
        try:
            row = registry.read_task(entry['run'])
        except (OSError, ValueError, FileNotFoundError):
            continue
        if row.get('status') in {'queued', 'running', 'dispatching'}:
            reasons.append('任务 ' + entry['task_id'] + ' 状态 ' + str(row.get('status')))
    # Only inspect stores that already exist; a dry inspection must not create one.
    stores = list(projects or [])
    if legacy_db().is_file() or project_storage.indexed_projects():
        stores.append(None)
    counts = {}
    for store in stores:
        by_status = (stage_store.stats(store) if store else stage_store.stats()).get('by_status') or {}
        for state, number in by_status.items():
            counts[state] = counts.get(state, 0) + int(number)
    for state in ('dispatched', 'running', 'recovery_pending'):
        if counts.get(state):
            reasons.append('阶段状态 %s × %s' % (state, counts[state]))
    return reasons


def _live_workers(projects=None):
    roots = [str(root).replace('/', '\\').casefold() for _project, root in project_storage.task_roots()]
    for project in (projects or []):
        try:
            usable = project_storage.usable(project)
            if usable:
                roots.append(str(project_storage.runs_dir(usable)).replace('/', '\\').casefold())
        except (OSError, ValueError):
            continue
    roots.append(str(legacy_runs()).replace('/', '\\').casefold())
    roots = sorted(set(roots))
    try:
        import psutil
    except ImportError:
        return ['无法加载 psutil，不能证明没有 worker 进程']
    found = []
    for process in psutil.process_iter(['name']):
        if process.info.get('name') not in {'python.exe', 'node.exe', 'DeepSeek Harness.exe'}:
            continue
        try:
            command = ' '.join(process.cmdline()).replace('/', '\\').casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(root in command for root in roots):
            found.append({'pid': process.info.get('pid'), 'name': process.info.get('name')})
    return found


def assert_idle(projects=None):
    reasons = _active_metadata(projects)
    workers = _live_workers(projects)
    if workers:
        reasons.append('仍有引用工作台 runs 的进程：' + json.dumps(workers, ensure_ascii=False))
    if reasons:
        raise MigrationError('队列并非空闲，拒绝真实迁移/压缩：' + '；'.join(reasons))


def _project_db_rows(project):
    path = project_storage.records_db(project)
    return path


def _copy_goals(project, goal_rows, stages_by_goal, logs_by_goal):
    """Copy goal/stage/log rows and return exactly what *this run* created.

    Every record carries ``created`` plus a content digest, so a rollback can
    prove it is undoing its own work: an id that already existed is never
    claimed, and a later edit is detected by the digest.  Logs are matched by a
    stable fingerprint, which makes a repeated apply idempotent instead of
    duplicating the whole log.
    """
    conn = project_storage.connect(_project_db_rows(project), create=True)
    copied = {'goals': 0, 'stages': 0, 'logs': 0}
    facts = {'goals': [], 'stages': [], 'logs': []}
    try:
        known_logs = {_log_fingerprint(dict(row)) for row in
                      conn.execute('SELECT at, goal_id, stage_id, task_id, action, actor, detail FROM stage_log')}
        for goal in goal_rows:
            existing = conn.execute('SELECT id FROM goals WHERE id = ?', (goal['id'],)).fetchone()
            created = existing is None
            if created:
                conn.execute('INSERT INTO goals (id, title, objective, project, status, created_at, updated_at, data) '
                             'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                             (goal['id'], goal['title'], goal.get('objective', ''), project,
                              goal.get('status', 'active'), goal.get('created_at'), goal.get('updated_at'),
                              goal.get('data') or '{}'))
                copied['goals'] += 1
            row = conn.execute('SELECT * FROM goals WHERE id = ?', (goal['id'],)).fetchone()
            facts['goals'].append({'goal_id': goal['id'], 'project': project, 'copied': created,
                                   'digest': _row_digest(row)})
            project_storage.register('goal', goal['id'], project)
            for stage in stages_by_goal.get(goal['id'], []):
                row = conn.execute('SELECT id FROM stages WHERE id = ?', (stage['id'],)).fetchone()
                stage_created = row is None
                if stage_created:
                    conn.execute(
                        'INSERT INTO stages (id, goal_id, ordinal, title, objective, kind, project, files, acceptance, '
                        'candidates, fixed_model, depends_on, status, session_policy, session_key, task_ids, '
                        'active_task_id, created_at, updated_at, data) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (stage['id'], goal['id'], stage.get('ordinal', 1), stage['title'], stage.get('objective', ''),
                         stage.get('kind') or 'edit', project, stage.get('files') or '[]',
                         stage.get('acceptance') or '[]', stage.get('candidates') or '[]', stage.get('fixed_model') or '',
                         stage.get('depends_on') or '[]', stage.get('status', 'pending'),
                         stage.get('session_policy') or 'new_session', stage.get('session_key') or '',
                         stage.get('task_ids') or '[]', stage.get('active_task_id') or '',
                         stage.get('created_at'), stage.get('updated_at'), stage.get('data') or '{}'))
                    copied['stages'] += 1
                row = conn.execute('SELECT * FROM stages WHERE id = ?', (stage['id'],)).fetchone()
                facts['stages'].append({'stage_id': stage['id'], 'goal_id': goal['id'], 'project': project,
                                        'copied': stage_created, 'digest': _row_digest(row)})
                project_storage.register('stage', stage['id'], project)
            for log in logs_by_goal.get(goal['id'], []):
                fingerprint = _log_fingerprint(log)
                if fingerprint in known_logs:
                    continue
                known_logs.add(fingerprint)
                cursor = conn.execute(
                    'INSERT INTO stage_log (at, goal_id, stage_id, task_id, action, actor, detail) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (log.get('at'), log.get('goal_id') or '', log.get('stage_id') or '',
                     log.get('task_id') or '', log.get('action') or '', log.get('actor') or 'main',
                     log.get('detail') or ''))
                copied['logs'] += 1
                facts['logs'].append({'seq': cursor.lastrowid, 'goal_id': goal['id'], 'project': project,
                                      'stage_id': log.get('stage_id') or '',
                                      'task_id': log.get('task_id') or ''})
    finally:
        conn.close()
    return copied, facts


def migrate(write):
    """Plan and (when ``write``) copy legacy records into project stores."""
    report = discover()
    if not write:
        report['mode'] = 'dry-run'
        report['action'] = '不写入任何文件；以上为将要执行的迁移计划'
        return report
    assert_idle([row['project'] for row in report['projects']])
    # One apply at a time: two concurrent applies would interleave their
    # staging/index writes and could publish a half-plan journal.
    with project_storage.exclusive_lock('project-migration'):
        return _apply(report)


def _apply(report):
    """The write half of an apply; the caller holds the migration lock."""
    # Unique per run: two applies in the same wall-clock second must never share
    # a journal, or the first plan's rollback evidence would be overwritten.
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]
    backup = backups_root() / stamp
    backup.mkdir(parents=True, exist_ok=True)
    manifest = {'created_at': registry.now(), 'legacy_runs': str(legacy_runs()),
                'legacy_db': str(legacy_db()), 'tasks': [], 'goals': []}
    legacy_backup = None
    if legacy_db().is_file():
        # WAL-safe: a plain file copy would silently drop everything still in
        # the -wal file, which is exactly the freshest legacy history.
        legacy_backup = backup / stage_store.DB_NAME
        manifest['legacy_db_backup'] = str(legacy_backup)
        manifest['legacy_db_digest'] = backup_sqlite(legacy_db(), legacy_backup)
        manifest['legacy_db_wal_note'] = '使用 SQLite 在线备份 API 复制（包含 WAL 中已提交但未 checkpoint 的数据）'
    journal = {'created_at': registry.now(), 'root': str(backup), 'phase': 'applying',
               'tasks': [], 'goals': [], 'stages': [], 'logs': []}
    registry.write_json(backup / MANIFEST_NAME, manifest)
    registry.write_json(backup / JOURNAL_NAME, journal)
    swept = sweep_staging([row['project'] for row in report['tasks']['assigned']])

    goals, stages, logs = _legacy_goals()
    stages_by_goal, logs_by_goal = {}, {}
    for stage in stages:
        stages_by_goal.setdefault(stage.get('goal_id') or '', []).append(stage)
    for row in logs:
        logs_by_goal.setdefault(row.get('goal_id') or '', []).append(row)
    goals_by_project = {}
    for goal in goals:
        project = project_storage.usable(goal.get('project'))
        if project is None:
            continue
        goals_by_project.setdefault(project_storage.key(project), []).append(goal)

    copied = 0
    # Records already owned by a project are reported as skipped, never re-copied.
    skipped = len(report['tasks']['already'])
    failures = []
    for row in report['tasks']['assigned']:
        project, task_id, source = row['project'], row['task_id'], Path(row['run'])
        dest = project_storage.task_dir(project, task_id)
        record = _read_task(source / 'task.json')
        try:
            if record is None:
                raise MigrationError('源 task.json 不可读')
            if dest.exists() and _same_task(source, dest, record):
                skipped += 1
                journal['tasks'].append({'task_id': task_id, 'project': project, 'dest': str(dest),
                                         'status': 'already', 'copied': False})
                # Repair a crash window: the copy may exist while the index or
                # the derived association row was never written.  Re-asserting
                # both is idempotent and never touches task.json.
                project_storage.register('task', task_id, project)
                project_storage.register_task_record(project, task_id, record.get('goal_id'),
                                                     record.get('stage_id'), record.get('kind'),
                                                     record.get('created_at'))
            elif dest.exists():
                # Never overwrite a record that was not produced by this exact plan.
                failures.append({'task_id': task_id, 'reason': '目标已存在且内容不同，拒绝覆盖'})
                journal['tasks'].append({'task_id': task_id, 'project': project, 'dest': str(dest),
                                         'status': 'conflict', 'copied': False})
            else:
                project_storage.ensure_project(project)
                _copy_run(source, dest, record)
                # The journal records the copy before the index/derived writes,
                # so a crash here is still identifiable and repairable.
                journal['tasks'].append({'task_id': task_id, 'project': project, 'dest': str(dest),
                                         'status': 'copying', 'copied': True,
                                         'digest': project_storage.digest_tree(dest)})
                registry.write_json(backup / JOURNAL_NAME, journal)
                project_storage.register('task', task_id, project)
                project_storage.register_task_record(project, task_id, record.get('goal_id'),
                                                     record.get('stage_id'), record.get('kind'),
                                                     record.get('created_at'))
                copied += 1
                journal['tasks'][-1]['status'] = 'copied'
            manifest['tasks'].append({'task_id': task_id, 'source': str(source), 'dest': str(dest),
                                      'source_digest': project_storage.digest_tree(source)})
            registry.write_json(backup / JOURNAL_NAME, journal)
        except Exception as error:  # noqa: BLE001 - report per-record, keep going
            leftover = [str(item) for item in _staging_dirs(project)
                        if item.name.split(STAGING_TOKEN)[0] == task_id]
            failures.append({'task_id': task_id, 'reason': str(error)})
            journal['tasks'].append({'task_id': task_id, 'project': project, 'dest': str(dest),
                                     'status': 'failed', 'copied': False, 'reason': str(error),
                                     'partial_cleaned': not leftover, 'partial_left': leftover})
            registry.write_json(backup / JOURNAL_NAME, journal)

    goal_copies = {'goals': 0, 'stages': 0, 'logs': 0}
    for project_key, goal_rows in goals_by_project.items():
        project = goal_rows[0]['project']
        try:
            project_storage.ensure_project(project)
            result, facts = _copy_goals(project, goal_rows, stages_by_goal, logs_by_goal)
            for key in ('goals', 'stages', 'logs'):
                goal_copies[key] += result[key]
            # Provenance, not "the goal now exists": only rows really inserted by
            # this run may ever be removed by its rollback.
            journal['goals'].extend(facts['goals'])
            journal['stages'].extend(facts['stages'])
            journal['logs'].extend(facts['logs'])
            journal['goal_copies'] = goal_copies
            registry.write_json(backup / JOURNAL_NAME, journal)
        except Exception as error:  # noqa: BLE001
            failures.append({'goal_id': project_key, 'reason': str(error)})
            registry.write_json(backup / JOURNAL_NAME, journal)
    journal['phase'] = 'applied'
    journal['failures'] = failures
    journal['copied_tasks'] = copied
    journal['skipped_tasks'] = skipped
    journal['staging_swept'] = swept
    registry.write_json(backup / JOURNAL_NAME, journal)
    manifest['copied_tasks'] = copied
    manifest['skipped_tasks'] = skipped
    manifest['staging_swept'] = swept
    registry.write_json(backup / MANIFEST_NAME, manifest)
    report.update({'mode': 'apply', 'backup': str(backup), 'copied_tasks': copied,
                   'skipped_tasks': skipped, 'goals_copied': goal_copies, 'failures': failures,
                   'staging_swept': swept,
                   'legacy_db_backup': str(legacy_backup) if legacy_backup else None,
                   'originals': '旧记录原件未删除、未修改；它们同时是这次迁移的可恢复备份',
                   'idempotency': '重复执行 apply 会跳过已迁移且内容一致的记录，不产生第二份。'})
    return report


def verify(project=None):
    """Compare every migrated destination with the legacy original it came from."""
    report = discover()
    checks = []
    targets = [row for row in report['tasks']['already'] + report['tasks']['assigned']
               if not project or project_storage.key(row['project']) == project_storage.key(project)]
    unresolved = []
    for row in targets:
        task_id = row['task_id']
        project = row['project']
        source = legacy_runs() / task_id
        dest = project_storage.task_dir(project, task_id)
        record = _read_task(source / 'task.json')
        problems = []
        if record is None:
            unresolved.append({'task_id': task_id, 'reason': '旧原件不可读，无法复核'})
            continue
        if not dest.is_dir():
            problems.append('项目目标缺少该记录')
        else:
            expected = rewrite_record(record, source, dest)
            try:
                current = json.loads((dest / 'task.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                current = None
            if current != expected:
                problems.append('task.json 与原件重写结果不一致')
            for item in source.rglob('*'):
                if item.is_dir():
                    continue
                target = dest / item.relative_to(source)
                if not target.is_file():
                    problems.append('缺少文件 ' + item.relative_to(source).as_posix())
                elif item.name != 'task.json':
                    if project_storage.digest_file(item) != project_storage.digest_file(target):
                        problems.append('文件内容不一致 ' + item.relative_to(source).as_posix())
        if project_storage.locate('task', task_id) != project:
            problems.append('全局定位索引未指向该项目')
        checks.append({'task_id': task_id, 'project': project, 'ok': not problems, 'problems': problems})
    goals, stages, _logs = _legacy_goals()
    goal_checks = []
    for goal in goals:
        project_path = project_storage.usable(goal.get('project'))
        if project_path is None or (project and project_storage.key(project_path) != project_storage.key(project)):
            continue
        problems = []
        conn = project_storage.connect(project_storage.records_db(project_path), create=False)
        try:
            row = conn.execute('SELECT * FROM goals WHERE id = ?', (goal['id'],)).fetchone()
            if row is None:
                problems.append('项目库缺少目标')
            else:
                if row['status'] != goal.get('status'):
                    problems.append('目标状态不一致')
                for stage in stages:
                    if stage.get('goal_id') != goal['id']:
                        continue
                    stage_row = conn.execute('SELECT * FROM stages WHERE id = ?', (stage['id'],)).fetchone()
                    if stage_row is None:
                        problems.append('缺少阶段 ' + str(stage['id']))
                    elif stage_row['task_ids'] != (stage.get('task_ids') or '[]'):
                        problems.append('阶段任务关联不一致 ' + str(stage['id']))
        finally:
            conn.close()
        goal_checks.append({'goal_id': goal['id'], 'project': project_path, 'ok': not problems,
                            'problems': problems})
    return {'mode': 'verify', 'tasks': checks, 'goals': goal_checks, 'unresolved': unresolved,
            'ok': all(row['ok'] for row in checks) and all(row['ok'] for row in goal_checks)
                  and not unresolved,
            'ledger_note': '迁移不读取也不写入模型配置与费用总账；旧费用 taskid 关联原样保留。'}


def _rollback_goals(conn, row, facts, skipped):
    """Delete only the rows this exact journal created and never changed since.

    A goal that already existed before the apply is never removed, and a goal or
    stage edited/added after the migration is kept with its newer history.  A
    goal is only removed when every stage it owns was created by this run and is
    still byte-identical to the recorded digest, so a rollback can never restore
    an old state over newer work.
    """
    goal_id = row.get('goal_id')
    current = conn.execute('SELECT * FROM goals WHERE id = ?', (goal_id,)).fetchone()
    if current is None:
        project_storage.unregister('goal', goal_id)
        return 0, 0
    if not row.get('copied'):
        skipped.append({'goal_id': goal_id, 'reason': '目标在迁移前已存在，回滚不会删除既有记录'})
        return 0, 0
    if row.get('digest') and _row_digest(current) != row['digest']:
        skipped.append({'goal_id': goal_id, 'reason': '目标在迁移后被修改，保留新历史，拒绝删除'})
        return 0, 0
    stage_facts = {item['stage_id']: item for item in facts.get('stages', [])
                   if item.get('goal_id') == goal_id}
    present = {stage['id']: stage for stage in conn.execute('SELECT * FROM stages WHERE goal_id = ?', (goal_id,))}
    removable = []
    kept = []
    for stage_id, stage in present.items():
        fact = stage_facts.get(stage_id)
        if fact is None or not fact.get('copied'):
            kept.append({'stage_id': stage_id, 'reason': '阶段不属于本次迁移创建，保留'})
            continue
        if fact.get('digest') and _row_digest(stage) != fact['digest']:
            kept.append({'stage_id': stage_id, 'reason': '阶段在迁移后被修改，保留'})
            continue
        removable.append(stage_id)
    if kept:
        skipped.append({'goal_id': goal_id,
                        'reason': '迁移后新增或修改了阶段，保留目标与全部新历史',
                        'kept_stages': kept})
        return 0, 0
    log_seqs = [item['seq'] for item in facts.get('logs', [])
                if item.get('goal_id') == goal_id and item.get('seq') is not None
                and item.get('created', True)]
    for seq in log_seqs:
        conn.execute('DELETE FROM stage_log WHERE seq = ?', (seq,))
    for stage_id in removable:
        conn.execute('DELETE FROM stages WHERE id = ?', (stage_id,))
        project_storage.unregister('stage', stage_id)
    conn.execute('DELETE FROM goals WHERE id = ?', (goal_id,))
    project_storage.unregister('goal', goal_id)
    return 1, len(removable)


def rollback(backup=None):
    """Undo one apply using its journal; originals and user files are untouched.

    Safety rules: nothing runs while the queue is busy, only copies this journal
    proves it created are removed, and a record changed after the migration (or
    any newer stage/log under it) is kept and reported instead of being erased.
    """
    root = Path(backup) if backup else None
    if root is None:
        candidates = sorted((row for row in backups_root().iterdir() if row.is_dir()), reverse=True) \
            if backups_root().is_dir() else []
        if not candidates:
            raise MigrationError('没有可回滚的迁移日志')
        root = candidates[0]
    journal_path = root / JOURNAL_NAME
    if not journal_path.is_file():
        raise MigrationError('迁移日志不存在：' + str(journal_path))
    # The stores named by the journal are checked directly, so a missing locator
    # index cannot hide an active task/stage that this rollback would race.
    try:
        recorded = registry.read_json(journal_path)
    except (OSError, ValueError):
        recorded = {}
    touched = {row.get('project') for row in
               list(recorded.get('tasks') or []) + list(recorded.get('goals') or [])}
    assert_idle([path for path in touched if path])
    with project_storage.exclusive_lock('project-migration'):
        return _rollback(root, journal_path)


def _rollback(root, journal_path):
    journal = registry.read_json(journal_path)
    facts = {'stages': journal.get('stages', []), 'logs': journal.get('logs', [])}
    removed_tasks = removed_goals = removed_stages = 0
    skipped = []
    for row in journal.get('tasks', []):
        if not row.get('copied'):
            continue
        dest = Path(row['dest'])
        recorded = row.get('digest')
        if not dest.exists():
            project_storage.forget_task(row['task_id'], row.get('project'))
            continue
        # Any leftover partial copy of this task is this tool's own artifact.
        for item in _staging_dirs(row['project']):
            if item.name.split(STAGING_TOKEN)[0] == row['task_id']:
                shutil.rmtree(item, ignore_errors=True)
        current = project_storage.digest_tree(dest)
        if recorded and current != recorded:
            skipped.append({'task_id': row['task_id'], 'reason': '迁移后记录已变化，拒绝删除'})
            continue
        project_storage.remove_tree(dest, project_storage.runs_dir(row['project']))
        project_storage.forget_task(row['task_id'], row['project'])
        removed_tasks += 1
    for row in journal.get('goals', []):
        project = row.get('project')
        if not project:
            continue
        try:
            conn = project_storage.connect(project_storage.records_db(project), create=False)
        except (OSError, ValueError) as error:
            skipped.append({'goal_id': row.get('goal_id'), 'reason': '项目库不可读：' + str(error)})
            continue
        try:
            with conn:
                goals, stages = _rollback_goals(conn, row, facts, skipped)
                removed_goals += goals
                removed_stages += stages
        finally:
            conn.close()
    journal['phase'] = 'rolled_back'
    journal['rolled_back_at'] = registry.now()
    journal['rollback_skipped'] = skipped
    registry.write_json(journal_path, journal)
    return {'mode': 'rollback', 'backup': str(root), 'removed_tasks': removed_tasks,
            'removed_goals': removed_goals, 'removed_stages': removed_stages, 'skipped': skipped,
            'note': '只撤销本日志确实创建且此后未改变的项目副本；旧原件、既有记录与用户项目文件未改动。'}


def _render(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description='Migrate legacy workbench records into per-project stores.')
    parser.add_argument('--mode', choices=['preview', 'dry-run', 'apply', 'verify', 'rollback'],
                        default='preview')
    parser.add_argument('--project', help='只处理/复核一个明确项目')
    parser.add_argument('--backup', help='rollback 使用的备份/日志目录')
    args = parser.parse_args(argv)
    try:
        if args.mode == 'preview':
            result = discover()
            result['mode'] = 'preview'
        elif args.mode == 'dry-run':
            result = migrate(write=False)
        elif args.mode == 'apply':
            result = migrate(write=True)
        elif args.mode == 'verify':
            result = verify(args.project)
        else:
            result = rollback(args.backup)
    except (OSError, ValueError, MigrationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(_render(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
