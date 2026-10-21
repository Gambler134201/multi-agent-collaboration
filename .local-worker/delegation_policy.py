"""Outcome delegation and independent-acceptance records for the model desk.

Division-of-labour target: the main mind keeps a small share of the work
(clarify the need, make key decisions, route, and accept independently) while
the executor agent investigates, designs, implements and tests. That share is a
responsibility split, **not** a measurable token ratio: the shared Codex
account does not expose per-task main-mind tokens, so this module only stores
real counts (delegations, accept/return/block decisions, existing worker
usage). It never claims a savings rate.

Rules enforced here:

* Executor completion and acceptance are separate states.
* An executor's own report or self-run test never approves its work; passing
  acceptance needs at least one independent evidence item.
* Tasks without the outcome-delegation marker (old history) can never be
  retroactively accepted.
* At most one automatic rework; after that the record must be blocked/escalated.

No function in this module calls a model, opens a socket, or spends budget.
"""
from pathlib import Path
import json
from functools import wraps

import model_registry as registry
import project_storage

SCHEMA = 1
REVIEW_NAME = 'review.json'

DISPATCHED = 'dispatched'
EXECUTOR_COMPLETED = 'executor_completed'
EXECUTOR_FAILED = 'executor_failed'
ACCEPTED = 'accepted'
FINAL_ACCEPTED = 'final_accepted'
RETURNED = 'returned'
BLOCKED = 'blocked'

STATE_LABELS = {
    DISPATCHED: '已委派 · 待执行',
    EXECUTOR_COMPLETED: '执行器完成 · 待独立验收',
    EXECUTOR_FAILED: '执行器未完成 · 待处理',
    ACCEPTED: '独立验收通过',
    FINAL_ACCEPTED: '主脑介入 · 最终验收通过',
    RETURNED: '退回整改',
    BLOCKED: '受阻 · 需主脑判断',
}
DECIDED = {ACCEPTED, FINAL_ACCEPTED, BLOCKED}
ACCEPTED_STATES = {ACCEPTED, FINAL_ACCEPTED}
MAX_AUTO_REWORK = 1
MAX_CRITERIA = 20
MAX_TEXT = 1000


class ReworkClaimRefused(ValueError):
    """A rework claim was refused by validation before any record was written.

    The brain request journal may treat this specific refusal as a proven
    rejection (no effect), instead of an uncertain outcome.
    """


MAIN_ACTORS = {'main', 'codex', 'human', 'user'}
EXECUTOR_ACTORS = {'executor', 'worker', 'dsh', 'pi', 'workbuddy', 'agent', 'self', 'panel'}
INDEPENDENT_SOURCES = {'reviewer_file', 'reviewer_test', 'reviewer_inspection'}
EXECUTOR_SOURCES = {'executor_report', 'executor_test', 'self_report'}
EVIDENCE_SOURCES = INDEPENDENT_SOURCES | EXECUTOR_SOURCES
PASS = 'pass'
FAIL = 'fail'

DELEGATION_NOTE = ('委派数、验收/退回/受阻次数与已有 worker 用量是真实记录；'
                   '主脑约 30% 是职责分工目标，不是可精确测量的 token 占比或节约率。')


# ------------------------------------------------------------------ persistence

def review_path(run):
    return Path(run) / REVIEW_NAME


def _read_json_strict(path):
    """Read local JSON with the registry's bounded retries.

    A missing file is a normal state; a transient Windows sharing violation or
    a corrupt record must NOT be treated as "absent", because that would reset
    the rework counter and bypass the one-rework limit.
    """
    try:
        return registry.read_json(Path(path))
    except FileNotFoundError:
        return None
    except PermissionError as error:
        raise ValueError('本地记录暂时无法读取（文件被占用），已停止以避免绕过整改限制：' + Path(path).name) from error
    except ValueError as error:
        raise ValueError('本地记录无法解析，已停止以避免绕过整改限制：' + Path(path).name) from error


def task_state(run):
    path = Path(run) / 'task.json'
    data = _read_json_strict(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError('任务记录结构不正确，已停止以避免绕过整改限制：' + path.name)
    return data


def delegation_marker(task):
    marker = task.get('delegation')
    return marker if isinstance(marker, dict) and marker.get('schema') == SCHEMA else None


def is_delegated(task):
    return delegation_marker(task) is not None


# ------------------------------------------------------------- brain ownership

def _brain_identity(task):
    """(owned, owner, project) read from the persisted task, never from a tool arg."""
    brain = task.get('brain') if isinstance(task.get('brain'), dict) else {}
    owner = str(brain.get('owner') or task.get('owner') or '').strip()
    project = str(brain.get('project') or task.get('project') or '').strip()
    owned = bool(brain) or bool(owner) or str(task.get('source') or '') == 'brain'
    return owned, owner, project


def is_brain_owned(task):
    return _brain_identity(task)[0]


def brain_identity(task):
    """Public (owned, owner, project) view used by the panel link guards."""
    return _brain_identity(task)


def _require_brain_authority(task, authority):
    """Privileged changes to a brain-owned task need controller authority.

    Legacy/non-brain tasks keep their historical behaviour.  A plain
    ``actor='main'`` string (CLI or ordinary panel route) can never approve a
    brain-owned task; the broker passes a signed authority for the connection's
    immutable owner/project.
    """
    owned, owner, project = _brain_identity(task)
    if not owned:
        return None
    if not owner:
        raise ValueError('主脑任务的归属记录不完整，拒绝变更；不会自动收养该任务')
    import brain_context
    binding_key = (task.get('brain') or {}).get('binding_key')
    if (not binding_key or
            not brain_context.authority_matches(authority, owner, project or None, binding_key)):
        raise ValueError('该任务属于主脑 ' + owner + '；仅填写 actor 或普通面板请求不能验收、退回、'
                         '受阻或整改，需要经本机控制器验证的同一拥有者连接')
    return authority


def find_request(run, request_id):
    """Return the review record only when it actually carries this request id."""
    if not isinstance(request_id, str) or not request_id:
        return None
    record = load(run)
    if record is None:
        return None
    if record.get('last_request_id') == request_id:
        return record
    if str((record.get('rework') or {}).get('request_id') or '') == request_id:
        return record
    for row in record.get('decisions') or []:
        if isinstance(row, dict) and row.get('request_id') == request_id:
            return record
    return None


def load(run):
    """Return the stored record, or None when it has not been created yet.

    Raises ValueError when an existing record cannot be read or does not match
    the current schema, so a temporary read failure cannot silently reset a
    decided or returned record.
    """
    path = review_path(run)
    data = _read_json_strict(path)
    if data is None:
        return None
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise ValueError('验收记录版本不匹配，已停止以避免绕过整改限制：' + path.name)
    if not isinstance(data.get('rework_count', 0), int) or isinstance(data.get('rework_count'), bool) or not 0 <= data.get('rework_count', 0) <= MAX_AUTO_REWORK:
        raise ValueError('验收记录的整改计数不合法，已停止以避免绕过整改限制：' + path.name)
    return data


def safe_load(run):
    """Read-only view for panels/stats: never raises, reports the broken state."""
    try:
        return load(run), ''
    except ValueError as error:
        return None, str(error)


def save(run, record):
    record['updated_at'] = registry.now()
    registry.write_json(review_path(run), record)
    return record


def serialized(function):
    @wraps(function)
    def apply(run, *args, **kwargs):
        with registry.file_lock(Path(run) / 'review.lock'):
            return function(run, *args, **kwargs)
    return apply


def _mark_task(run, record):
    registry.patch_task(Path(run), review_state=record.get('state'), review_updated_at=record.get('updated_at'))


# --------------------------------------------------------------------- helpers

def _clean_text(value, limit=MAX_TEXT):
    return str(value if value is not None else '').strip()[:limit]


def _build_criteria(texts):
    out = []
    for text in texts:
        clean = _clean_text(text, 500)
        if not clean or any(item['text'] == clean for item in out):
            continue
        out.append({'id': 'c' + str(len(out) + 1), 'text': clean, 'status': 'unknown',
                    'evidence': [], 'note': ''})
        if len(out) >= MAX_CRITERIA:
            break
    return out


def _criteria_from_acceptance(task):
    """Normalize a canonical acceptance declaration into separate conditions.

    ``requirements.acceptance`` may be a string (split on separators) or a real
    array.  An array is kept as one condition per element and is never flattened
    into a single ``str(list)`` condition.
    """
    route = task.get('route') or {}
    requirements = route.get('requirements') or {}
    raw = requirements.get('acceptance')
    lines = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            clean = str(item).strip()
            if clean:
                lines.append(clean)
    elif raw:
        for chunk in str(raw).replace('\r', '\n').replace('；', '\n').replace(';', '\n').split('\n'):
            line = chunk.strip(' -\t')
            if line:
                lines.append(line)
    return lines[:MAX_CRITERIA]


def _blank(run, task):
    marker = delegation_marker(task) or {}
    owned, owner, _project = _brain_identity(task)
    try:
        base = max(0, int(marker.get('inherited_rework_count', marker.get('rework_count', 0)) or 0))
    except (TypeError, ValueError):
        base = 0
    return {'schema': SCHEMA, 'task_id': task.get('id') or Path(run).name,
            'created_at': registry.now(), 'updated_at': registry.now(),
            'state': DISPATCHED, 'rework_count': base, 'executor': None,
            'criteria': _build_criteria(_criteria_from_acceptance(task)),
            'rework': None, 'blocked_reason': '', 'reviewer_notes': '', 'decisions': [],
            'stage_id': task.get('stage_id'), 'goal_id': task.get('goal_id'),
            'brain_owner': owner if owned and owner else None,
            'dispatch_request_id': task.get('request_id') if owned else None,
            'last_request_id': None}


def _require_record(run):
    task = task_state(run)
    record = load(run)
    if not is_delegated(task):
        raise ValueError('旧任务或非成果委派任务没有可追认的验收记录；请对新的委派任务记录验收，旧历史不能追认通过。')
    return (record or _blank(run, task)), task


def _require_main(actor, authority=None):
    value = _clean_text(actor, 160).lower()
    if value.startswith('brain:') and len(value) > len('brain:'):
        import brain_context
        if (brain_context.verify_authority(authority)
                and value == ('brain:' + authority.owner).lower()):
            return value
        raise ValueError('主脑 actor 名称需要经控制器验证的授权，不能自行声明')
    if value in EXECUTOR_ACTORS or value not in MAIN_ACTORS:
        raise ValueError('只有主脑可以记录验收结论；执行器或自报结果不能批准自己的成果。')
    return value


def _compact_test(test):
    if not isinstance(test, dict):
        return None
    code = test.get('exit_code')
    if isinstance(code, bool) or not isinstance(code, int):
        code = None
    return {'exit_code': code, 'ok': code == 0, 'excerpt': str(test.get('output') or '')[-800:]}


def _decision_rows(criteria):
    if not isinstance(criteria, (list, tuple)) or not criteria:
        raise ValueError('请为每一条验收条件提供结论和证据')
    rows = []
    for item in criteria:
        if not isinstance(item, dict):
            raise ValueError('验收结论格式不正确')
        key = _clean_text(item.get('id') or item.get('criterion') or item.get('text'), 500)
        status = _clean_text(item.get('status'), 20).lower()
        if status not in {PASS, FAIL}:
            raise ValueError('验收结论只能是 pass 或 fail')
        evidence = []
        for entry in item.get('evidence') or []:
            if not isinstance(entry, dict):
                raise ValueError('证据格式不正确')
            source = _clean_text(entry.get('source'), 40).lower()
            if source not in EVIDENCE_SOURCES:
                raise ValueError('证据来源需要是主脑检查 / 主脑测试 / 执行器自报之一')
            detail = _clean_text(entry.get('detail'), MAX_TEXT)
            if not detail:
                raise ValueError('证据需要可核验的说明')
            code = entry.get('exit_code')
            if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
                raise ValueError('测试证据的退出码需要是整数')
            evidence.append({'source': source, 'detail': detail, 'exit_code': code})
        rows.append({'key': key, 'status': status, 'evidence': evidence,
                     'note': _clean_text(item.get('note'), MAX_TEXT)})
    return rows


# ----------------------------------------------------------------- public API

def _resolve_acceptance(record, criteria):
    """Validate per-condition conclusions/evidence against a record's criteria."""
    rows = _decision_rows(criteria)
    by_id = {item['id']: item for item in record['criteria']}
    by_text = {item['text']: item for item in record['criteria']}
    resolved, seen = [], set()
    for row in rows:
        target = by_id.get(row['key']) or by_text.get(row['key'])
        if target is None:
            raise ValueError('验收条件不在记录中：' + (row['key'] or '未命名'))
        if target['id'] in seen:
            raise ValueError('同一条验收条件被重复记录：' + target['id'])
        seen.add(target['id'])
        resolved.append({'target': target['id'], **row})
    missing = [item['id'] for item in record['criteria'] if item['id'] not in seen]
    if missing:
        raise ValueError('缺少验收条件的结论与证据：' + '、'.join(missing))
    failed = []
    for row in resolved:
        if row['status'] == FAIL:
            failed.append(row['target'])
            continue
        if not row['evidence']:
            raise ValueError('验收通过的条件需要至少一条证据：' + row['target'])
        if not any(entry['source'] in INDEPENDENT_SOURCES for entry in row['evidence']):
            raise ValueError('每条验收条件都需要主脑独立证据：' + row['target'])
        for entry in row['evidence']:
            if entry['source'] == 'reviewer_test' and entry['exit_code'] != 0:
                raise ValueError('标记为验收通过的测试证据退出码不是 0：' + row['target'])
    if failed:
        raise ValueError('存在未通过的验收条件，不能验收通过；请退回整改：' + '、'.join(failed))
    independent = [entry for row in resolved for entry in row['evidence'] if entry['source'] in INDEPENDENT_SOURCES]
    if not independent:
        raise ValueError('只有执行器自报内容，不能验收通过；请主脑检查实际文件或运行测试作为独立证据。')
    return resolved


def acceptance_problems(record):
    """Read-only coherence check for a record that claims acceptance.

    The writer (:func:`accept` / :func:`final_accept`) already refuses a pass
    without per-condition main-mind evidence.  A record found on disk, however,
    can be hand-edited, truncated or produced by an older writer, so read-only
    views must not treat the bare ``state == accepted`` as verified.  Returns a
    list of concrete problems; an empty list means the accepted record is
    structurally coherent.  Never raises and never mutates ``record``.
    """
    problems = []
    if not isinstance(record, dict):
        return ['验收记录结构不正确']
    state = _clean_text(record.get('state'), 40).lower()
    if state not in ACCEPTED_STATES:
        return ['验收状态不是通过：' + (state or '未知')]
    criteria = record.get('criteria')
    if not isinstance(criteria, list) or not criteria:
        return ['验收通过但没有任何可判定的验收条件与逐条证据']
    for index, item in enumerate(criteria):
        fallback = '条件#' + str(index + 1)
        if not isinstance(item, dict):
            problems.append(fallback + ' 结构不正确')
            continue
        name = _clean_text(item.get('id') or item.get('text'), 120) or fallback
        status = _clean_text(item.get('status'), 20).lower()
        if status != PASS:
            problems.append(name + ' 的结论不是通过：' + (status or '未知'))
            continue
        evidence = item.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            problems.append(name + ' 缺少逐条证据')
            continue
        independent = False
        for entry in evidence:
            if not isinstance(entry, dict):
                continue
            source = _clean_text(entry.get('source'), 40).lower()
            detail = _clean_text(entry.get('detail'), MAX_TEXT)
            if source in INDEPENDENT_SOURCES and detail:
                independent = True
                if source == 'reviewer_test' and (type(entry.get('exit_code')) is not int
                                                   or entry.get('exit_code') != 0):
                    problems.append(name + ' 的测试证据退出码不是 0：' +
                                    _clean_text(entry.get('exit_code'), 20))
        if not independent:
            problems.append(name + ' 没有主脑独立证据（执行器自报不算）')
    return problems


def _store_acceptance(record, resolved):
    decided = {row['target']: row for row in resolved}
    for item in record['criteria']:
        row = decided.get(item['id'])
        if row:
            item['status'] = row['status']
            item['evidence'] = row['evidence']
            item['note'] = row['note']


@serialized
def initialize(run, criteria=None, actor='main', authority=None, request_id=''):
    """Create or update the decidable acceptance conditions (main mind only)."""
    actor = _require_main(actor, authority)
    task = task_state(run)
    _require_brain_authority(task, authority)
    if not is_delegated(task):
        raise ValueError('这不是成果委派任务（旧任务或手动任务），不能创建可追认的验收记录。')
    record = load(run) or _blank(run, task)
    if record['state'] in DECIDED:
        raise ValueError('验收结论已经记录，不能再改动验收条件。')
    if criteria:
        texts = [criteria] if isinstance(criteria, str) else list(criteria)
        record['criteria'] = _build_criteria(texts)
    record['decisions'].append({'at': registry.now(), 'actor': actor,
                                'action': 'init', 'notes': '记录验收条件',
                                'request_id': _clean_text(request_id, 80) or None})
    if request_id:
        record['last_request_id'] = _clean_text(request_id, 80)
    record['decisions'] = record['decisions'][-50:]
    save(run, record)
    _mark_task(run, record)
    return record


@serialized
def record_executor(run, executor):
    """Record executor completion/failure. Executors can never record acceptance.

    Returns None when the task has no outcome-delegation marker, so old or
    manual work never gets a retroactive acceptance record.
    """
    task = task_state(run)
    if not is_delegated(task):
        return None
    record = load(run) or _blank(run, task)
    completed = bool(executor.get('completed'))
    record['executor'] = {
        'status': 'completed' if completed else 'failed',
        'at': registry.now(),
        'model_id': task.get('model_id'),
        'model': task.get('model'),
        'dsh_session_id': task.get('dsh_session_id'),
        'engine': task.get('engine'),
        'pi_session_id': task.get('pi_session_id'),
        'changed_files': [str(name)[:300] for name in (executor.get('changed_files') or [])][:30],
        'test': _compact_test(executor.get('test')),
    }
    if record['state'] not in DECIDED:
        record['state'] = EXECUTOR_COMPLETED if completed else EXECUTOR_FAILED
    record['decisions'].append({'at': registry.now(), 'actor': 'executor', 'action': 'report',
                                'notes': '执行器完成' if completed else '执行器未完成'})
    record['decisions'] = record['decisions'][-50:]
    save(run, record)
    _mark_task(run, record)
    return record


@serialized
def accept(run, actor, criteria, notes='', authority=None, request_id=''):
    """Independently accept a completed executor result with per-condition evidence."""
    actor = _require_main(actor, authority)
    record, task = _require_record(run)
    _require_brain_authority(task, authority)
    if record['state'] in ACCEPTED_STATES:
        raise ValueError('这个成果已经验收通过，不能重复追认。')
    if record['state'] == EXECUTOR_FAILED:
        raise ValueError('执行器并未完成，普通验收不能把失败伪造成执行器完成；'
                         '若主脑已独立接管并完成，请使用最终验收（--final-accept）。')
    if record['state'] != EXECUTOR_COMPLETED:
        raise ValueError('执行器尚未完成或已结束，不能验收通过；请等待执行结果或记录受阻。')
    if not record['criteria']:
        raise ValueError('尚未记录可判定的验收条件；请先用 --init 记录验收条件。')
    resolved = _resolve_acceptance(record, criteria)
    executor_test = (record.get('executor') or {}).get('test') or {}
    if executor_test.get('exit_code') not in (0, None) and not _clean_text(notes):
        raise ValueError('执行器自报测试未通过，验收需要主脑给出说明并附独立证据。')
    _store_acceptance(record, resolved)
    record['state'] = ACCEPTED
    record['blocked_reason'] = ''
    record['reviewer_notes'] = _clean_text(notes, MAX_TEXT)
    record['decisions'].append({'at': registry.now(), 'actor': actor, 'action': 'accept',
                                'notes': record['reviewer_notes'],
                                'request_id': _clean_text(request_id, 80) or None})
    if request_id:
        record['last_request_id'] = _clean_text(request_id, 80)
    record['decisions'] = record['decisions'][-50:]
    save(run, record)
    _mark_task(run, record)
    return record


@serialized
def final_accept(run, actor, criteria, notes='', authority=None, request_id=''):
    """Main-mind final acceptance after taking over failed/blocked work.

    This is a separate state from ordinary acceptance: the executor's failure
    (or blocked record) is preserved verbatim, the rework counter is never reset
    and the intervention is stored under ``final`` so a panel or an auditor can
    see that the executor did not complete the work.
    """
    actor = _require_main(actor, authority)
    record, task = _require_record(run)
    _require_brain_authority(task, authority)
    if record['state'] in ACCEPTED_STATES:
        raise ValueError('这个成果已经验收通过，不能重复追认。')
    if record['state'] not in {EXECUTOR_FAILED, BLOCKED, RETURNED}:
        raise ValueError('最终验收只用于执行器未完成、已受阻或已退回后的主脑介入；执行器正常完成请使用普通验收。')
    if not _clean_text(notes):
        raise ValueError('最终验收需要写明主脑接管了什么、如何独立验证')
    if not record['criteria']:
        raise ValueError('尚未记录可判定的验收条件；请先用 --init 记录验收条件。')
    resolved = _resolve_acceptance(record, criteria)
    executor = record.get('executor') or {}
    executor_test = executor.get('test') or {}
    record['final'] = {
        'accepted_at': registry.now(),
        'actor': actor,
        'notes': _clean_text(notes, MAX_TEXT),
        'prior_state': record['state'],
        'executor_status_preserved': executor.get('status'),
        'executor_test_exit_preserved': executor_test.get('exit_code'),
        'rework_count_at_acceptance': record['rework_count'],
        'prior_blocked_reason': record.get('blocked_reason') or '',
        'rework_skipped': bool(record.get('rework')),
    }
    _store_acceptance(record, resolved)
    record['state'] = FINAL_ACCEPTED
    record['blocked_reason'] = ''
    record['reviewer_notes'] = _clean_text(notes, MAX_TEXT)
    record['decisions'].append({'at': registry.now(), 'actor': actor, 'action': 'final_accept',
                                'notes': record['reviewer_notes'],
                                'request_id': _clean_text(request_id, 80) or None})
    if request_id:
        record['last_request_id'] = _clean_text(request_id, 80)
    record['decisions'] = record['decisions'][-50:]
    save(run, record)
    _mark_task(run, record)
    return record


@serialized
def request_rework(run, actor, failed_criteria, problems, boundaries, notes='',
                   authority=None, request_id=''):
    """Return the work with the failed conditions, the real problem and the boundary."""
    actor = _require_main(actor, authority)
    record, task = _require_record(run)
    _require_brain_authority(task, authority)
    if record['state'] in DECIDED:
        raise ValueError('验收结论已经结束（验收通过或受阻），不能再次退回。')
    if record['state'] == RETURNED:
        raise ValueError('已经退回整改，请等待整改执行完成后重新记录验收。')
    if record['state'] not in {EXECUTOR_COMPLETED, EXECUTOR_FAILED}:
        raise ValueError('执行器还没有结果，暂时不能退回整改。')
    if record['rework_count'] >= MAX_AUTO_REWORK:
        raise ValueError('已经自动整改过一次，不能继续循环；请记录受阻或由主脑决定升级/接管。')
    keys = failed_criteria if isinstance(failed_criteria, (list, tuple)) else [failed_criteria]
    keys = [_clean_text(key, 500) for key in keys if _clean_text(key, 500)]
    if not keys:
        raise ValueError('请写明未通过的验收条件')
    by_id = {item['id']: item for item in record['criteria']}
    by_text = {item['text']: item for item in record['criteria']}
    resolved = []
    for key in keys:
        target = by_id.get(key) or by_text.get(key)
        if target is None:
            raise ValueError('未通过的验收条件不在记录中：' + key)
        if target['id'] not in resolved:
            resolved.append(target['id'])
    problems = _clean_text(problems, 2000)
    boundaries = _clean_text(boundaries, MAX_TEXT)
    if not problems:
        raise ValueError('请写明实际观察到的问题')
    if not boundaries:
        raise ValueError('请写明整改范围与边界')
    record['state'] = RETURNED
    record['rework_count'] += 1
    record['rework'] = {'failed_criteria': resolved,
                        'failed_texts': [by_id[item]['text'] for item in resolved],
                        'problems': problems, 'boundaries': boundaries,
                        'notes': _clean_text(notes, MAX_TEXT),
                        'attempts_left': MAX_AUTO_REWORK - record['rework_count'],
                        'at': registry.now(), 'dispatched_task_id': None, 'dispatched_at': None,
                        'request_id': _clean_text(request_id, 80) or None}
    record['decisions'].append({'at': registry.now(), 'actor': actor, 'action': 'return',
                                'notes': problems,
                                'request_id': _clean_text(request_id, 80) or None})
    if request_id:
        record['last_request_id'] = _clean_text(request_id, 80)
    record['decisions'] = record['decisions'][-50:]
    save(run, record)
    marker = {**(delegation_marker(task) or {}), 'rework_count': record['rework_count']}
    registry.patch_task(Path(run), review_state=RETURNED, review_updated_at=record['updated_at'], delegation=marker)
    return record


@serialized
def block(run, actor, reason, authority=None, request_id=''):
    """Record a blocked/escalated outcome instead of looping rework forever."""
    actor = _require_main(actor, authority)
    record, task = _require_record(run)
    _require_brain_authority(task, authority)
    if record['state'] in ACCEPTED_STATES:
        raise ValueError('成果已验收通过，不能追认为受阻。')
    if record['state'] == BLOCKED:
        raise ValueError('这个成果已经记录为受阻。')
    reason = _clean_text(reason, 2000)
    if not reason:
        raise ValueError('请写明受阻原因或需要主脑判断的问题')
    record['state'] = BLOCKED
    record['blocked_reason'] = reason
    record['decisions'].append({'at': registry.now(), 'actor': actor, 'action': 'block',
                                'notes': reason,
                                'request_id': _clean_text(request_id, 80) or None})
    if request_id:
        record['last_request_id'] = _clean_text(request_id, 80)
    record['decisions'] = record['decisions'][-50:]
    save(run, record)
    _mark_task(run, record)
    return record


@serialized
def mark_rework_dispatched(run, child_id, authority=None, request_id=''):
    """Record that the single allowed rework has been dispatched as a continuation."""
    task = task_state(run)
    if task:
        _require_brain_authority(task, authority)
    record = load(run)
    if record is None or record.get('state') != RETURNED:
        raise ReworkClaimRefused('只有处于退回整改状态的任务才能派发整改')
    rework = record.get('rework') or {}
    if rework.get('dispatched_task_id'):
        if rework['dispatched_task_id'] == str(child_id):
            return record
        raise ReworkClaimRefused('整改已经派发为任务 ' + str(rework['dispatched_task_id']))
    rework['dispatched_task_id'] = str(child_id)
    rework['dispatched_at'] = registry.now()
    if request_id:
        rework['request_id'] = _clean_text(request_id, 80)
        record['last_request_id'] = _clean_text(request_id, 80)
    record['rework'] = rework
    return save(run, record)


@serialized
def release_rework_dispatch(run, child_id, reason='', authority=None):
    """Release only a dispatch marker whose worker never started.

    Used when the pipeline refuses the claim *after* the atomic rework marker
    was written.  The rework count, the review state and every acceptance /
    cancellation guard stay exactly as recorded; only the un-started child
    binding is cleared, with a durable decision row so the release is auditable
    and can never be mistaken for a fresh rework slot.
    """
    task = task_state(run)
    if task:
        _require_brain_authority(task, authority)
    record = load(run)
    if record is None:
        return None
    rework = record.get('rework') or {}
    if str(rework.get('dispatched_task_id') or '') != str(child_id):
        # Nothing of ours to release; a different (real) claim stays untouched.
        return record
    rework['dispatched_task_id'] = None
    rework['dispatched_at'] = None
    rework['release_reason'] = _clean_text(reason, 1000)
    record['rework'] = rework
    record['decisions'].append({'at': registry.now(), 'actor': 'main',
                                'action': 'release_unstarted_rework',
                                'notes': ('派发前校验/认领失败，释放未启动的整改子任务绑定；'
                                          '整改次数与验收状态不变。原因：' + _clean_text(reason, 500)),
                                'unstarted_task_id': str(child_id)})
    record['decisions'] = record['decisions'][-50:]
    return save(run, record)


def rework_prompt(record, deliverable=None, new_session=True):
    """Compact, non-solution rework brief keeping the original model and scope.

    By default the corrective round runs in a *new* execution Agent session: it carries the
    previous deliverable, the failure evidence and the correction scope instead
    of silently inheriting the old conversation.  ``new_session=False`` is the
    explicit exception (``--reuse-session``).
    """
    rework = (record or {}).get('rework') or {}
    if not rework:
        raise ValueError('没有可用的整改说明')
    executor = (record or {}).get('executor') or {}
    test = executor.get('test') or {}
    lines = ['整改任务：上一轮独立验收未通过，请在' +
             ('新的执行 Agent 会话中' if new_session else '显式复用的原会话中') +
             '、同一项目、同一文件范围内自行解决。',
             '未通过的验收条件：']
    for text in rework.get('failed_texts') or rework.get('failed_criteria') or []:
        lines.append('- ' + str(text))
    summary = ''
    if isinstance(deliverable, dict):
        summary = str(deliverable.get('summary') or '')
        if deliverable.get('changed_files'):
            summary += '\n上一轮已改文件：' + '、'.join(str(name) for name in deliverable['changed_files'])
    elif deliverable:
        summary = str(deliverable)
    lines += ['上一轮成果交接：', summary[-1500:] or '（未提供上一轮摘要，请从任务记录查看）',
              '失败证据：', _failure_line(executor, test),
              '实际观察到的问题：', str(rework.get('problems') or ''),
              '范围与边界：', str(rework.get('boundaries') or ''),
              '要求：先自行调查原因，再设计并修改，运行适当的测试；只改允许范围内的文件。'
              '完成后简短说明改动、可核验证据和未解决问题，不要重述整段任务或主脑聊天。']
    return '\n'.join(lines)


def _failure_line(executor, test):
    parts = ['执行器状态：' + str((executor or {}).get('status') or '未知')]
    if test:
        parts.append('验收测试退出码：' + str(test.get('exit_code')))
        excerpt = str(test.get('excerpt') or '')[-600:]
        if excerpt:
            parts.append('测试输出摘录：\n' + excerpt)
    return '；'.join(parts)


def panel_summary(record):
    if not record:
        return None
    rework = record.get('rework') or {}
    executor = record.get('executor') or {}
    test = executor.get('test') or {}
    return {'task_id': record.get('task_id'),
            'state': record.get('state'),
            'label': STATE_LABELS.get(record.get('state'), record.get('state') or ''),
            'owner': record.get('brain_owner'),
            'rework_count': record.get('rework_count', 0),
            'max_auto_rework': MAX_AUTO_REWORK,
            'updated_at': record.get('updated_at'),
            'executor_status': executor.get('status'),
            'executor_test_exit': test.get('exit_code'),
            'criteria': [{'id': item.get('id'), 'text': item.get('text'), 'status': item.get('status'),
                          'evidence': len(item.get('evidence') or [])}
                         for item in record.get('criteria') or []],
            'returned_criteria': list(rework.get('failed_texts') or []),
            'problems': rework.get('problems') or '',
            'boundaries': rework.get('boundaries') or '',
            'rework_dispatched': bool(rework.get('dispatched_task_id')),
            'blocked_reason': record.get('blocked_reason') or '',
            'reviewer_notes': record.get('reviewer_notes') or '',
            'final_accepted': record.get('state') == FINAL_ACCEPTED,
            'final': record.get('final') or None}


def aggregate():
    """Persisted counts. Never estimates a main-mind token share."""
    stats = {'tasks': 0, 'delegated': 0, 'legacy': 0, 'unreadable': 0, DISPATCHED: 0,
             EXECUTOR_COMPLETED: 0, EXECUTOR_FAILED: 0, ACCEPTED: 0, FINAL_ACCEPTED: 0, RETURNED: 0, BLOCKED: 0,
             'rework_dispatched': 0, 'model_calls': 0, 'worker_tokens': 0, 'note': DELEGATION_NOTE}
    runs = [row['run'] for row in project_storage.iter_task_dirs()]
    for folder in runs:
        if not folder.is_dir():
            continue
        try:
            task = task_state(folder)
        except ValueError:
            stats['unreadable'] += 1
            continue
        if not task:
            continue
        stats['tasks'] += 1
        record, error = safe_load(folder)
        if error:
            stats['unreadable'] += 1
            continue
        if not is_delegated(task):
            if record is None:
                stats['legacy'] += 1
            continue
        stats['delegated'] += 1
        state = (record or {}).get('state') or DISPATCHED
        if state in stats:
            stats[state] += 1
        calls = task.get('model_calls')
        if isinstance(calls, int) and not isinstance(calls, bool):
            stats['model_calls'] += calls
        tokens = (task.get('usage') or {}).get('total_tokens')
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            stats['worker_tokens'] += tokens
        if ((record or {}).get('rework') or {}).get('dispatched_task_id'):
            stats['rework_dispatched'] += 1
    return stats


def review_index():
    rows = []
    folders = [row['run'] for row in project_storage.iter_task_dirs()]
    for folder in folders:
        if not folder.is_dir():
            continue
        record, error = safe_load(folder)
        if record is None:
            if error:
                rows.append({'task_id': folder.name, 'state': 'unreadable', 'label': '记录无法读取',
                             'rework_count': None, 'max_auto_rework': MAX_AUTO_REWORK, 'error': error})
            continue
        try:
            task = task_state(folder)
        except ValueError:
            task = {}
        summary = panel_summary(record)
        summary.update({'title': task.get('title') or '', 'model_id': task.get('model_id'),
                        'executor_model': task.get('model_name') or task.get('model'),
                        'parent_task_id': task.get('parent_task_id'), 'created_at': task.get('created_at')})
        rows.append(summary)
        if len(rows) >= 150:
            break
    return {'reviews': rows, 'stats': aggregate(), 'note': DELEGATION_NOTE}
