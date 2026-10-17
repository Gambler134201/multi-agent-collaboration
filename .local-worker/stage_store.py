"""Persistent goals and stages for the local Model Desk.

Design (deliberately local, with no new LLM loop):

* Goals and stages live in one SQLite database (``stages.db``) next to the model
  registry.  Every write runs in an explicit transaction; a dispatch claim uses
  ``BEGIN IMMEDIATE`` so at most one caller can start a stage task.
* A dependent stage becomes dispatchable only after every prerequisite stage was
  **independently accepted** (``accepted`` or the main-mind ``final_accepted``).
* This module only persists state, reports readiness and reconciles itself with
  the real task records.  Planning and acceptance stay with the main brain; no
  function here calls a model, opens a socket or spends budget.
* Restarting the panel never silently re-sends a paid request: a stage that was
  dispatched but whose task did not finish becomes ``recovery_pending`` and must
  be explicitly requeued after inspection.
* Existing JSON task records and the legacy cost ledger are untouched.  This is
  a new, separate store and no old fee record is migrated.

CLI (venv Python, from ``.local-worker``)::

    stage_store.py --list-goals
    stage_store.py --create-goal --title "..." --objective "..." --project <abs>
    stage_store.py --add-stage --goal <goal> --title "..." --objective "..." \\
        --depends <stage>,<stage> --files a.py,b.py --acceptance "c1;c2" \\
        --candidates home --fixed-model home --kind chat|edit
    stage_store.py --stage <stage>
    stage_store.py --list-stages <goal>
    stage_store.py --sync
    stage_store.py --recover
    stage_store.py --requeue --stage <stage> --reason "..."
    stage_store.py --cancel --stage <stage> --reason "..."

Dispatch itself still goes through the shared queue: ``dispatch_task.py --stage
<stage> --task-file <UTF-8-task>``.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys

import delegation_policy
import model_registry as registry
import project_storage

SCHEMA = 1
DB_NAME = 'stages.db'

GOAL_ACTIVE = 'active'
GOAL_COMPLETED = 'completed'
GOAL_CANCELLED = 'cancelled'
GOAL_STATES = {GOAL_ACTIVE, GOAL_COMPLETED, GOAL_CANCELLED}

PENDING = 'pending'
READY = 'ready'
DISPATCHED = 'dispatched'
RUNNING = 'running'
EXECUTOR_COMPLETED = 'executor_completed'
ACCEPTED = 'accepted'
FINAL_ACCEPTED = 'final_accepted'
RETURNED = 'returned'
BLOCKED = 'blocked'
FAILED = 'failed'
CANCELLED = 'cancelled'
RECOVERY_PENDING = 'recovery_pending'

STAGE_STATES = {PENDING, READY, DISPATCHED, RUNNING, EXECUTOR_COMPLETED, ACCEPTED,
                FINAL_ACCEPTED, RETURNED, BLOCKED, FAILED, CANCELLED, RECOVERY_PENDING}
ACCEPTED_STATES = {ACCEPTED, FINAL_ACCEPTED}
ACTIVE_STATES = {DISPATCHED, RUNNING}
# Statuses that may be claimed again once prerequisites are accepted.  ``failed``
# and ``recovery_pending`` are intentionally absent: they need an explicit
# requeue so a crash or a failed run is never retried behind the main mind's back.
DISPATCHABLE_STATES = {PENDING, READY, RETURNED, EXECUTOR_COMPLETED}
REQUEUEABLE_STATES = {FAILED, BLOCKED, RECOVERY_PENDING, CANCELLED, EXECUTOR_COMPLETED}

SESSION_NEW = 'new_session'
SESSION_REUSE = 'reuse_session'
SESSION_POLICIES = {SESSION_NEW, SESSION_REUSE}

# A stage fixes the task mode it will ever be dispatched with: ``chat`` is a
# read-only investigation/design stage, ``edit`` is a file-modifying stage.  The
# dispatch path reads this persisted value and never lets a request parameter
# silently promote a read-only stage into a full-access one.  Old databases and
# old CLI callers have no column/flag, so the default is ``edit`` (the historical
# behaviour) for backward compatibility.
KIND_CHAT = 'chat'
KIND_EDIT = 'edit'
KIND_DEFAULT = KIND_EDIT
STAGE_KINDS = (KIND_CHAT, KIND_EDIT)
KIND_LABELS = {KIND_CHAT: '只读分析 · chat', KIND_EDIT: '完全权限修改 · edit'}

MAX_STAGES = 200
MAX_TEXT = 2000
MAX_ITEMS = 40
MAX_FILES = 30

STAGE_LABELS = {
    PENDING: '待前置阶段通过',
    READY: '就绪 · 可派发',
    DISPATCHED: '已派发 · 待执行',
    RUNNING: '执行中',
    EXECUTOR_COMPLETED: '执行器完成 · 待独立验收',
    ACCEPTED: '独立验收通过',
    FINAL_ACCEPTED: '主脑介入 · 最终验收通过',
    RETURNED: '退回整改',
    BLOCKED: '受阻 · 需主脑判断',
    FAILED: '执行未完成 · 需主脑决定',
    CANCELLED: '已取消',
    RECOVERY_PENDING: '待恢复 · 未完成派发需人工确认',
}

STAGES_NOTE = ('阶段状态、依赖与实际任务记录是真实持久记录；调度器只报告就绪与恢复，'
               '计划与验收仍由主脑负责，不存在自动 LLM 主循环。')


class StageError(ValueError):
    """A user-facing validation failure for goals/stages."""


# ------------------------------------------------------------------ persistence

def db_path(project=None):
    """Legacy global stage DB, or one project's ``.model-desk/records.db``.

    ``project=None``/``''`` keeps the historical global database.  A real project
    path routes to that project's records.db; nothing here ever mutates a global
    "current project" or ``RUNS`` variable.
    """
    if project:
        return project_storage.records_db(project)
    return Path(registry.HERE) / DB_NAME


def _schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            objective TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS stages (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            title TEXT NOT NULL,
            objective TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'edit',
            project TEXT NOT NULL DEFAULT '',
            files TEXT NOT NULL DEFAULT '[]',
            acceptance TEXT NOT NULL DEFAULT '[]',
            candidates TEXT NOT NULL DEFAULT '[]',
            fixed_model TEXT NOT NULL DEFAULT '',
            depends_on TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            session_policy TEXT NOT NULL DEFAULT 'new_session',
            session_key TEXT NOT NULL DEFAULT '',
            task_ids TEXT NOT NULL DEFAULT '[]',
            active_task_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS stage_log (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            goal_id TEXT NOT NULL DEFAULT '',
            stage_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'main',
            detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS stages_goal ON stages (goal_id, ordinal);
        CREATE INDEX IF NOT EXISTS stage_log_stage ON stage_log (stage_id, seq);
    ''')
    conn.execute('INSERT INTO meta (key, value) VALUES ("schema", ?) '
                 'ON CONFLICT(key) DO NOTHING', (str(SCHEMA),))
    # Forward-compatible migration: an existing stages.db created before stage
    # ``kind`` existed gets the column with the historical ``edit`` default, so
    # old records keep working instead of being reinterpreted.
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(stages)')}
    if 'kind' not in columns:
        try:
            conn.execute("ALTER TABLE stages ADD COLUMN kind TEXT NOT NULL DEFAULT 'edit'")
        except sqlite3.OperationalError as exc:
            # A concurrent first-open added the column between PRAGMA and ALTER;
            # the resulting schema is identical, so this is not a failure.
            if 'duplicate column' not in str(exc).lower():
                raise


@contextmanager
def connection(project=None, create=False):
    """Open one stage store, ensure the schema, and always close it.

    ``project`` is either '' / None (the legacy global stages.db) or a real
    project directory (its ``.model-desk/records.db``).  ``create=False`` never
    creates a project store: a missing project DB is served as an empty
    in-memory database, so reading an untouched project cannot write a hidden
    directory.  ``create=True`` is the explicit first-time initialization.
    """
    if project:
        if create:
            project_storage.ensure_project(project)
        conn = project_storage.connect(project_storage.records_db(project), create=create)
        try:
            yield conn
        finally:
            conn.close()
        return
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        # busy_timeout must be set before any statement that can need a write
        # lock, otherwise a concurrent first-open of a legacy file can fail with
        # "database is locked" while it is still being switched to WAL.
        conn.execute('PRAGMA busy_timeout=15000')
        try:
            conn.execute('PRAGMA journal_mode=WAL')
        except sqlite3.OperationalError:
            # A racing connection is switching the same brand-new file to WAL.
            # The journal mode is a property of the file, so the other winner's
            # switch is equivalent to ours.
            pass
        _schema(conn)
        yield conn
    finally:
        conn.close()


def _transaction(conn):
    """Explicit write transaction; ``BEGIN IMMEDIATE`` serialises claimers."""
    class _Tx:
        def __enter__(self):
            conn.execute('BEGIN IMMEDIATE')
            return conn

        def __exit__(self, exc_type, exc, tb):
            # A nested ``with conn:`` may already have committed/rolled back.
            if exc_type is None:
                if conn.in_transaction:
                    conn.execute('COMMIT')
            elif conn.in_transaction:
                conn.execute('ROLLBACK')
            return False
    return _Tx()


def record_store(kind, record_id, project=None):
    """The store that really holds one goal/stage ('' = the legacy global store)."""
    if kind not in ('goal', 'stage'):
        raise StageError('未知记录类型：' + str(kind))
    return _store_holding(kind, record_id, project)


def _project_of_goal(goal_id):
    return project_storage.locate('goal', goal_id) or ''


def _project_of_stage(stage_id):
    return project_storage.locate('stage', stage_id) or ''


def _store_holding(kind, record_id, project=None):
    """The store that really holds one record, preferring an explicit project.

    The project's own records.db is authoritative when it holds the row, so an
    explicit project resolves without the global locator.  A record that only
    lives in the legacy store still resolves there.  The locator never decides
    *which* store is read; it only offers a candidate.
    """
    table = {'goal': 'goals', 'stage': 'stages'}[kind]
    candidates = []
    if project:
        candidates.append(project_storage.normalize(project))
    try:
        owning = project_storage.locate(kind, record_id)
    except project_storage.StorageError:
        owning = None
    if owning:
        candidates.append(owning)
    candidates.append('')
    for store in candidates:
        try:
            with connection(store) as conn:
                row = conn.execute('SELECT 1 FROM %s WHERE id = ?' % table, (record_id,)).fetchone()
        except (OSError, ValueError):
            continue
        if row is not None:
            return store
    return ''


def _all_projects():
    """Initialized project stores; the legacy global store is handled separately."""
    return project_storage.indexed_projects()


def _belongs(project_value, project):
    """True when a record's own ``project`` really resolves to ``project``."""
    if not project_value:
        return False
    try:
        return project_storage.key(project_value) == project_storage.key(project)
    except (ValueError, OSError, TypeError):
        return False


def _conflicting(project_value, project):
    """True when a record names *another existing* project directory.

    A stale/unresolvable path is not a conflict with a different project; the
    physical store (whose ``.model-desk`` holds the row) stays authoritative
    there.  Naming a second real project is a genuine identity conflict.
    """
    if not project_value or _belongs(project_value, project):
        return False
    return project_storage.usable(project_value) is not None


def _scoped_stores(project):
    """Stores an explicit project request may read: its own first, then legacy.

    The project's own records.db is authoritative, so the global locator index
    is not needed to open that project.  The legacy global store is still read
    so pre-migration rows of the same project stay visible.
    """
    project = project_storage.normalize(project)
    stores = [project]
    legacy = ''
    if legacy not in stores:
        stores.append(legacy)
    return project, stores


def _register_goal(goal_id, project):
    if project:
        project_storage.register('goal', goal_id, project)


def _register_stage(stage_id, project):
    if project:
        project_storage.register('stage', stage_id, project)


# --------------------------------------------------------------------- helpers

def _clean_text(value, limit=MAX_TEXT):
    return str(value if value is not None else '').strip()[:limit]


def _clean_items(value, limit=MAX_ITEMS, item_limit=500):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise StageError('列表字段格式不正确')
    out = []
    for item in list(value)[:limit]:
        clean = _clean_text(item, item_limit)
        if clean and clean not in out:
            out.append(clean)
    return out


def _clean_files(value, limit=MAX_FILES):
    files = _clean_items(value, limit=limit, item_limit=300)
    for name in files:
        path = Path(name)
        if path.is_absolute() or '..' in path.parts:
            raise StageError('阶段文件范围需要是项目内的相对路径：' + name)
    return files


def _row_goal(row):
    goal = dict(row)
    goal['data'] = json.loads(goal.get('data') or '{}')
    return goal


def _row_stage(row):
    stage = dict(row)
    for key in ('files', 'acceptance', 'candidates', 'depends_on', 'task_ids'):
        stage[key] = json.loads(stage.get(key) or '[]')
    stage['kind'] = stage.get('kind') if stage.get('kind') in STAGE_KINDS else KIND_DEFAULT
    stage['data'] = json.loads(stage.get('data') or '{}')
    return stage


def _log(conn, *, action, actor='main', goal_id='', stage_id='', task_id='', detail=''):
    conn.execute('INSERT INTO stage_log (at, goal_id, stage_id, task_id, action, actor, detail) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?)',
                 (registry.now(), goal_id, stage_id, task_id, action, actor, _clean_text(detail)))


def _new_id(prefix):
    stamp = registry.now().replace(':', '').replace('-', '').replace('+', '').replace('.', '')[:15]
    from uuid import uuid4
    return prefix + '-' + stamp + '-' + uuid4().hex[:6]


# ------------------------------------------------------------------ goals API

def create_goal(title, objective='', project='', actor='main', status=GOAL_ACTIVE, store='auto'):
    """Create a goal.

    ``store='auto'`` (the historical default) keeps a goal in the legacy global
    store unless that project already owns a ``.model-desk`` store.  The panel
    and CLI pass ``store='project'`` to perform the explicit first-time project
    initialization, so a direct legacy caller/test is never silently relocated.
    """
    title = _clean_text(title, 200)
    if not title:
        raise StageError('请填写目标标题')
    if status not in GOAL_STATES:
        raise StageError('目标状态不正确')
    raw_project = _clean_text(project, 500)
    project = str(Path(raw_project).resolve()) if raw_project else ''
    storage_project = ''
    if store == 'project':
        if not raw_project:
            raise StageError('项目记录需要明确的已存在项目目录')
        try:
            storage_project = project_storage.normalize(raw_project)
        except (ValueError, OSError) as exc:
            raise StageError('项目目录无效：' + str(exc)) from None
        project = storage_project
    elif raw_project:
        candidate = project_storage.usable(raw_project)
        if candidate and project_storage.is_initialized(candidate):
            storage_project = candidate
            project = candidate
    now = registry.now()
    goal_id = _new_id('goal')
    with connection(storage_project, create=bool(storage_project)) as conn, _transaction(conn):
        with conn:
            conn.execute('INSERT INTO goals (id, title, objective, project, status, created_at, updated_at, data) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (goal_id, title, _clean_text(objective), project, status, now, now, '{}'))
            _log(conn, action='goal_create', actor=actor, goal_id=goal_id, detail=title)
    _register_goal(goal_id, storage_project)
    return get_goal(goal_id)


def _read_goal(store, goal_id):
    with connection(store) as conn:
        row = conn.execute('SELECT * FROM goals WHERE id = ?', (goal_id,)).fetchone()
        if row is None:
            return None, None
        goal = _row_goal(row)
        stages = [_row_stage(item) for item in
                  conn.execute('SELECT * FROM stages WHERE goal_id = ? ORDER BY ordinal, id', (goal_id,))]
    return goal, stages


def _finish_goal(goal, stages):
    goal['stages'] = [stage_view(item['id'], conn_stages=stages, goal_status=goal['status'])
                      for item in stages]
    goal['status_label'] = {'active': '进行中', 'completed': '已完成', 'cancelled': '已取消'}[goal['status']]
    goal['note'] = STAGES_NOTE
    return goal


def get_goal(goal_id, project=None):
    """Full goal view.

    With an explicit ``project`` the project's own store is authoritative and the
    global locator is not required.  A row physically stored in this project but
    labelled with another project is an identity conflict and is refused; a row
    that only exists in the legacy store is returned so the caller's scope check
    can reject it, instead of it silently disappearing.
    """
    if project:
        project = project_storage.normalize(project)
        goal, stages = _read_goal(project, goal_id)
        if goal is not None:
            if _conflicting(goal.get('project'), project):
                raise StageError('目标 %s 的项目标识指向其他项目，拒绝读取' % goal_id)
        else:
            goal, stages = _read_goal('', goal_id)
        if goal is None:
            raise StageError('目标不存在')
    else:
        store = _project_of_goal(goal_id)
        goal, stages = _read_goal(store, goal_id)
        if goal is None:
            raise StageError('目标不存在')
    return _finish_goal(goal, stages)


def list_goals(project=None):
    """Every live goal once, or only one explicit project's goals.

    A project view reads that project's own records.db directly and adds legacy
    rows whose real ``project`` resolves to the same directory, so neither the
    locator index nor another project's history is read.
    """
    if project is not None:
        project, stores = _scoped_stores(project)
    else:
        project, stores = None, _all_projects() + ['']
    seen = {}
    for store in stores:
        owned = bool(project is not None and store == project)
        with connection(store) as conn:
            rows = [_row_goal(row) for row in conn.execute('SELECT * FROM goals ORDER BY created_at DESC')]
            counts = {row['goal_id']: row for row in conn.execute(
                'SELECT goal_id, COUNT(*) AS total, '
                'SUM(CASE WHEN status IN ("accepted", "final_accepted") THEN 1 ELSE 0 END) AS accepted '
                'FROM stages GROUP BY goal_id')}
        for goal in rows:
            if goal['id'] in seen:
                continue
            if project is not None and not owned and not _belongs(goal.get('project'), project):
                continue
            if project is not None and owned and _conflicting(goal.get('project'), project):
                raise StageError('目标 %s 的项目标识指向其他项目，拒绝在当前项目读取' % goal['id'])
            summary = counts.get(goal['id'])
            goal['stage_count'] = int(summary['total']) if summary else 0
            goal['accepted_count'] = int(summary['accepted'] or 0) if summary else 0
            seen[goal['id']] = goal
    return sorted(seen.values(), key=lambda row: str(row.get('created_at') or ''), reverse=True)


def set_goal_status(goal_id, status, actor='main', project=None):
    """Change one goal's status.

    An explicit ``project`` resolves the goal from that project's own store, so
    a project-scoped request does not need the global locator to find its goal.
    """
    if status not in GOAL_STATES:
        raise StageError('目标状态不正确')
    store = _store_holding('goal', goal_id, project)
    with connection(store) as conn, _transaction(conn):
        with conn:
            row = conn.execute('SELECT * FROM goals WHERE id = ?', (goal_id,)).fetchone()
            if row is None:
                raise StageError('目标不存在')
            if row['status'] == GOAL_CANCELLED and status != GOAL_CANCELLED:
                raise StageError('目标已取消，不能改回其他状态；请新建目标')
            stages = conn.execute('SELECT status FROM stages WHERE goal_id = ?', (goal_id,)).fetchall()
            if status == GOAL_COMPLETED:
                if not stages:
                    raise StageError('目标没有任何阶段，不能标记为完成；完成需要至少一个独立验收通过的阶段')
                pending = [item['status'] for item in stages if item['status'] not in ACCEPTED_STATES]
                if pending:
                    raise StageError('目标完成需要全部阶段独立验收通过；仍有阶段未通过：' +
                                     '、'.join(sorted({STAGE_LABELS.get(state, state) for state in pending})))
            conn.execute('UPDATE goals SET status = ?, updated_at = ? WHERE id = ?',
                         (status, registry.now(), goal_id))
            _log(conn, action='goal_status', actor=actor, goal_id=goal_id, detail=status)
    return get_goal(goal_id, store or None)


# ----------------------------------------------------------------- stages API

def add_stage(goal_id, title, objective='', depends_on=(), files=(), acceptance=(),
              candidates=(), fixed_model='', project=None, session_policy=SESSION_NEW,
              kind=KIND_DEFAULT, actor='main'):
    """Add one stage to a goal.

    ``project`` is the explicit project hint (also the stage's project when it
    differs from the goal's, which is then refused); it lets a project-scoped
    request find its goal without the global locator.
    """
    title = _clean_text(title, 200)
    if not title:
        raise StageError('请填写阶段标题')
    objective = _clean_text(objective)
    if not objective:
        raise StageError('请填写阶段目标（清楚的目标描述）')
    if session_policy not in SESSION_POLICIES:
        raise StageError('会话策略不正确')
    if kind not in STAGE_KINDS:
        raise StageError('阶段类型只能是只读分析 chat 或完全权限修改 edit')
    depends_on = _clean_items(depends_on, limit=MAX_STAGES, item_limit=120)
    files = _clean_files(files)
    acceptance = _clean_items(acceptance, limit=40, item_limit=500)
    if not acceptance:
        raise StageError('请填写至少一条可判定的验收条件')
    candidates = _clean_items(candidates, limit=50, item_limit=120)
    fixed_model = _clean_text(fixed_model, 120)
    prefix = 'goal'
    store = _store_holding('goal', goal_id, project)
    with connection(store) as conn, _transaction(conn):
        with conn:
            goal = conn.execute('SELECT * FROM goals WHERE id = ?', (goal_id,)).fetchone()
            if goal is None:
                raise StageError('目标不存在')
            prefix = goal_id
            total = conn.execute('SELECT COUNT(*) AS n FROM stages WHERE goal_id = ?', (goal_id,)).fetchone()['n']
            if total >= MAX_STAGES:
                raise StageError('单个目标最多 %d 个阶段' % MAX_STAGES)
            known = {row['id']: _row_stage(row) for row in
                     conn.execute('SELECT * FROM stages WHERE goal_id = ?', (goal_id,))}
            clean_deps = []
            for dep in depends_on:
                if dep == 'self' or dep not in known:
                    raise StageError('前置阶段不在同一目标中：' + dep)
                if dep not in clean_deps:
                    clean_deps.append(dep)
            merged_project = _clean_text(project if project is not None else goal['project'], 500)
            if merged_project:
                merged_project = str(Path(merged_project).resolve())
            if goal['project'] and merged_project and merged_project != goal['project']:
                raise StageError('阶段项目需要与目标项目一致')
            ordinal = int(conn.execute('SELECT COALESCE(MAX(ordinal), 0) AS m FROM stages WHERE goal_id = ?',
                                       (goal_id,)).fetchone()['m']) + 1
            stage_id = _new_id('stage')
            # A stage with no prerequisites is immediately ready; others wait for
            # their prerequisites' independent acceptance.
            initial = PENDING if clean_deps else READY
            now = registry.now()
            conn.execute('INSERT INTO stages (id, goal_id, ordinal, title, objective, kind, project, files, acceptance, '
                         'candidates, fixed_model, depends_on, status, session_policy, session_key, task_ids, '
                         'active_task_id, created_at, updated_at, data) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "", "[]", "", ?, ?, "{}")',
                         (stage_id, goal_id, ordinal, title, objective, kind, merged_project,
                          json.dumps(files, ensure_ascii=False), json.dumps(acceptance, ensure_ascii=False),
                          json.dumps(candidates, ensure_ascii=False), fixed_model,
                          json.dumps(clean_deps, ensure_ascii=False), initial, session_policy, now, now))
            _log(conn, action='stage_add', actor=actor, goal_id=goal_id, stage_id=stage_id, detail=title)
    _register_stage(stage_id, store)
    return get_stage(stage_id)


def _read_stage(store, stage_id):
    with connection(store) as conn:
        row = conn.execute('SELECT * FROM stages WHERE id = ?', (stage_id,)).fetchone()
    return _row_stage(row) if row is not None else None


def get_stage(stage_id, project=None):
    """One stage, optionally resolved inside an explicit project.

    With a project the project's own store is authoritative and no locator index
    is required; a row stored here but labelled with another project is refused.
    A row that only exists in the legacy store is still returned so the caller's
    scope check can reject it explicitly.
    """
    if project:
        project = project_storage.normalize(project)
        stage = _read_stage(project, stage_id)
        if stage is not None:
            if _conflicting(stage.get('project'), project):
                raise StageError('阶段 %s 的项目标识指向其他项目，拒绝读取' % stage_id)
            return stage
        return _read_stage('', stage_id)
    return _read_stage(_project_of_stage(stage_id), stage_id)


def list_stages(goal_id=None, project=None):
    if project is not None:
        project, stores = _scoped_stores(project)
    else:
        project, stores = None, _all_projects() + ['']
    if goal_id:
        seen = {}
        for store in stores:
            with connection(store) as conn:
                rows = conn.execute('SELECT * FROM stages WHERE goal_id = ? ORDER BY ordinal, id', (goal_id,))
                for row in rows:
                    stage = _row_stage(row)
                    seen.setdefault(stage['id'], stage)
        return list(seen.values())
    seen = {}
    for store in stores:
        owned = bool(project is not None and store == project)
        with connection(store) as conn:
            rows = [_row_stage(row) for row in conn.execute('SELECT * FROM stages ORDER BY goal_id, ordinal, id')]
        for stage in rows:
            if stage['id'] in seen:
                continue
            if project is not None and not owned and not _belongs(stage.get('project'), project):
                continue
            if project is not None and owned and _conflicting(stage.get('project'), project):
                raise StageError('阶段 %s 的项目标识指向其他项目，拒绝在当前项目读取' % stage['id'])
            seen[stage['id']] = stage
    return list(seen.values())


def _dependency_states(conn, stage):
    states = {}
    titles = {}
    for dep in stage['depends_on']:
        row = conn.execute('SELECT id, status, title FROM stages WHERE id = ?', (dep,)).fetchone()
        if row is None:
            states[dep] = ''
            titles[dep] = ''
        else:
            states[dep] = row['status']
            titles[dep] = row['title']
    return states, titles


def readiness(stage_or_id, project=None):
    """Decidable dispatch precondition, independent of any model call."""
    stage_id = stage_or_id.get('id') if isinstance(stage_or_id, dict) else stage_or_id
    store = _store_holding('stage', stage_id, project)
    with connection(store) as conn:
        stage = stage_or_id if isinstance(stage_or_id, dict) else _stage(conn, stage_or_id)
        return _readiness(conn, stage)


def _stage(conn, stage_id):
    row = conn.execute('SELECT * FROM stages WHERE id = ?', (stage_id,)).fetchone()
    if row is None:
        raise StageError('阶段不存在')
    return _row_stage(row)


def _goal_status(conn, goal_id):
    row = conn.execute('SELECT status FROM goals WHERE id = ?', (goal_id,)).fetchone()
    return row['status'] if row else ''


def _readiness_reasons(stage, states, titles, goal_status=''):
    """Decidable dispatch preconditions shared by the live and snapshot views.

    The owning goal is part of the precondition: a cancelled or completed goal
    can never become a ready dispatch source, even if a stale task record would
    otherwise look accepted.
    """
    blocked_by = []
    reasons = []
    for dep, state in states.items():
        if state not in ACCEPTED_STATES:
            blocked_by.append(dep)
            reasons.append('前置阶段尚未独立验收通过：' + (titles.get(dep) or dep))
    if goal_status == GOAL_CANCELLED:
        reasons.append('目标已取消，阶段不能再派发')
    if goal_status == GOAL_COMPLETED:
        reasons.append('目标已完成，阶段不能再派发')
    if stage['status'] in ACTIVE_STATES:
        reasons.append('阶段已有进行中的任务：' + (stage.get('active_task_id') or '未知'))
    if stage['status'] == RECOVERY_PENDING:
        reasons.append('上次派发未完成，需要主脑显式恢复，不能自动重发')
    if stage['status'] == BLOCKED:
        reasons.append('阶段已记录受阻，需要主脑决定')
    if stage['status'] == CANCELLED:
        reasons.append('阶段已取消')
    if stage['status'] == ACCEPTED or stage['status'] == FINAL_ACCEPTED:
        reasons.append('阶段已经验收通过')
    if stage['status'] not in DISPATCHABLE_STATES:
        reasons.append('阶段当前状态不可派发：' + STAGE_LABELS.get(stage['status'], stage['status']))
    return blocked_by, reasons


def _readiness(conn, stage):
    states, titles = _dependency_states(conn, stage)
    blocked_by, reasons = _readiness_reasons(stage, states, titles,
                                             _goal_status(conn, stage['goal_id']))
    return {'stage_id': stage['id'], 'ready': not reasons, 'blocked_by': blocked_by,
            'reasons': reasons, 'dependency_states': states,
            'status': stage['status'], 'status_label': STAGE_LABELS.get(stage['status'], stage['status'])}


def claim_dispatch(stage_id, task_id, actor='main', mode='auto', project=None):
    """Atomically reserve one task for a stage.  Only one claimer can win.

    Raises ValueError when prerequisites are not accepted, when the stage is not
    dispatchable, or when another task is already active for the stage.
    Re-claiming the same task id is idempotent.  An explicit ``project`` locates
    the stage's own store directly instead of relying on the locator index.
    """
    task_id = _clean_text(task_id, 120)
    if not task_id:
        raise StageError('缺少任务 ID')
    store = _store_holding('stage', stage_id, project)
    with connection(store) as conn, _transaction(conn):
        with conn:
            stage = _stage(conn, stage_id)
            if stage['active_task_id'] == task_id and stage['status'] in ACTIVE_STATES:
                return stage
            if stage['active_task_id'] and stage['status'] in ACTIVE_STATES:
                raise StageError('阶段已派发任务：' + stage['active_task_id'])
            state = _readiness(conn, stage)
            if stage['status'] == RETURNED and mode != 'rework':
                raise StageError('退回阶段必须通过 rework-of 派发，并保持整改次数限制')
            if not state['ready']:
                raise StageError('阶段不满足派发条件：' + '；'.join(state['reasons']))
            task_ids = list(stage['task_ids'])
            if task_id not in task_ids:
                task_ids.append(task_id)
            session_key = stage['session_key'] or ('desk-' + task_id)
            conn.execute('UPDATE stages SET status = ?, active_task_id = ?, task_ids = ?, session_key = ?, '
                         'updated_at = ?, data = ? WHERE id = ?',
                         (DISPATCHED, task_id, json.dumps(task_ids, ensure_ascii=False), session_key,
                          registry.now(), json.dumps(stage['data'], ensure_ascii=False), stage_id))
            _log(conn, action='dispatch', actor=actor, goal_id=stage['goal_id'], stage_id=stage_id,
                 task_id=task_id, detail='mode=' + _clean_text(mode, 20))
            return _stage(conn, stage_id)


def dispatch_precheck(stage_id, mode='auto', project=None):
    """Read-only mirror of ``claim_dispatch``'s refusals.

    Used before a rework slot is marked used: a rejection that can still be
    repaired must happen before anything durable is consumed.  Writes nothing.
    """
    store = _store_holding('stage', stage_id, project)
    with connection(store) as conn:
        stage = _stage(conn, stage_id)
        reasons = []
        if stage['active_task_id'] and stage['status'] in ACTIVE_STATES:
            reasons.append('阶段已派发任务：' + stage['active_task_id'])
        elif stage['status'] == RETURNED and mode != 'rework':
            reasons.append('退回阶段必须通过 rework-of 派发，并保持整改次数限制')
        else:
            state = _readiness(conn, stage)
            if not state['ready']:
                reasons.append('阶段不满足派发条件：' + '；'.join(state['reasons']))
        return {'stage_id': stage_id, 'ready': not reasons, 'reasons': reasons,
                'status': stage['status'],
                'status_label': STAGE_LABELS.get(stage['status'], stage['status']),
                'active_task_id': stage['active_task_id']}


def requeue_stage(stage_id, reason, actor='main', project=None):
    """Explicitly release a failed/blocked/recovery_pending stage for dispatch."""
    reason = _clean_text(reason, 1000)
    if not reason:
        raise StageError('请写明恢复/重新派发的原因')
    with connection(_store_holding('stage', stage_id, project)) as conn, _transaction(conn):
        with conn:
            stage = _stage(conn, stage_id)
            goal_status = _goal_status(conn, stage['goal_id'])
            if goal_status in {GOAL_CANCELLED, GOAL_COMPLETED}:
                raise StageError('目标已取消或已完成，不能重新派发阶段；请新建目标')
            if stage['status'] not in REQUEUEABLE_STATES:
                raise StageError('当前状态不需要重新排队：' + STAGE_LABELS.get(stage['status'], stage['status']))
            state = _readiness(conn, {**stage, 'status': READY})
            target = READY if state['ready'] else PENDING
            conn.execute('UPDATE stages SET status = ?, active_task_id = "", updated_at = ? WHERE id = ?',
                         (target, registry.now(), stage_id))
            _log(conn, action='requeue', actor=actor, goal_id=stage['goal_id'], stage_id=stage_id, detail=reason)
            return _stage(conn, stage_id)


def cancel_stage(stage_id, reason, actor='main', project=None):
    """Record a durable cancellation; never silent."""
    reason = _clean_text(reason, 1000)
    if not reason:
        raise StageError('请写明取消原因')
    with connection(_store_holding('stage', stage_id, project)) as conn, _transaction(conn):
        with conn:
            stage = _stage(conn, stage_id)
            if stage['status'] in ACCEPTED_STATES:
                raise StageError('阶段已通过验收，不能取消')
            conn.execute('UPDATE stages SET status = ?, active_task_id = "", updated_at = ? WHERE id = ?',
                         (CANCELLED, registry.now(), stage_id))
            _log(conn, action='cancel', actor=actor, goal_id=stage['goal_id'], stage_id=stage_id,
                 task_id=stage['active_task_id'], detail=reason)
            return _stage(conn, stage_id)


def log_entries(stage_id=None, goal_id=None, limit=50, project=None):
    if stage_id or goal_id:
        if project:
            stores = _scoped_stores(project)[1]
        else:
            stores = [_project_of_stage(stage_id) if stage_id else _project_of_goal(goal_id)]
        seen = {}
        for store in stores:
            with connection(store) as conn:
                if stage_id:
                    rows = conn.execute('SELECT * FROM stage_log WHERE stage_id = ? ORDER BY seq DESC LIMIT ?',
                                        (stage_id, limit))
                else:
                    rows = conn.execute('SELECT * FROM stage_log WHERE goal_id = ? ORDER BY seq DESC LIMIT ?',
                                        (goal_id, limit))
                for row in rows:
                    item = dict(row)
                    seen.setdefault(item.get('seq'), item)
        out = [item for item in seen.values() if item is not None]
        out.sort(key=lambda row: (str(row.get('at') or ''), int(row.get('seq') or 0)), reverse=True)
        return out[:limit]
    rows = []
    stores = _scoped_stores(project)[1] if project else _all_projects() + ['']
    for store in stores:
        with connection(store) as conn:
            rows.extend(dict(row) for row in
                        conn.execute('SELECT * FROM stage_log ORDER BY seq DESC LIMIT ?', (limit,)))
    rows.sort(key=lambda row: (str(row.get('at') or ''), int(row.get('seq') or 0)), reverse=True)
    return rows[:limit]


# ------------------------------------------------- reconciliation and recovery

def _task_state(run):
    path = Path(run) / 'task.json'
    if not path.is_file():
        return None
    try:
        data = registry.read_json(path)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _latest_task(stage, project=None):
    if not stage['task_ids']:
        return None, None
    for task_id in reversed(stage['task_ids']):
        try:
            run = registry.task_dir(task_id, project)
        except ValueError:
            continue
        state = _task_state(run)
        if state is not None:
            return task_id, state
    return stage['task_ids'][-1], None


def _state_from_task(task, record):
    if record is not None:
        state = record.get('state')
        if state == delegation_policy.ACCEPTED:
            return ACCEPTED
        if state == getattr(delegation_policy, 'FINAL_ACCEPTED', 'final_accepted'):
            return FINAL_ACCEPTED
        if state == delegation_policy.RETURNED:
            return RETURNED
        if state == delegation_policy.BLOCKED:
            return BLOCKED
        if state == delegation_policy.EXECUTOR_FAILED:
            return FAILED
        if state == delegation_policy.EXECUTOR_COMPLETED:
            return EXECUTOR_COMPLETED
    status = str((task or {}).get('status') or '')
    if status == RECOVERY_PENDING:
        # A task awaiting explicit manual recovery is still an unfinished,
        # active dispatch: its stage must stay active (never look completed and
        # never be dispatched again automatically).
        return RUNNING
    if status in {'queued', 'running'}:
        return RUNNING
    if status == 'completed':
        return EXECUTOR_COMPLETED
    if status in {'failed', 'cancelled'}:
        return FAILED
    return None


def acceptance_mismatch(stage, record):
    """True when a stored review decided different conditions than the stage.

    Dispatch always writes the stage's persisted acceptance into the task and
    records a durable ``stage_policy`` marker, so a mismatch on such a task means
    the record was produced for other conditions (tampered, foreign or legacy).
    Callers only apply this to tasks that carry that authoritative marker, so
    direct/legacy records are never reinterpreted by reconciliation.
    """
    if not isinstance(record, dict):
        return False
    criteria = record.get('criteria')
    if not isinstance(criteria, list):
        return False
    texts = []
    for item in criteria:
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get('text'), 500)
        if text and text not in texts:
            texts.append(text)
    return texts != list(stage.get('acceptance') or [])


def _restart_marker(task):
    text = str((task or {}).get('phase') or '') + ' ' + str((task or {}).get('error') or '')
    return any(token in text for token in ('重启', '未执行', '意外结束', '进程已结束'))


def sync_stage(stage, task, record, goal_status=None):
    """Return the stage status implied by the linked task, or None to keep it.

    Two durable records are never overwritten by reconciliation: a stage that
    was explicitly cancelled, and any stage of a cancelled/completed goal.
    """
    if stage['status'] == CANCELLED:
        return None
    if goal_status in {GOAL_CANCELLED, GOAL_COMPLETED}:
        return None
    state = _state_from_task(task, record)
    if stage['status'] == RECOVERY_PENDING:
        # Only a real terminal result clears the recovery marker.
        if state in {ACCEPTED, FINAL_ACCEPTED, RETURNED, BLOCKED}:
            return state
        return None
    return state


def _sync_store(conn, goal_id, changed):
    with conn:
        goals = {row['id']: row['status'] for row in conn.execute('SELECT id, status FROM goals')}
        if goal_id:
            rows = conn.execute('SELECT * FROM stages WHERE goal_id = ? ORDER BY ordinal', (goal_id,))
        else:
            rows = conn.execute('SELECT * FROM stages ORDER BY goal_id, ordinal')
        for row in rows:
            stage = _row_stage(row)
            task_id, task = _latest_task(stage)
            record = None
            if task_id:
                try:
                    record = delegation_policy.load(registry.task_dir(task_id))
                except (OSError, ValueError):
                    record = None
            target = sync_stage(stage, task, record, goal_status=goals.get(stage['goal_id']))
            # Only stage-dispatched tasks carry the authoritative
            # ``stage_policy`` marker; their review must decide the stage's
            # own conditions.  Direct/legacy records keep reconciling.
            mismatch = bool(target in ACCEPTED_STATES and record
                            and isinstance((task or {}).get('stage_policy'), dict)
                            and acceptance_mismatch(stage, record))
            if mismatch:
                # The review was decided against other conditions: keep the
                # executor completion and require a real independent review
                # instead of silently accepting a replaced condition set.
                target = EXECUTOR_COMPLETED
            if target and target != stage['status']:
                active = task_id if target in ACTIVE_STATES else ''
                conn.execute('UPDATE stages SET status = ?, updated_at = ?, active_task_id = ? WHERE id = ?',
                             (target, registry.now(), active, stage['id']))
                _log(conn, action='sync', goal_id=stage['goal_id'], stage_id=stage['id'],
                     task_id=task_id or '',
                     detail=stage['status'] + '->' + target +
                            (' · 验收条件与阶段保存值不一致，拒绝标记通过' if mismatch else ''))
                changed.append({'stage_id': stage['id'], 'from': stage['status'], 'to': target,
                                'task_id': task_id, 'criteria_mismatch': mismatch})
            elif mismatch:
                _log(conn, action='sync_blocked', goal_id=stage['goal_id'], stage_id=stage['id'],
                     task_id=task_id or '', detail='验收条件与阶段保存值不一致，拒绝标记通过')
                changed.append({'stage_id': stage['id'], 'from': stage['status'], 'to': stage['status'],
                                'task_id': task_id, 'criteria_mismatch': True})
    return changed


def sync_from_tasks(goal_id=None, project=None):
    """Reconcile persisted stages with the real task/review records.

    With no ``goal_id`` every live store is reconciled (project stores first,
    then the legacy global store); each stage is written back to its owner.  An
    explicit ``project`` is consulted first so a scoped request does not depend
    on the global locator to find its own goal.
    """
    changed = []
    if goal_id:
        stores = []
        for store in ([project_storage.normalize(project)] if project else []) + [_project_of_goal(goal_id), '']:
            if store not in stores:
                stores.append(store)
    else:
        stores = _all_projects() + ['']
    for store in stores:
        with connection(store) as conn, _transaction(conn):
            _sync_store(conn, goal_id, changed)
    return {'changed': changed, 'note': STAGES_NOTE}


def _recover_store(conn, actor, actions):
    with conn:
        goals = {row['id']: row['status'] for row in conn.execute('SELECT id, status FROM goals')}
        rows = conn.execute('SELECT * FROM stages WHERE status IN ("dispatched", "running")').fetchall()
        for row in rows:
            stage = _row_stage(row)
            if goals.get(stage['goal_id']) in {GOAL_CANCELLED, GOAL_COMPLETED}:
                # A cancelled/completed goal stays as recorded; recovery never
                # revives it or turns it into a new pending dispatch.
                continue
            task_id, task = _latest_task(stage)
            if task is None:
                reason = '找不到阶段对应的任务记录，需主脑确认后再派发'
            elif task.get('status') == 'recovery_pending':
                reason = ('对应任务已标记为待恢复（重启或执行进程消失），'
                          '需主脑确认后显式恢复，不自动重发')
            elif task.get('status') in {'queued', 'running'}:
                reason = '面板重启时任务仍未结束，需主脑确认后再派发，不自动重发'
            elif _restart_marker(task):
                reason = '上次执行在完成前中断，需主脑确认后再派发，不自动重发'
            else:
                continue
            conn.execute('UPDATE stages SET status = ?, updated_at = ?, data = ? WHERE id = ?',
                         (RECOVERY_PENDING, registry.now(),
                          json.dumps({**stage['data'], 'recovery_reason': reason}, ensure_ascii=False),
                          stage['id']))
            _log(conn, action='recovery_pending', actor=actor, goal_id=stage['goal_id'],
                 stage_id=stage['id'], task_id=task_id or '', detail=reason)
            actions.append({'stage_id': stage['id'], 'task_id': task_id, 'reason': reason})
    return actions


def recover(actor='main'):
    """Mark unfinished dispatches as recovery_pending instead of re-sending them.

    The panel calls this once at startup, after it has already reconciled the
    real queued/running task records.  Every live store is checked; nothing is
    dispatched here.
    """
    actions = []
    for store in _all_projects() + ['']:
        with connection(store) as conn, _transaction(conn):
            _recover_store(conn, actor, actions)
    return {'actions': actions, 'pending': pending_recovery(), 'note': STAGES_NOTE}


def mark_recovery_pending(stage_id, reason, actor='main', project=None):
    """Mark exactly one stage ``recovery_pending`` for an unfinished linked task.

    Used by an explicit task recovery: it never touches another stage, never
    dispatches anything, and refuses when the linked task already ended.
    """
    reason = _clean_text(reason, 1000)
    if not reason:
        raise StageError('请写明待恢复原因')
    with connection(_store_holding('stage', stage_id, project)) as conn, _transaction(conn):
        with conn:
            stage = _stage(conn, stage_id)
            goal_status = _goal_status(conn, stage['goal_id'])
            if goal_status in {GOAL_CANCELLED, GOAL_COMPLETED}:
                raise StageError('目标已取消或已完成，不能恢复阶段；请新建目标')
            if stage['status'] not in ACTIVE_STATES | {RECOVERY_PENDING}:
                raise StageError('阶段当前不需要恢复：' + STAGE_LABELS.get(stage['status'], stage['status']))
            task_id, task = _latest_task(stage, project)
            if task is not None and task.get('status') not in {'recovery_pending', 'queued', 'running'}:
                raise StageError('阶段对应任务已经结束，不接受待恢复标记')
            conn.execute('UPDATE stages SET status = ?, updated_at = ?, data = ? WHERE id = ?',
                         (RECOVERY_PENDING, registry.now(),
                          json.dumps({**stage['data'], 'recovery_reason': reason}, ensure_ascii=False),
                          stage_id))
            _log(conn, action='recovery_pending', actor=actor, goal_id=stage['goal_id'],
                 stage_id=stage_id, task_id=task_id or '', detail=reason)
            return _stage(conn, stage_id)


def pending_recovery(project=None):
    out = []
    seen = set()
    if project is not None:
        project, stores = _scoped_stores(project)
    else:
        project, stores = None, _all_projects() + ['']
    for store in stores:
        owned = bool(project is not None and store == project)
        with connection(store) as conn:
            rows = conn.execute('SELECT * FROM stages WHERE status = ? ORDER BY updated_at DESC',
                                (RECOVERY_PENDING,)).fetchall()
        for row in rows:
            stage = _row_stage(row)
            if stage['id'] in seen:
                continue
            if project is not None and not owned and not _belongs(stage.get('project'), project):
                continue
            seen.add(stage['id'])
            task_id, task = _latest_task(stage, project)
            out.append({'stage_id': stage['id'], 'goal_id': stage['goal_id'], 'title': stage['title'],
                        'task_id': task_id or stage['active_task_id'],
                        'task_status': (task or {}).get('status'),
                        'reason': stage['data'].get('recovery_reason') or '需要主脑确认后显式恢复'})
    return out


# ------------------------------------------------------------------ reporting

def stage_view(stage_id, conn_stages=None, goal_status=None, project=None):
    """Full stage view: dependencies, readiness, tasks, delivery and recovery."""
    store = _store_holding('stage', stage_id, project)
    if conn_stages is not None:
        stage = next((item for item in conn_stages if item['id'] == stage_id), None)
        if stage is None:
            raise StageError('阶段不存在')
        # Readiness needs dependency states from the same goal snapshot.
        states = {}
        titles = {}
        for dep in stage['depends_on']:
            match = next((item for item in conn_stages if item['id'] == dep), None)
            states[dep] = match['status'] if match else ''
            titles[dep] = match['title'] if match else ''
        if goal_status is None:
            with connection(store) as conn:
                goal_status = _goal_status(conn, stage['goal_id'])
        blocked_by, reasons = _readiness_reasons(stage, states, titles, goal_status)
        ready = {'stage_id': stage_id, 'ready': not reasons, 'blocked_by': blocked_by,
                 'reasons': reasons, 'dependency_states': states,
                 'status': stage['status'], 'status_label': STAGE_LABELS.get(stage['status'], stage['status'])}
        return _decorate_stage(stage, ready, project=store or None)
    with connection(store) as conn:
        stage = _stage(conn, stage_id)
        return _decorate_stage(stage, _readiness(conn, stage), project=store or None)


def _decorate_stage(stage, ready, project=None):
    tasks = []
    for task_id in stage['task_ids']:
        entry = {'task_id': task_id}
        try:
            run = registry.task_dir(task_id, project)
        except ValueError:
            tasks.append(entry)
            continue
        state = _task_state(run)
        if state:
            entry.update({'status': state.get('status'), 'model': state.get('model'),
                          'model_id': state.get('model_id'), 'phase': state.get('phase'),
                          'changed_files': state.get('changed_files') or [],
                          'dsh_session_id': state.get('dsh_session_id'),
                          'created_at': state.get('created_at')})
        try:
            record = delegation_policy.load(run)
            review_error = ''
        except (OSError, ValueError) as error:
            record = None
            review_error = str(error) or '验收记录无法读取'
        if record:
            entry['review_state'] = record.get('state')
            entry['review_label'] = delegation_policy.STATE_LABELS.get(record.get('state'))
        elif review_error:
            # A corrupt/unreadable record stays explicitly unknown; it is never
            # shown as accepted and never treated as "no review yet".
            entry['review_state'] = 'unreadable'
            entry['review_label'] = '验收记录无法读取'
            entry['review_error'] = review_error
        else:
            entry['review_state'] = None
            entry['review_label'] = None
        tasks.append(entry)
    delivery = stage_delivery(stage, project)
    stage = {
        **stage,
        'kind': stage.get('kind') or KIND_DEFAULT,
        'kind_label': KIND_LABELS.get(stage.get('kind') or KIND_DEFAULT),
        'status_label': STAGE_LABELS.get(stage['status'], stage['status']),
        'readiness': ready,
        'tasks': tasks,
        'delivery': delivery,
        'note': STAGES_NOTE,
    }
    return stage


def stage_delivery(stage, project=None):
    """Persisted delivery index for a stage: accepted handovers only."""
    import task_handover
    deliveries = []
    warnings = []
    for task_id in stage['task_ids']:
        try:
            run = registry.task_dir(task_id, project)
        except ValueError:
            continue
        record, error = task_handover.safe_load(run)
        if error:
            warnings.append({task_id: error})
            continue
        if not record:
            continue
        fresh = task_handover.verify(run, record)
        deliveries.append({'task_id': task_id, 'stage_id': record.get('stage_id'),
                           'goal_id': record.get('goal_id'), 'summary': record.get('summary'),
                           'files': record.get('files') or [], 'interfaces': record.get('interfaces') or [],
                           'decisions': record.get('decisions') or [],
                           'remaining_issues': record.get('remaining_issues') or [],
                           'verification': record.get('verification') or {},
                           'fresh': fresh})
    return {'stage_id': stage['id'], 'deliveries': deliveries, 'warnings': warnings}


def stats(project=None):
    if project is not None:
        goals = len(list_goals(project))
        stages = list_stages(project=project)
        counts = {}
        for row in stages:
            counts[row['status']] = counts.get(row['status'], 0) + 1
        total = len(stages)
    else:
        goals = 0
        counts = {}
        for store in _all_projects() + ['']:
            with connection(store) as conn:
                goals += int(conn.execute('SELECT COUNT(*) AS n FROM goals').fetchone()['n'])
                for row in conn.execute('SELECT status, COUNT(*) AS n FROM stages GROUP BY status'):
                    counts[row['status']] = counts.get(row['status'], 0) + int(row['n'])
        total = sum(counts.values())
    return {'goals': int(goals), 'stages': int(total), 'by_status': counts,
            'ready': counts.get(READY, 0), 'accepted': counts.get(ACCEPTED, 0) + counts.get(FINAL_ACCEPTED, 0),
            'recovery_pending': counts.get(RECOVERY_PENDING, 0), 'note': STAGES_NOTE}


# ------------------------------------------------------------------------- CLI

def _render(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description='Persistent goals and stages (local SQLite).')
    parser.add_argument('--list-goals', action='store_true')
    parser.add_argument('--list-stages', nargs='?', const='', default=None, metavar='GOAL')
    parser.add_argument('--goal')
    parser.add_argument('--stage')
    parser.add_argument('--create-goal', action='store_true')
    parser.add_argument('--add-stage', action='store_true')
    parser.add_argument('--set-goal-status', choices=sorted(GOAL_STATES))
    parser.add_argument('--title', default='')
    parser.add_argument('--objective', default='')
    parser.add_argument('--project', default='')
    parser.add_argument('--depends', default='', help='逗号分隔的前置阶段 ID')
    parser.add_argument('--files', default='', help='逗号分隔的项目内相对路径')
    parser.add_argument('--acceptance', default='', help='分号或换行分隔的验收条件')
    parser.add_argument('--candidates', default='', help='逗号分隔的候选模型 ID')
    parser.add_argument('--fixed-model', default='')
    parser.add_argument('--session-policy', choices=sorted(SESSION_POLICIES), default=SESSION_NEW)
    parser.add_argument('--kind', choices=list(STAGE_KINDS), default=KIND_DEFAULT,
                        help='阶段固定的任务类型：chat 只读分析 / edit 完全权限修改（默认 edit，兼容旧调用）')
    parser.add_argument('--sync', action='store_true')
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--requeue', action='store_true')
    parser.add_argument('--cancel', action='store_true')
    parser.add_argument('--reason', default='')
    parser.add_argument('--log', action='store_true')
    parser.add_argument('--stats', action='store_true')
    args = parser.parse_args(argv)

    def split(value):
        return [part.strip() for part in value.replace('；', ';').replace('\n', ';').replace('、', ',').replace(',', ';').split(';') if part.strip()]

    try:
        if args.stats:
            print(_render(stats())); return 0
        if args.create_goal:
            print(_render(create_goal(args.title, args.objective, args.project,
                                      store='project' if args.project else 'auto'))); return 0
        if args.add_stage:
            if not args.goal:
                raise StageError('请用 --goal 指定目标')
            print(_render(add_stage(args.goal, args.title, args.objective,
                                    depends_on=split(args.depends), files=split(args.files),
                                    acceptance=split(args.acceptance), candidates=split(args.candidates),
                                    fixed_model=args.fixed_model, project=args.project or None,
                                    session_policy=args.session_policy, kind=args.kind))); return 0
        if args.set_goal_status:
            if not args.goal:
                raise StageError('请用 --goal 指定目标')
            print(_render(set_goal_status(args.goal, args.set_goal_status))); return 0
        if args.sync:
            print(_render(sync_from_tasks(args.goal))); return 0
        if args.recover:
            print(_render(recover())); return 0
        if args.requeue:
            if not args.stage:
                raise StageError('请用 --stage 指定阶段')
            print(_render(requeue_stage(args.stage, args.reason))); return 0
        if args.cancel:
            if not args.stage:
                raise StageError('请用 --stage 指定阶段')
            print(_render(cancel_stage(args.stage, args.reason))); return 0
        if args.log:
            print(_render(log_entries(stage_id=args.stage, goal_id=args.goal))); return 0
        if args.list_goals:
            print(_render({'goals': list_goals(), 'stats': stats()})); return 0
        if args.list_stages is not None:
            print(_render({'stages': [stage_view(item['id']) for item in list_stages(args.list_stages or None)]})); return 0
        if args.goal:
            print(_render(get_goal(args.goal))); return 0
        if args.stage:
            print(_render(stage_view(args.stage))); return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    parser.error('请指定一个操作：--list-goals / --create-goal / --add-stage / --stage / --sync / --recover ...')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
