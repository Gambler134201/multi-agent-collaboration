"""Durable cross-session deliverables for the local Model Desk.

A handover is the compact, persisted result of one *accepted* stage task.  It
carries the caller's short summary, the changed files with real SHA-256 digests,
interfaces/key decisions, test evidence, remaining issues and a few context
pointers -- never whole messages or large logs.

Rules enforced here:

* A handover can only be created for a delegated task whose review record is
  already *independently accepted* (``accepted`` or the main-mind
  ``final_accepted``).  Unfinished or legacy work is refused explicitly.
* The file list must stay inside the task's own scope (``files`` plus the
  executor's recorded ``changed_files``); anything else is refused as
  out-of-scope.
* Digests are computed from the real files at write time, so a human-written
  summary is stored as a *claim* and can never be mistaken for verification.
* ``verify`` re-hashes the files and reports staleness; a changed file is never
  silently trusted.
* ``resolve_reference`` reads identity from the real task/handover records: a
  caller may restate the stage ids but cannot relabel them, a staged consumer
  may only read a proven (transitive) prerequisite inside the same goal, and a
  stale (changed/missing file) handover is refused rather than returned.
* ``prerequisite_prompt`` gives a dependent stage dispatch the validated,
  hash-checked prerequisite handovers as compact prompt context; a missing or
  stale required handover blocks the new dispatch instead of silently sending
  an under-contextualized request.

CLI (venv Python, from ``.local-worker``)::

    task_handover.py --task <id>
    task_handover.py --create --task <id> --evidence-file <UTF-8-JSON>
    task_handover.py --verify --task <id>
    task_handover.py --reference --task <consumer-id> --source-task <source-id> \\
        [--consumer-stage <stage>] [--source-stage <stage>]

The optional ``--consumer-stage`` / ``--source-stage`` values are restatements
only: they are checked against the real task/handover records and a mismatch is
refused, so a caller cannot widen or relabel its reference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import delegation_policy
import model_registry as registry

SCHEMA = 1
HANDOVER_NAME = 'handover.json'
MAX_TEXT = 2000
MAX_ITEMS = 30
MAX_FILES = 40

ACCEPTED_STATES = {delegation_policy.ACCEPTED,
                   getattr(delegation_policy, 'FINAL_ACCEPTED', 'final_accepted')}
# Only these sources count as independent verification.  A caller's own summary
# or the executor's self-report is a claim, never evidence.
INDEPENDENT_SOURCES = {'main', 'reviewer', 'independent', 'main_test', 'reviewer_test',
                       'reviewer_file', 'reviewer_inspection'}
CLAIM_SOURCES = {'summary', 'claim', 'human', 'caller', 'executor', 'self',
                 'executor_test', 'executor_report', 'panel'}

HANDOVER_NOTE = ('交接文件摘要与 SHA-256 由本模块在写入时从真实文件计算；'
                 '调用者手写摘要只是说明，不计为验证证据。文件改动后引用会标记失效。')


class HandoverError(ValueError):
    """A user-facing handover validation failure."""


# ------------------------------------------------------------------ persistence

def handover_path(run):
    return Path(run) / HANDOVER_NAME


def _read_strict(path):
    try:
        return registry.read_json(Path(path))
    except FileNotFoundError:
        return None
    except PermissionError as error:
        raise HandoverError('交接记录暂时无法读取（文件被占用）：' + Path(path).name) from error
    except ValueError as error:
        raise HandoverError('交接记录无法解析：' + Path(path).name) from error


def load(run):
    data = _read_strict(handover_path(run))
    if data is None:
        return None
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise HandoverError('交接记录版本不匹配：' + handover_path(run).name)
    return data


def safe_load(run):
    try:
        return load(run), ''
    except HandoverError as error:
        return None, str(error)


def save(run, record):
    record['updated_at'] = registry.now()
    registry.write_json(handover_path(run), record)
    return record


# --------------------------------------------------------------------- helpers

def _clean(value, limit=MAX_TEXT):
    return str(value if value is not None else '').strip()[:limit]


def _items(value, limit=MAX_ITEMS, item_limit=500):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise HandoverError('交接列表字段格式不正确')
    out = []
    for item in list(value)[:limit]:
        clean = _clean(item, item_limit)
        if clean and clean not in out:
            out.append(clean)
    return out


def _task(run):
    task = delegation_policy.task_state(run)
    if not task:
        raise HandoverError('任务记录不存在')
    return task


def digest_files(project, files):
    """Real SHA-256 evidence for each file, computed at call time."""
    project = Path(project)
    rows = []
    for name in files:
        target = (project / name).resolve()
        entry = {'path': name, 'exists': target.is_file()}
        if entry['exists']:
            raw = target.read_bytes()
            entry['sha256'] = hashlib.sha256(raw).hexdigest()
            entry['bytes'] = len(raw)
        else:
            entry['sha256'] = None
            entry['bytes'] = None
        rows.append(entry)
    return rows


def _scope(task):
    scope = []
    for name in list(task.get('files') or []) + list(task.get('changed_files') or []):
        value = _clean(name, 300)
        if value and value not in scope:
            scope.append(value)
    return scope


def _binding(task):
    """Real (stage_id, goal_id) for a task record, never caller-supplied.

    A stage dispatch records both ids; a manually created fixture may record
    only the stage, so the goal is derived from the stage store itself instead
    of being taken on trust from the caller.
    """
    stage = _clean((task or {}).get('stage_id'), 120) or None
    goal = _clean((task or {}).get('goal_id'), 120) or None
    if stage and not goal:
        row = None
        try:
            import stage_store
            row = stage_store.get_stage(stage)
        except Exception:
            row = None
        if row:
            goal = _clean(row.get('goal_id'), 120) or None
    return stage, goal


def _normalize_evidence(test_evidence):
    entries = []
    if test_evidence is None:
        return entries
    if not isinstance(test_evidence, (list, tuple)):
        raise HandoverError('测试证据需要是数组')
    for item in list(test_evidence)[:MAX_ITEMS]:
        if isinstance(item, str):
            item = {'name': item, 'source': 'claim'}
        if not isinstance(item, dict):
            raise HandoverError('测试证据格式不正确')
        source = _clean(item.get('source') or 'claim', 40).lower()
        if source not in INDEPENDENT_SOURCES and source not in CLAIM_SOURCES:
            raise HandoverError('测试证据来源不正确：' + source)
        code = item.get('exit_code')
        if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
            raise HandoverError('测试证据退出码需要是整数')
        entries.append({'name': _clean(item.get('name'), 300),
                        'source': source,
                        'exit_code': code,
                        'detail': _clean(item.get('detail'), MAX_TEXT),
                        'counts_as_evidence': source in INDEPENDENT_SOURCES})
    return entries


# ----------------------------------------------------------------- public API

def create(run, summary='', interfaces=(), decisions=(), remaining_issues=(),
           test_evidence=(), context=(), files=None, actor='main', stage_id=None,
           goal_id=None):
    """Persist a compact, hash-verified handover for one accepted task."""
    run = Path(run)
    task = _task(run)
    record = delegation_policy.load(run)
    if record is None or record.get('state') not in ACCEPTED_STATES:
        raise HandoverError('交接必须在执行器成果独立验收通过后创建；未完成验收的来源被明确拒绝')
    project = _clean(task.get('project'), 500)
    if not project or not Path(project).is_dir():
        raise HandoverError('任务缺少可用的项目目录，无法做文件验证')
    # Identity comes from the real task metadata; a caller may restate it but
    # never widen or relabel it.
    task_stage, task_goal = _binding(task)
    if _clean(stage_id, 120) and _clean(stage_id, 120) != (task_stage or ''):
        raise HandoverError('交接阶段与任务记录不一致，已拒绝（不能自选阶段身份）')
    if _clean(goal_id, 120) and _clean(goal_id, 120) != (task_goal or ''):
        raise HandoverError('交接目标与任务记录不一致，已拒绝（不能自选目标身份）')
    scope = _scope(task)
    requested = _items(files, limit=MAX_FILES, item_limit=300) if files is not None else scope
    out_of_scope = [name for name in requested if name not in scope]
    if out_of_scope:
        raise HandoverError('交接文件超出任务文件范围，已拒绝：' + '、'.join(out_of_scope))
    entries = _normalize_evidence(test_evidence)
    independent = [entry for entry in entries if entry['counts_as_evidence']]
    claims = [entry for entry in entries if not entry['counts_as_evidence']]
    digests = digest_files(project, requested)
    payload = {
        'schema': SCHEMA,
        'task_id': task.get('id') or run.name,
        'stage_id': task_stage,
        'goal_id': task_goal,
        'project': project,
        'model_id': task.get('model_id'),
        'model': task.get('model'),
        'dsh_session_id': task.get('dsh_session_id'),
        'engine': task.get('engine'),
        'pi_session_id': task.get('pi_session_id'),
        'review_state': record.get('state'),
        'review_task_id': task.get('parent_task_id'),
        'summary': _clean(summary, MAX_TEXT),
        'summary_is_claim': True,
        'files': digests,
        'interfaces': _items(interfaces, item_limit=500),
        'decisions': _items(decisions, item_limit=500),
        'remaining_issues': _items(remaining_issues, item_limit=500),
        'context': _items(context, item_limit=300),
        'test_evidence': entries,
        'claims': claims,
        'verification': {
            'source': 'handover_files_sha256',
            'digest_algorithm': 'sha256',
            'file_count': len(requested),
            'files_verified': all(item['exists'] for item in digests),
            'evidence_independent': bool(independent),
            'summary_counts_as_evidence': False,
            'review_state': record.get('state'),
            'verified_at': registry.now(),
        },
        'created_at': registry.now(),
        'updated_at': registry.now(),
        'actor': _clean(actor, 40).lower(),
        'note': HANDOVER_NOTE,
    }
    return save(run, payload)


def verify(run, record=None):
    """Re-hash the recorded files; a changed or missing file means stale."""
    run = Path(run)
    record = record if record is not None else load(run)
    if record is None:
        raise HandoverError('没有可验证的交接记录')
    project = record.get('project') or ''
    changed, missing = [], []
    if not project or not Path(project).is_dir():
        return {'valid': False, 'stale': True, 'changed': [], 'missing': [item.get('path') for item in record.get('files') or []],
                'checked_at': registry.now(), 'message': '项目目录不可用，无法复核交接文件'}
    for item in record.get('files') or []:
        target = (Path(project) / str(item.get('path'))).resolve()
        if not target.is_file():
            missing.append(item.get('path'))
            continue
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != item.get('sha256'):
            changed.append(item.get('path'))
    stale = bool(changed or missing)
    return {'valid': not stale, 'stale': stale, 'changed': changed, 'missing': missing,
            'checked_at': registry.now(),
            'message': ('交接文件已改动或缺失，不能悄悄沿用旧摘要：' +
                        '、'.join(changed + missing)) if stale else '交接文件与记录一致'}


def _review_accepted(run):
    record = delegation_policy.load(run)
    if record is None or record.get('state') not in ACCEPTED_STATES:
        return None
    return record


def _stage_dependencies(consumer_stage_id, source_stage_id):
    """Lazy import avoids a module cycle; returns (ok, reason)."""
    import stage_store
    consumer = stage_store.get_stage(consumer_stage_id)
    if consumer is None:
        return False, '消费阶段不存在：' + consumer_stage_id
    source = stage_store.get_stage(source_stage_id)
    if source is None:
        return False, '来源阶段不存在：' + source_stage_id
    if source['goal_id'] != consumer['goal_id']:
        return False, '来源阶段不在同一目标内，越界引用被拒绝'
    seen, frontier = set(), list(consumer['depends_on'])
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == source_stage_id:
            return True, ''
        row = stage_store.get_stage(current)
        if row:
            frontier.extend(row['depends_on'])
    return False, '来源阶段不是消费阶段的必要前置依赖，跨阶段读取被拒绝'


def resolve_reference(consumer_run, source_task_id, consumer_stage_id=None, source_stage_id=None):
    """Resolve an allowed cross-session reference, refusing unsafe ones.

    Identity is read from the real task/handover records.  A caller may restate
    the stage ids but can never relabel them: the project, goal and dependency
    checks use the recorded metadata, so a staged consumer cannot omit its ids
    or point at another goal's accepted deliverable.  A stale (changed/missing
    file) handover is refused instead of being returned for use.
    """
    consumer_run = Path(consumer_run)
    consumer_task = _task(consumer_run)
    try:
        source_run = registry.task_dir(source_task_id)
    except ValueError as error:
        raise HandoverError('来源任务 ID 不正确') from error
    if not source_run.is_dir():
        raise HandoverError('来源任务不存在')
    source_task = _task(source_run)
    record, error = safe_load(source_run)
    if record is None:
        raise HandoverError(error or '来源任务没有成果交接，明确拒绝引用')
    review = _review_accepted(source_run)
    if review is None:
        raise HandoverError('来源任务尚未完成独立验收，明确拒绝其成果交接')
    consumer_project = _clean(consumer_task.get('project'), 500)
    if not consumer_project or consumer_project != record.get('project'):
        raise HandoverError('跨项目引用成果交接被拒绝')

    consumer_stage, consumer_goal = _binding(consumer_task)
    real_source_stage, real_source_goal = _binding(source_task)
    record_stage = _clean(record.get('stage_id'), 120) or None
    record_goal = _clean(record.get('goal_id'), 120) or None
    if real_source_stage and record_stage and real_source_stage != record_stage:
        raise HandoverError('来源任务的阶段元数据与交接记录不一致，已拒绝')
    if real_source_goal and record_goal and real_source_goal != record_goal:
        raise HandoverError('来源任务的目标元数据与交接记录不一致，已拒绝')
    source_stage = real_source_stage or record_stage
    source_goal = real_source_goal or record_goal

    given_consumer = _clean(consumer_stage_id, 120) or None
    given_source = _clean(source_stage_id, 120) or None
    if given_consumer and given_consumer != consumer_stage:
        raise HandoverError('消费阶段参数与任务记录不一致，不能自选阶段身份')
    if given_source and given_source != source_stage:
        raise HandoverError('来源阶段参数与任务记录不一致，不能指认来源阶段')

    if consumer_stage and not source_stage:
        raise HandoverError('来源任务不属于任何阶段，无法证明是必要依赖')
    if not consumer_stage and source_stage:
        raise HandoverError('来源成果属于阶段任务，非阶段任务不能引用，越界引用被拒绝')
    if consumer_goal != source_goal:
        raise HandoverError('来源交接与消费任务不在同一目标内，越界引用被拒绝')
    if consumer_stage:
        allowed, reason = _stage_dependencies(consumer_stage, source_stage)
        if not allowed:
            raise HandoverError(reason)

    freshness = verify(source_run, record)
    if freshness['stale']:
        raise HandoverError('成果交接已失效，拒绝引用：' + freshness['message'])
    payload = {'status': 'ok',
               'task_id': record.get('task_id'), 'stage_id': record.get('stage_id'),
               'goal_id': record.get('goal_id'), 'project': record.get('project'),
               'summary': record.get('summary'), 'files': record.get('files'),
               'interfaces': record.get('interfaces'), 'decisions': record.get('decisions'),
               'remaining_issues': record.get('remaining_issues'), 'context': record.get('context'),
               'verification': record.get('verification'), 'review_state': review.get('state'),
               'freshness': freshness, 'warning': '', 'note': HANDOVER_NOTE}
    return payload


# --------------------------------------------------- prerequisite dispatch input

MAX_PREREQ_CHARS = 8000
MAX_PREREQ_FILES = 20
PREREQ_LABELS = (('接口', 'interfaces'), ('关键决策', 'decisions'),
                 ('测试证据', 'test_evidence'), ('遗留问题', 'remaining_issues'),
                 ('上下文', 'context'))


def _prereq_delivery(stage, project=None):
    """The freshest accepted handover bound to `stage`, or a blocking error."""
    accepted_without_handover = []
    for task_id in reversed(list(stage.get('task_ids') or [])):
        try:
            run = registry.task_dir(task_id, project)
        except ValueError:
            continue
        if _review_accepted(run) is None:
            continue
        record, _error = safe_load(run)
        if record is None:
            accepted_without_handover.append(task_id)
            continue
        if _clean(record.get('stage_id'), 120) != _clean(stage.get('id'), 120):
            raise HandoverError('交接记录的阶段与前置阶段不一致，已拒绝：' + task_id)
        if _clean(record.get('goal_id'), 120) != _clean(stage.get('goal_id'), 120):
            raise HandoverError('交接记录的目标与前置阶段不一致，已拒绝：' + task_id)
        freshness = verify(run, record)
        if freshness['stale']:
            raise HandoverError('前置阶段成果交接已失效，禁止依赖派发：' +
                                (stage.get('title') or stage.get('id') or '') +
                                '（' + freshness['message'] + '）')
        return {'task_id': task_id, 'stage_id': stage.get('id'),
                'title': stage.get('title') or stage.get('id') or '',
                'summary': record.get('summary') or '',
                'files': record.get('files') or [], 'interfaces': record.get('interfaces') or [],
                'decisions': record.get('decisions') or [], 'test_evidence': record.get('test_evidence') or [],
                'remaining_issues': record.get('remaining_issues') or [],
                'context': record.get('context') or []}
    title = stage.get('title') or stage.get('id') or ''
    if accepted_without_handover:
        raise HandoverError('前置阶段缺少已验收的成果交接，禁止依赖派发：' + title +
                            '（任务 ' + '、'.join(accepted_without_handover) + '）')
    raise HandoverError('前置阶段尚未产生可用的已验收成果交接，禁止依赖派发：' + title)


def prerequisite_handovers(stage_id, project=None):
    """Validated handovers for explicitly declared direct prerequisites.

    Missing, mismatched or stale prerequisite handovers raise HandoverError so a
    dependent dispatch is blocked before any new charged request is queued.  An
    explicit ``project`` resolves the consumer and its prerequisites from that
    project's own store, so the locator index is not a precondition.
    """
    import stage_store
    consumer = stage_store.get_stage(stage_id, project)
    if consumer is None:
        raise HandoverError('阶段不存在：' + str(stage_id))
    seen, order, frontier = set(), [], list(consumer.get('depends_on') or [])
    while frontier:
        dep = frontier.pop(0)
        if dep in seen:
            continue
        seen.add(dep)
        row = stage_store.get_stage(dep, project)
        if row is None:
            raise HandoverError('前置阶段不存在：' + dep)
        order.append(row)
        # The immediate producer carries its own necessary decisions. Loading
        # all ancestors grows the prompt and makes old hashes stale after
        # legitimate downstream edits. Explicit references remain validated.
    return [_prereq_delivery(row, project) for row in order]


def prerequisite_prompt(stage_id, limit=MAX_PREREQ_CHARS, project=None):
    """Compact prompt section carrying validated prerequisite handovers.

    Only short structured fields and file digests are included; no whole
    messages, transcripts or logs, so the consumer starts a new session with
    the essential context instead of asking for the full history again.
    """
    blocks = prerequisite_handovers(stage_id, project)
    if not blocks:
        return ''
    lines = ['## 前置阶段成果交接（写入时按真实文件 SHA-256 核验，已独立验收）',
             '下面是本阶段可直接使用的前置成果；不要再次索要整段历史，也不要引用未列出的文件。']
    for entry in blocks:
        lines.append('')
        lines.append('### ' + entry['title'] + '（阶段 ' + entry['stage_id'] +
                     ' / 任务 ' + entry['task_id'] + '）')
        if entry['summary']:
            lines.append('- 摘要（调用者说明，不是验证证据）：' + entry['summary'])
        if entry['files']:
            rows = []
            for item in entry['files'][:MAX_PREREQ_FILES]:
                digest = str((item or {}).get('sha256') or '')[:12] if isinstance(item, dict) else ''
                rows.append(str((item or {}).get('path') if isinstance(item, dict) else item) +
                            '（sha256:' + digest + '）')
            lines.append('- 改动文件：' + '；'.join(rows))
        for label, key in PREREQ_LABELS:
            values = entry.get(key) or []
            if not values:
                continue
            if key == 'test_evidence':
                values = [(str(item.get('name')) + '[' + str(item.get('source')) + ']')
                          if isinstance(item, dict) else str(item) for item in values]
            lines.append('- ' + label + '：' + '；'.join(str(item) for item in values))
    text = '\n'.join(lines)
    if len(text) > limit:
        text = text[:limit] + '\n（前置交接已按长度截断）'
    return text


# ------------------------------------------------------------------------- CLI

def _evidence_file(path):
    if not path:
        return {}
    raw = Path(path).read_text(encoding='utf-8-sig')
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise HandoverError('证据文件需要是 UTF-8 JSON') from error
    if not isinstance(data, dict):
        raise HandoverError('证据文件需要是 JSON 对象')
    return data


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description='Durable cross-session handovers (local JSON).')
    parser.add_argument('--task', help='查看/创建交界面的任务 ID')
    parser.add_argument('--create', action='store_true')
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--evidence-file', type=Path, help='UTF-8 JSON：summary/interfaces/decisions/remaining_issues/context/files/test_evidence')
    parser.add_argument('--summary', default='')
    parser.add_argument('--files', default='', help='逗号分隔的相对路径，默认沿用任务范围')
    parser.add_argument('--reference', action='store_true')
    parser.add_argument('--source-task')
    parser.add_argument('--consumer-stage')
    parser.add_argument('--source-stage')
    parser.add_argument('--actor', default='main')
    args = parser.parse_args(argv)

    def render(value):
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))

    try:
        if args.reference:
            if not args.task or not args.source_task:
                raise HandoverError('引用需要 --task 与 --source-task')
            return render(resolve_reference(registry.task_dir(args.task), args.source_task,
                                            consumer_stage_id=args.consumer_stage,
                                            source_stage_id=args.source_stage)) or 0
        if not args.task:
            raise HandoverError('请用 --task 指定任务')
        run = registry.task_dir(args.task)
        if not run.is_dir():
            raise HandoverError('任务不存在')
        if args.verify:
            return render(verify(run)) or 0
        if args.create:
            data = _evidence_file(args.evidence_file)
            files = [part.strip() for part in args.files.replace('；', ',').replace(',', ';').split(';') if part.strip()]
            render(create(run, summary=args.summary or data.get('summary', ''),
                          interfaces=data.get('interfaces') or [],
                          decisions=data.get('decisions') or [],
                          remaining_issues=data.get('remaining_issues') or [],
                          test_evidence=data.get('test_evidence') or [],
                          context=data.get('context') or [],
                          files=files if files else None,
                          actor=args.actor,
                          stage_id=data.get('stage_id'),
                          goal_id=data.get('goal_id')))
            return 0
        record, error = safe_load(run)
        if record is None:
            if error:
                raise HandoverError(error)
            return render({'task_id': args.task, 'handover': None,
                           'message': '尚无成果交接；仅在独立验收通过后可创建。'}) or 0
        render({'task_id': args.task, 'handover': record, 'freshness': verify(run, record)})
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
