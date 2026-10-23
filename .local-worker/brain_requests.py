"""Project-owned controller request journal for at-most-once brain mutations.

The journal lives in the project's own ``.model-desk/records.db``.  It is
durable, not an in-memory cache: a panel restart, a lost response or a crash
between reservation and effect can never silently replay a dispatch, review or
rework.  Rules enforced here:

* one ``(owner, request_id)`` is one mutation identity;
* the canonical payload fingerprint includes the operation kind; the same id
  with a different payload is refused;
* a reservation is written *before* the effect;
* concurrent duplicates never run the effect twice (one wins, the other gets an
  explicit in-flight/duplicate result);
* a reservation left behind by a restart becomes ``unknown`` and is never
  replayed; only an attributable existing task/review record may complete it;
* request records never contain controller capabilities, panel tokens or keys.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import re
import threading

import project_storage

SCHEMA = 1
KINDS = ('dispatch', 'review', 'rework')
STATES = ('reserved', 'completed', 'rejected', 'unknown')
REQUEST_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
OWNER_ID = re.compile(r'^[^\s/\\\x00-\x1f]{1,128}$')
_FORBIDDEN_KEYS = ('token', 'secret', 'capability', 'api_key', 'apikey', 'password',
                   'authorization', 'credential')

_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()


class RequestError(ValueError):
    """A safe, user-facing request-journal refusal."""


class RequestConflict(RequestError):
    """The same request id was already used with a different payload."""


class RequestInFlight(RequestError):
    """An identical request is being executed right now; no second effect."""


class RequestRejected(RequestError):
    """A previously proven refusal was recorded for this exact request."""


class UnknownOutcome(RequestError):
    """The original request may have taken effect; it is never replayed."""

    def __init__(self, message, reference, kind='', request_id=''):
        reference = str(reference or '')
        if reference and reference not in str(message):
            message = str(message) + ' 原请求引用：' + reference
        super().__init__(message)
        self.reference = reference
        self.kind = kind
        self.request_id = request_id


def _now():
    import model_registry as registry
    return registry.now()


def _clean_id(value, label):
    if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
        raise RequestError(label + ' 必须是 1-80 位字母、数字、下划线或短横线')
    return value


def _clean_owner(value):
    if not isinstance(value, str) or not OWNER_ID.fullmatch(value):
        raise RequestError('主脑拥有者标识格式不正确')
    return value


def _check_payload(value, path='payload'):
    if isinstance(value, dict):
        for key, item in value.items():
            text = str(key).lower()
            if any(marker in text for marker in _FORBIDDEN_KEYS):
                raise RequestError('请求记录不能包含凭据字段：' + path + '.' + str(key))
            _check_payload(item, path + '.' + str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_payload(item, path + '[' + str(index) + ']')
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise RequestError('请求记录包含无法持久化的值：' + path)


def canonical_payload(payload):
    """Deterministic JSON (sorted keys, compact separators) for the fingerprint."""
    if not isinstance(payload, (dict, list)):
        raise RequestError('请求内容需要是对象或数组')
    _check_payload(payload)
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RequestError('请求内容无法规范化：' + str(error)) from None


def fingerprint(kind, payload):
    if kind not in KINDS:
        raise RequestError('未知的请求类型')
    return hashlib.sha256((kind + '\n' + canonical_payload(payload)).encode('utf-8')).hexdigest()


def reference(owner, request_id):
    return str(owner) + '/' + str(request_id)


def _connect(project):
    project = project_storage.normalize(project)
    path = project_storage.records_db(project)
    conn = project_storage.connect(path, create=True)
    return project, conn


def _row(row):
    if row is None:
        return None
    data = dict(row)
    try:
        data['receipt'] = json.loads(data.get('receipt') or '{}')
    except ValueError:
        data['receipt'] = {}
    data['reference'] = reference(data.get('owner'), data.get('request_id'))
    return data


def _select(conn, owner, request_id):
    return conn.execute(
        'SELECT request_id, owner, kind, payload_digest, state, task_id, receipt, detail, '
        'created_at, updated_at FROM brain_requests WHERE owner=? AND request_id=?',
        (owner, request_id)).fetchone()


def reserve(project, owner, kind, request_id, payload):
    """Reserve one mutation identity before any effect.

    Returns a row with ``reused=False`` when this call owns the reservation, or
    ``reused=True`` with the persisted state/receipt for an identical completed
    or rejected request.  An identical in-flight request raises
    :class:`RequestInFlight`; a leftover reservation from another process (for
    example after a restart) becomes ``unknown`` and raises
    :class:`UnknownOutcome`  because replaying it could duplicate the effect.
    """
    owner = _clean_owner(owner)
    request_id = _clean_id(request_id, 'request_id')
    if kind not in KINDS:
        raise RequestError('未知的请求类型')
    digest = fingerprint(kind, payload)
    project, conn = _connect(project)
    key = (project, owner, request_id)
    claimed = False
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = _select(conn, owner, request_id)
        if existing is not None:
            row = _row(existing)
            if row['payload_digest'] != digest:
                raise RequestConflict('request_id %s 已用于不同的请求内容；拒绝覆盖或自动换 ID' % request_id)
            if row['state'] == 'reserved':
                with _ACTIVE_LOCK:
                    active = key in _ACTIVE
                if active:
                    raise RequestInFlight('相同 request_id 的请求正在处理；未重复执行，请稍后按原记录查询')
                conn.execute('UPDATE brain_requests SET state=?, detail=?, updated_at=? '
                             'WHERE owner=? AND request_id=?',
                             ('unknown',
                              'reservation found without an in-process owner (restart or crashed request); not replayed',
                              _now(), owner, request_id))
                conn.execute('COMMIT')
                raise UnknownOutcome(
                    '原请求在完成前中断，结果未知；未自动重派。请按原请求引用核查。',
                    reference(owner, request_id), kind, request_id)
            conn.execute('COMMIT')
            row['reused'] = True
            return row
        now = _now()
        conn.execute('INSERT INTO brain_requests '
                     '(request_id, owner, kind, payload_digest, state, task_id, receipt, detail, created_at, updated_at) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (request_id, owner, kind, digest, 'reserved', '', '{}', '', now, now))
        with _ACTIVE_LOCK:
            _ACTIVE.add(key)
            claimed = True
        conn.execute('COMMIT')
    except Exception:
        if claimed:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(key)
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return {'request_id': request_id, 'owner': owner, 'kind': kind, 'payload_digest': digest,
            'state': 'reserved', 'task_id': '', 'receipt': {}, 'detail': '',
            'reference': reference(owner, request_id), 'reused': False}


def _finish(project, owner, request_id, state, receipt=None, task_id='', detail=''):
    owner = _clean_owner(owner)
    request_id = _clean_id(request_id, 'request_id')
    project, conn = _connect(project)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = _select(conn, owner, request_id)
        if existing is None:
            raise RequestError('请求记录不存在')
        if state == 'unknown' and existing['state'] in {'completed', 'rejected'}:
            # A late diagnostic failure must not erase an already committed receipt.
            conn.execute('COMMIT')
            return _row(existing)
        conn.execute('UPDATE brain_requests SET state=?, task_id=?, receipt=?, detail=?, updated_at=? '
                     'WHERE owner=? AND request_id=?',
                     (state, str(task_id or '')[:80],
                      json.dumps(receipt if isinstance(receipt, (dict, list)) else {},
                                 ensure_ascii=False, sort_keys=True),
                      str(detail or '')[:500], _now(), owner, request_id))
        conn.execute('COMMIT')
        row = _row(_select(conn, owner, request_id))
        row['reused'] = False
        return row
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise
    finally:
        conn.close()
        with _ACTIVE_LOCK:
            _ACTIVE.discard((project, owner, request_id))


def complete(project, owner, request_id, receipt, task_id=''):
    return _finish(project, owner, request_id, 'completed', receipt=receipt, task_id=task_id)


def reject(project, owner, request_id, detail):
    return _finish(project, owner, request_id, 'rejected', receipt={'error': str(detail)[:500]},
                   detail=detail)


def mark_unknown(project, owner, request_id, detail):
    return _finish(project, owner, request_id, 'unknown', detail=detail)


def status(project, owner, request_id):
    owner = _clean_owner(owner)
    request_id = _clean_id(request_id, 'request_id')
    project, conn = _connect(project)
    try:
        row = _select(conn, owner, request_id)
        return _row(row) if row is not None else None
    finally:
        conn.close()


def lookup(project, owner, request_id):
    """Read one journal row without initializing/migrating a project database."""
    owner = _clean_owner(owner)
    request_id = _clean_id(request_id, 'request_id')
    project = project_storage.normalize(project)
    path = project_storage.records_db(project)
    if project_storage._is_link(path.parent) or project_storage._is_link(path):
        raise RequestError('请求记录路径不可核实')
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                           timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_requests'").fetchone() is None:
            return None
        return _row(_select(conn, owner, request_id))
    finally:
        conn.close()


def _unknown_after_reservation(project, owner, kind, request_id, error):
    # Persisting an uncertainty marker can itself fail (locked disk, full disk,
    # unreadable DB). The public outcome must still remain unknown, never a 400
    # rejection that invites a new request ID. No raw storage error is exposed.
    try:
        mark_unknown(project, owner, request_id,
                     'outcome confirmation failed: ' + type(error).__name__)
    except Exception:
        pass
    finally:
        try:
            normalized = project_storage.normalize(project)
        except (OSError, ValueError):
            normalized = str(project)
        with _ACTIVE_LOCK:
            _ACTIVE.discard((normalized, owner, request_id))
    raise UnknownOutcome(
        '变更结果未知（请求已保留，结果确认失败）；未自动重试。请按原请求引用核查。',
        reference(owner, request_id), kind, request_id) from error


def _complete_or_unknown(project, owner, kind, request_id, receipt):
    try:
        return complete(project, owner, request_id, receipt,
                        task_id=receipt.get('task_id', '') if isinstance(receipt, dict) else '')
    except Exception as error:
        _unknown_after_reservation(project, owner, kind, request_id, error)


def replay(project, owner, kind, request_id, payload):
    """Return a completed/rejected original row for an identical request.

    Returns ``None`` when there is no record yet (the caller then reserves).  A
    different payload raises :class:`RequestConflict`; an unknown outcome raises
    :class:`UnknownOutcome`; an in-flight reservation returns ``None`` so the
    normal reserve path can report the duplicate without a second effect.
    """
    digest = fingerprint(kind, payload)
    row = status(project, owner, request_id)
    if row is None:
        return None
    if row['payload_digest'] != digest:
        raise RequestConflict('request_id %s 已用于不同的请求内容；拒绝覆盖或自动换 ID' % request_id)
    if row['state'] == 'unknown':
        raise UnknownOutcome('原请求结果未知，未自动重派；请按原请求引用核查。',
                             row['reference'], kind, request_id)
    if row['state'] == 'rejected':
        raise RequestRejected('原请求已被明确拒绝；未重复执行')
    if row['state'] == 'completed':
        row['reused'] = True
        return row
    return None


def run_once(project, owner, kind, request_id, payload, effect, recover=None,
             reject_on=()):
    """Reserve, execute exactly once, and persist the original receipt.

    ``effect`` must return a JSON-safe receipt.  Any exception after the
    reservation is treated as an uncertain outcome unless ``recover`` returns an
    actually attributable existing record, or the effect raises one of the
    explicitly proven ``reject_on`` refusal types (validated before effect).
    """
    reserved = reserve(project, owner, kind, request_id, payload)
    if reserved['reused']:
        if reserved['state'] == 'unknown':
            raise UnknownOutcome('原请求结果未知，未自动重派；请按原请求引用核查。',
                                 reserved['reference'], kind, request_id)
        if reserved['state'] == 'rejected':
            raise RequestRejected(str(reserved.get('detail') or '原请求已被明确拒绝；未重复执行'))
        return reserved
    try:
        receipt = effect()
    except Exception as error:
        if reject_on and isinstance(error, reject_on):
            try:
                reject(project, owner, request_id, str(error))
            except Exception as confirmation_error:
                _unknown_after_reservation(project, owner, kind, request_id, confirmation_error)
            raise RequestRejected(str(error)) from error
        found = None
        if recover is not None:
            try:
                found = recover()
            except Exception:
                found = None
        if isinstance(found, dict) and found:
            return _complete_or_unknown(project, owner, kind, request_id, found)
        _unknown_after_reservation(project, owner, kind, request_id, error)
    return _complete_or_unknown(project, owner, kind, request_id, receipt)
