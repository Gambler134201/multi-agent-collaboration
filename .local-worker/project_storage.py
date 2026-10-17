"""Per-project workbench storage: one real directory owns its records.

Design invariants (the reason this module exists):

* A project is an explicit, existing local directory.  Nothing here ever guesses
  one from the process cwd, the installation directory or a "recent" list.  The
  recent-project bookmarks stay a convenience only and never decide ownership.
* A project owns ``<project>/.model-desk/records.db`` (structured tasks and their
  goal/stage/acceptance associations) and ``<project>/.model-desk/runs/`` (task
  records, prompts, logs, results, handovers).
* The installation keeps only a locator index
  (``.local-worker/project-index.json``) that maps record ids to a project key.
  It never stores a second full history; it is an index, not a copy.
* Opening/listing a project is read-only.  ``records.db``/``runs`` are created
  only by an explicit create/ensure call, so merely viewing an empty project
  never writes a hidden directory.
* ``task.json`` inside the owning runs directory is the **single authority** for
  task content.  ``records.db.tasks`` holds only the derived association row
  (id + goal/stage/kind); :func:`reconcile_tasks` rebuilds it from the runs
  directory and never overrides ``task.json``.
* A migrated legacy run is a backup, not a second live record: iteration skips a
  legacy id that already resolves to a project, so old records are never shown or
  counted twice.
* No code here reassigns a global ``RUNS`` or "current project" variable.  Every
  caller resolves an id through :func:`locate`/`resolve_task_dir` and stays inside
  the owning project.

Money caps are untouched here; the shared cost ledger, models, keys and workflow
switch stay global.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from uuid import uuid4

import project_scope as scope

DESK_DIR = '.model-desk'
RECORDS_NAME = 'records.db'
RUNS_NAME = 'runs'
INDEX_NAME = 'project-index.json'
INDEX_SCHEMA = 1

TASK_ID = re.compile(r'[A-Za-z0-9_-]{1,80}')
RECORD_KINDS = ('task', 'goal', 'stage')
_SECTION = {'task': 'tasks', 'goal': 'goals', 'stage': 'stages'}

RECORD_NOTE = ('项目目录下的 .model-desk/records.db 与 runs 属于该项目；全局只保留 '
               'id→项目 的定位索引，配置、密钥与费用总账仍然共享。')


class StorageError(ValueError):
    """A user-facing storage/ownership failure."""


# --------------------------------------------------------------- small helpers

def _registry():
    import sys
    return sys.modules.get('model_registry')


def install_dir():
    """The global install directory; follows a test-patched ``registry.HERE``."""
    module = _registry()
    here = getattr(module, 'HERE', None) if module is not None else None
    if here:
        return Path(here)
    return Path(__file__).resolve().parent


def index_path():
    return install_dir() / INDEX_NAME


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.04)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path):
    for attempt in range(6):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(.04)


@contextmanager
def _lock(path):
    """Cross-process file lock; bounded and explicit, never a silent no-op."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    try:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        if os.name == 'nt':
            import msvcrt
            deadline = time.monotonic() + 15
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise StorageError('项目索引正在被写入，请稍后重试')
                    time.sleep(.05)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                try:
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
    finally:
        handle.close()


def exclusive_lock(name):
    """Cross-process lock for one named workbench operation."""
    if not isinstance(name, str) or not TASK_ID.fullmatch(name):
        raise StorageError('锁名称无效：' + repr(name))
    return _lock(install_dir() / (name + '.lock'))


def _is_link(path):
    path = Path(path)
    if path.is_symlink():
        return True
    checker = getattr(os.path, 'isjunction', None)
    return bool(checker and checker(path))


# ------------------------------------------------------------ project identity

def _as_text(value):
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return value


def normalize(value):
    """Normalize an explicit existing local directory; raises on anything else."""
    return scope.normalize_project(_as_text(value))


def key(value):
    return scope.project_key(_as_text(value))


def usable(value):
    """Return the normalized path when ``value`` is an existing local dir, else None.

    This never raises and never creates anything, so a caller can keep the legacy
    fallback for an unknown/absent project instead of failing the whole request.
    """
    try:
        return scope.normalize_project(_as_text(value))
    except (ValueError, OSError, TypeError):
        return None


def desk_dir(project):
    return Path(project) / DESK_DIR


def records_db(project):
    return desk_dir(project) / RECORDS_NAME


def runs_dir(project):
    return desk_dir(project) / RUNS_NAME


def is_initialized(project):
    """True only when a real project store already exists (read-only check)."""
    path = usable(project)
    if path is None:
        return False
    if _is_link(desk_dir(path)):
        return False
    return records_db(path).is_file() or runs_dir(path).is_dir()


# --------------------------------------------------------------- locator index

def _empty_index():
    return {'schema': INDEX_SCHEMA, 'projects': {}, 'tasks': {}, 'goals': {}, 'stages': {}}


def index():
    """Read the global locator index.

    A missing index is empty.  A present-but-unreadable/corrupt index raises
    instead of silently reporting "no projects": hiding every project record
    would look like the history vanished, and overwriting the file could lose
    real ownership.
    """
    path = index_path()
    data = _empty_index()
    if not path.is_file():
        return data
    try:
        loaded = _read_json(path)
    except PermissionError as exc:
        raise StorageError('项目定位索引暂时不可读，请稍后重试：' + str(path)) from exc
    except (OSError, ValueError) as exc:
        raise StorageError('项目定位索引损坏，已拒绝继续（不会假装记录消失）：' + str(path)) from exc
    if not isinstance(loaded, dict):
        raise StorageError('项目定位索引格式损坏，已拒绝继续：' + str(path))
    data['schema'] = INDEX_SCHEMA
    for section in ('projects', 'tasks', 'goals', 'stages'):
        value = loaded.get(section)
        data[section] = value if isinstance(value, dict) else {}
    return data


def update_index(mutator):
    """Atomically read-modify-write the index under a cross-process lock."""
    def _run():
        data = index()
        result = mutator(data)
        _write_json(index_path(), data)
        return result
    with _lock(install_dir() / 'project-index.lock'):
        return _run()


def register_project(project, last_selected=None):
    project = normalize(project)
    project_key = key(project)

    def mutate(data):
        row = data['projects'].get(project_key)
        row = dict(row) if isinstance(row, dict) else {}
        row['path'] = project
        row.setdefault('created_at', _now())
        row['updated_at'] = _now()
        if last_selected:
            row['last_selected'] = last_selected
        data['projects'][project_key] = row
        return project
    return update_index(mutate)


def register(kind, record_id, project):
    """Point one record id at its owning project.  Ids stay unique globally.

    An id that already belongs to a *different* project is an ownership
    conflict, not a move: re-pointing it would silently relocate another
    project's record, so it raises instead.  Callers that really want to
    transfer a record must remove/rebuild the old owner explicitly first.
    """
    if kind not in _SECTION:
        raise StorageError('未知记录类型：' + str(kind))
    if not isinstance(record_id, str) or not record_id:
        raise StorageError('记录 id 无效')
    project = normalize(project)
    project_key = key(project)

    def mutate(data):
        current = data[_SECTION[kind]].get(record_id)
        if current and current != project_key:
            owner = data['projects'].get(current)
            owner = owner.get('path') if isinstance(owner, dict) else current
            raise StorageError('记录 %s 已归属项目 %s，拒绝静默改派到 %s'
                               % (record_id, owner or current, project))
        row = data['projects'].get(project_key)
        row = dict(row) if isinstance(row, dict) else {'created_at': _now()}
        row['path'] = project
        row['updated_at'] = _now()
        data['projects'][project_key] = row
        data[_SECTION[kind]][record_id] = project_key
    update_index(mutate)


def unregister(kind, record_id):
    if kind not in _SECTION:
        raise StorageError('未知记录类型：' + str(kind))

    def mutate(data):
        data[_SECTION[kind]].pop(record_id, None)
    update_index(mutate)


def locate(kind, record_id):
    """Return the owning project path for an id, or None for an unindexed/legacy id."""
    if kind not in _SECTION or not isinstance(record_id, str):
        return None
    data = index()
    project_key = data[_SECTION[kind]].get(record_id)
    if not project_key:
        return None
    row = data['projects'].get(project_key)
    path = row.get('path') if isinstance(row, dict) else None
    if not path:
        return None
    return path


def assert_owner(kind, record_id, project):
    """Re-assert one locator entry for a record proven to live in ``project``.

    Only called after the record was really read from that project's own store,
    so the mapping is proven, not guessed.  The locator is derived data: a
    missing entry is restored (so later by-id operations keep working), while an
    entry owned by a *different* project raises instead of being stolen.
    """
    if kind not in _SECTION:
        raise StorageError('未知记录类型：' + str(kind))
    project = normalize(project)
    project_key = key(project)
    current = locate(kind, record_id)
    if current:
        if key(current) == project_key:
            return False
        raise StorageError('记录 %s 已归属项目 %s，拒绝改派到 %s'
                           % (record_id, current, project))
    register(kind, record_id, project)
    return True


def project_row(project):
    return index()['projects'].get(key(project))


def indexed_projects(require_store=True):
    """Existing project directories that own a store (deduplicated, sorted)."""
    out = {}
    for row in index()['projects'].values():
        path = row.get('path') if isinstance(row, dict) else None
        normalized = usable(path)
        if normalized is None:
            continue
        if require_store and not is_initialized(normalized):
            continue
        out[scope.project_key(normalized)] = normalized
    return sorted(out.values(), key=lambda item: item.casefold())


def remembered_projects():
    rows = []
    for row in index()['projects'].values():
        if not isinstance(row, dict):
            continue
        path = usable(row.get('path'))
        if path is None:
            continue
        rows.append({'path': path, 'last_selected': row.get('last_selected'),
                     'created_at': row.get('created_at'), 'updated_at': row.get('updated_at')})
    rows.sort(key=lambda item: str(item.get('last_selected') or ''), reverse=True)
    return rows


# ----------------------------------------------------------------- project init

def ensure_schema(conn):
    """The one canonical schema shared by the legacy and per-project databases."""
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
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL DEFAULT '',
            stage_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS stages_goal ON stages (goal_id, ordinal);
        CREATE INDEX IF NOT EXISTS stage_log_stage ON stage_log (stage_id, seq);
        CREATE INDEX IF NOT EXISTS tasks_stage ON tasks (stage_id);
    ''')
    conn.execute('INSERT INTO meta (key, value) VALUES ("schema", ?) '
                 'ON CONFLICT(key) DO NOTHING', (str(INDEX_SCHEMA),))
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(stages)')}
    if 'kind' not in columns:
        try:
            conn.execute("ALTER TABLE stages ADD COLUMN kind TEXT NOT NULL DEFAULT 'edit'")
        except sqlite3.OperationalError as exc:
            if 'duplicate column' not in str(exc).lower():
                raise


def connect(path, create=True):
    """Open a records database; ``create=False`` on a missing file stays in memory."""
    path = Path(path)
    if not create and not path.is_file():
        conn = sqlite3.connect(':memory:', isolation_level=None)
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA busy_timeout=15000')
        try:
            conn.execute('PRAGMA journal_mode=WAL')
        except sqlite3.OperationalError:
            pass
        ensure_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def ensure_project(project):
    """Create ``.model-desk`` for a real project and register it.  First write only."""
    project = normalize(project)
    desk = desk_dir(project)
    if _is_link(desk):
        raise StorageError('项目记录目录是链接，拒绝使用：' + str(desk))
    desk.mkdir(parents=True, exist_ok=True)
    # Keep generated history out of Git without editing the project's own rules.
    # Exclusive create preserves an existing user-maintained ignore file.
    try:
        with (desk / '.gitignore').open('x', encoding='utf-8') as ignore:
            ignore.write('*\n!.gitignore\n')
    except FileExistsError:
        pass
    runs = runs_dir(project)
    runs.mkdir(parents=True, exist_ok=True)
    conn = connect(records_db(project), create=True)
    conn.close()
    register_project(project)
    return {'project': project, 'records_db': str(records_db(project)), 'runs': str(runs)}


# -------------------------------------------------------------------- task I/O

def task_dir(project, task_id):
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise StorageError('任务 id 无效：' + repr(task_id))
    return runs_dir(normalize(project)) / task_id


def legacy_runs():
    module = _registry()
    runs = getattr(module, 'RUNS', None) if module is not None else None
    return Path(runs) if runs else install_dir() / RUNS_NAME


def resolve_task_dir(task_id, project=None):
    """Locate one task strictly by id.

    With an explicit project its own runs directory wins when it really holds
    the record, so the locator index is an optimisation there.  An indexed task
    never falls back to a legacy backup copy, so a migrated record is not
    silently served twice.
    """
    if project:
        candidate = task_dir(project, task_id)
        if candidate.is_dir():
            return candidate
    owning = locate('task', task_id)
    if owning:
        return task_dir(owning, task_id)
    return legacy_runs() / task_id


def task_record_path(run):
    return Path(run) / 'task.json'


def read_task_record(run):
    path = task_record_path(run)
    if not path.is_file():
        raise FileNotFoundError('任务记录已删除或不存在')
    return json.loads(path.read_text(encoding='utf-8'))


def prepare_task_dir(task_id, project):
    """Initialize the project store (first write), then reserve one task run dir."""
    project = normalize(project)
    owning = locate('task', task_id)
    if owning and key(owning) != key(project):
        raise StorageError('任务 id %s 已归属项目 %s，拒绝在 %s 下创建同名记录'
                           % (task_id, owning, project))
    ensure_project(project)
    run = task_dir(project, task_id)
    run.mkdir(parents=True, exist_ok=True)
    register('task', task_id, project)
    register_task_record(project, task_id)
    return run


def register_task_record(project, task_id, goal_id='', stage_id='', kind='', created_at=None):
    """Upsert the derived association row; task.json remains the content authority."""
    conn = connect(records_db(project), create=True)
    try:
        now = created_at or _now()
        conn.execute(
            'INSERT INTO tasks (id, goal_id, stage_id, kind, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET goal_id=excluded.goal_id, stage_id=excluded.stage_id, '
            'kind=excluded.kind, updated_at=excluded.updated_at',
            (task_id, str(goal_id or ''), str(stage_id or ''), str(kind or ''), now, _now()))
    finally:
        conn.close()


def forget_task(task_id, project=None):
    """Remove the project association and locator entry for a deleted task."""
    owning = project or locate('task', task_id)
    if owning:
        path = records_db(owning)
        if path.is_file() and not _is_link(desk_dir(owning)):
            conn = connect(path, create=False)
            try:
                conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
            finally:
                conn.close()
    unregister('task', task_id)


def reconcile_tasks(project):
    """Rebuild ``records.db.tasks`` from the runs directory (derived mirror).

    A run whose id is already indexed to a *different* project is reported as a
    conflict instead of being silently re-assigned to this directory.
    """
    project = normalize(project)
    runs = runs_dir(project)
    seen = set()
    conflicts = []
    added = updated = removed = 0
    if runs.is_dir():
        for entry in runs.iterdir():
            if not entry.is_dir() or not TASK_ID.fullmatch(entry.name):
                continue
            record_path = entry / 'task.json'
            record = None
            if record_path.is_file():
                try:
                    record = json.loads(record_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    record = None
            record = record if isinstance(record, dict) else {}
            task_id = record.get('id') or entry.name
            if task_id != entry.name:
                continue
            seen.add(task_id)
            register_task_record(project, task_id, record.get('goal_id'),
                                 record.get('stage_id'), record.get('kind'),
                                 record.get('created_at'))
            try:
                register('task', task_id, project)
            except StorageError as exc:
                conflicts.append({'task_id': task_id, 'reason': str(exc)})
            added += 1
    conn = connect(records_db(project), create=False)
    try:
        rows = [row['id'] for row in conn.execute('SELECT id FROM tasks')]
        for task_id in rows:
            if task_id not in seen:
                conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
                unregister('task', task_id)
                removed += 1
    finally:
        conn.close()
    return {'project': project, 'present': len(seen), 'touched': added, 'removed': removed,
            'conflicts': conflicts}


# ---------------------------------------------------------------- enumeration

def task_roots():
    """Every live runs root: (project_or_None, path).  Legacy last."""
    roots = []
    for project in indexed_projects():
        runs = runs_dir(project)
        if runs.is_dir():
            roots.append((project, runs))
    runs = legacy_runs()
    if runs.is_dir():
        roots.append((None, runs))
    return roots


def _entries(root):
    """Task-id directories under one runs root, newest first; never raises."""
    try:
        items = list(Path(root).iterdir())
    except OSError:
        return []
    return sorted((item for item in items if item.is_dir() and TASK_ID.fullmatch(item.name)),
                  key=lambda item: item.name, reverse=True)


def _declared_project(run):
    """The project a run's own record declares (task.json first, then result.json)."""
    for name in ('task.json', 'result.json'):
        path = Path(run) / name
        if not path.is_file():
            continue
        try:
            row = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict):
            return row.get('project')
    return None


def _same_root(left, right):
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


def _global_rows():
    """Historical aggregation: every project store plus the legacy runs root."""
    rows = []
    seen = set()
    # Ids already owned by a project are never re-served from a legacy backup,
    # even when that project's store is temporarily unavailable.
    legacy_blocked = set(index()['tasks'])
    for project, runs in task_roots():
        for entry in _entries(runs):
            if entry.name in seen:
                continue
            if project is None and entry.name in legacy_blocked:
                continue
            seen.add(entry.name)
            rows.append({'task_id': entry.name, 'run': entry, 'project': project,
                         'source': 'project' if project else 'legacy', 'root': runs})
    return rows


def _project_rows(project):
    """Records an explicit project request may read: its own store, then legacy.

    The explicit directory is authoritative, so a missing/unreadable locator
    index is tolerated here.  A record that the index assigns to another project
    is an ownership conflict and is refused instead of being shown under this
    one.  Legacy records are served only when their own ``project`` field really
    resolves to this directory, which keeps an unmigrated project readable
    without reading another project's history.
    """
    project_key = scope.project_key(project)
    try:
        owned = dict(index()['tasks'])
    except StorageError:
        owned = {}
    rows = []
    seen = set()
    runs = runs_dir(project)
    for entry in _entries(runs):
        owner = owned.get(entry.name)
        if owner and owner != project_key:
            raise StorageError('任务 %s 的定位索引属于其他项目，拒绝在当前项目下读取；'
                               '请先核对归属' % entry.name)
        seen.add(entry.name)
        rows.append({'task_id': entry.name, 'run': entry, 'project': project,
                     'source': 'project', 'root': runs})
    legacy = legacy_runs()
    if legacy.is_dir() and not _same_root(legacy, runs):
        for entry in _entries(legacy):
            if entry.name in seen or entry.name in owned:
                continue
            declared = usable(_declared_project(entry))
            if declared is None or scope.project_key(declared) != project_key:
                continue
            seen.add(entry.name)
            rows.append({'task_id': entry.name, 'run': entry, 'project': project,
                         'source': 'legacy', 'root': legacy})
    return rows


def iter_task_dirs(view=None):
    """Every live task once; a project view reads only that project's records.

    ``view=None`` keeps the historical global aggregation (locator index plus
    legacy).  A ``project`` view enumerates the explicit directory directly, so
    the global locator is an optimisation and never a precondition for opening
    that project's records.  ``unbound`` matches nothing; other projects are
    never read for a project request.
    """
    if view is not None:
        parsed = scope.parse_view(view)
        if parsed['mode'] == scope.MODE_UNBOUND:
            return []
        if parsed['mode'] == scope.MODE_PROJECT:
            return _project_rows(parsed['project'])
    return _global_rows()


def list_task_ids(view=None):
    return [row['task_id'] for row in iter_task_dirs(view)]


def owning_root(task_id, project=None):
    """The runs root that really owns a task id (for safe path checks)."""
    if project:
        return runs_dir(project)
    owning = locate('task', task_id)
    if owning:
        return runs_dir(owning)
    return legacy_runs()


# ------------------------------------------------------------- context hygiene

CONTEXT_EXCLUDED_DIRS = (DESK_DIR,)


def is_record_path(path):
    """True when a path is inside a ``.model-desk`` record tree."""
    try:
        parts = Path(path).parts
    except (TypeError, ValueError):
        return False
    return DESK_DIR in parts


def context_exclusions():
    return {'directories': list(CONTEXT_EXCLUDED_DIRS),
            'note': '工作台记录目录 .model-desk 默认不进入代码索引与派发上下文；'
                    '这是默认搜索排除约定，不是操作系统的硬隔离。'}


# ------------------------------------------------------------------ DB compaction

def _space(conn):
    page_size = int(conn.execute('PRAGMA page_size').fetchone()[0] or 0)
    page_count = int(conn.execute('PRAGMA page_count').fetchone()[0] or 0)
    freelist = int(conn.execute('PRAGMA freelist_count').fetchone()[0] or 0)
    return {'page_size': page_size, 'page_count': page_count, 'freelist_count': freelist,
            'free_bytes': page_size * freelist, 'size_bytes': page_size * page_count}


def db_space(path):
    """Read-only SQLite space accounting for one database (None when absent)."""
    path = Path(path)
    if not path.is_file():
        return None
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    try:
        conn.execute('PRAGMA busy_timeout=15000')
        return {'size_bytes': path.stat().st_size, **_space(conn)}
    finally:
        conn.close()


def compact_db(path):
    """VACUUM one records database and report real page-level release.

    The reported number is SQLite page accounting, not a deleted-row count.  The
    caller is responsible for proving the queue is idle; this function never
    deletes a record.
    """
    path = Path(path)
    if not path.is_file():
        return {'path': str(path), 'skipped': '数据库不存在', 'before': None, 'after': None,
                'reclaimed_bytes': 0}
    if _is_link(path):
        raise StorageError('记录数据库是链接，拒绝压缩：' + str(path))
    if not os.access(path.parent, os.W_OK):
        raise StorageError('记录数据库所在目录不可写，拒绝压缩：' + str(path))
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    try:
        conn.execute('PRAGMA busy_timeout=15000')
        before = _space(conn)
        try:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.OperationalError:
            pass
        conn.execute('VACUUM')
        after = _space(conn)
    finally:
        conn.close()
    reclaimed = max(0, int(before['size_bytes']) - int(after['size_bytes']))
    return {'path': str(path), 'before': before, 'after': after, 'reclaimed_bytes': reclaimed,
            'note': 'SQLite 页级统计：freelist×page_size 的真实回收，不是删除记录数'}


def compact(project=None, all_projects=False):
    """Compact one project's records.db, or every store only when asked explicitly.

    A no-argument call used to vacuum every project store, which let a request
    that was scoped to one project clean up the whole machine.  Global work now
    requires the explicit ``all_projects=True``.
    """
    if project:
        return compact_db(records_db(normalize(project)))
    if not all_projects:
        raise StorageError('压缩需要明确的单个项目目录；全局压缩必须显式声明')
    return {'targets': [compact_db(records_db(item)) for item in indexed_projects(require_store=False)
                        if records_db(item).is_file()]}


def store_state(project):
    """Read-only state of one project's store for a simplified UI.

    Never creates ``.model-desk``: an untouched project reports
    ``initialized=False`` with a clear reason instead of an empty database.
    """
    project = normalize(project)
    desk = desk_dir(project)
    db = records_db(project)
    state = {'project': project, 'desk': str(desk), 'records_db': str(db),
             'desk_exists': desk.is_dir(), 'linked': _is_link(desk),
             'records_db_exists': db.is_file(), 'initialized': is_initialized(project),
             'runs_exists': runs_dir(project).is_dir(), 'writable': os.access(project, os.W_OK),
             'space': None, 'reason': ''}
    if state['linked']:
        state['reason'] = '项目记录目录是链接，已拒绝使用'
        state['writable'] = False
    elif not state['initialized']:
        state['reason'] = '项目尚未初始化工作台记录；只读查看不会创建隐藏目录'
    elif not state['records_db_exists']:
        state['reason'] = '项目只有 runs 记录，尚无 records.db 阶段库'
    elif not os.access(db.parent, os.W_OK):
        state['reason'] = '项目记录库不可写，压缩会被拒绝'
        state['writable'] = False
    if state['records_db_exists'] and not state['linked']:
        state['space'] = db_space(db)
    return state


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def digest_tree(path):
    """Stable content digest of a directory, independent of absolute location."""
    path = Path(path)
    digest = hashlib.sha256()
    if not path.is_dir():
        return None
    for item in sorted((row for row in path.rglob('*') if row.is_file()),
                       key=lambda row: row.relative_to(path).as_posix()):
        digest.update(item.relative_to(path).as_posix().encode('utf-8'))
        digest.update(b'\x00')
        digest.update(digest_file(item).encode('ascii'))
        digest.update(b'\n')
    return digest.hexdigest()


def remove_tree(path, root):
    """Delete a tree only when it provably stays inside ``root`` and is not a link."""
    path, root = Path(path), Path(root)
    if _is_link(path) or not path.resolve().is_relative_to(root.resolve()) or path.resolve() == root.resolve():
        raise StorageError('拒绝删除越界或链接路径：' + str(path))
    if path.exists():
        shutil.rmtree(path)
