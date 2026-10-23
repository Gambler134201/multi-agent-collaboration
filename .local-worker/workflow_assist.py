"""Efficiency helpers for the local Model Desk (read-only, no model calls).

Five small, usable mechanisms layered on the existing authorities
(``task.json`` / ``review.json`` / ``handover.json`` / ``stages.db``):

* ``delivery_packet``    compact standardized finished-task packet: real
  identity, execution vs independent acceptance, changed files, executor claims
  kept apart from independent evidence, failed/unverified criteria and context
  pointers.  Missing or corrupt evidence stays ``unknown``/``error`` and is
  never reported as passed.
* ``acceptance_plan``    deterministic selection of project acceptance profiles
  (``acceptance_profiles.json``).  Supplied command lines are data that is
  printed, never executed, and nothing here records acceptance.
* ``delegation_plan``    inspectable threshold aid: trivial work may stay with
  GPT; scoped understandable execution may be a delegation candidate; ambiguity,
  architecture, security, authorization and project-isolation work needs a GPT
  determination first.  It is a planning aid, never an authorization/candidate/
  budget/capability gate, and it never changes a model.
* ``stage_context``      short resume brief for a stage, built only from
  accepted, hash-verified handovers plus the real stage/goal records.  A stale,
  missing or corrupt prerequisite is refused, not summarised away.
* ``calibration_summary`` read-only regrouping of independently reviewed rework
  families through ``failure_classification`` + ``model_router``.  No score,
  savings or quota claim, no benchmark or training run.

Nothing here opens a socket, runs a shell command, writes a task/review record,
or touches model, secret, budget or workflow settings.  Packets and statistics
carry ``read_only: True``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROFILES_NAME = 'acceptance_profiles.json'
MAX_ITEMS = 40
MAX_DETAIL = 200
MAX_CRITERIA = 20

import delegation_policy as policy
import model_registry as registry
import execution_result
import project_storage

CONSEQUENTIAL_KEYS = ('architecture', 'security', 'authorization', 'project_isolation')
CONSEQUENTIAL_LABELS = {
    'architecture': '架构/接口决策',
    'security': '安全',
    'authorization': '授权/权限',
    'project_isolation': '项目隔离',
}


# ----------------------------------------------------------------- small utils

def _short(value, limit=MAX_DETAIL):
    return ' '.join(str(value if value is not None else '').split())[:limit]


def _items(value, limit=MAX_ITEMS, item_limit=300):
    out = []
    for item in value or ():
        clean = _short(item, item_limit)
        if clean and clean not in out:
            out.append(clean)
        if len(out) >= limit:
            break
    return out


def _run_for(task_id, project=None):
    try:
        return Path(registry.task_dir(task_id, project))
    except ValueError as error:
        raise ValueError('任务 ID 不正确：' + str(error)) from error


def _file_rows(files, limit=20):
    rows = []
    for item in files or ():
        if isinstance(item, dict):
            rows.append({'path': _short(item.get('path'), 300),
                         'sha256': _short(item.get('sha256'), 12)})
        else:
            rows.append({'path': _short(item, 300), 'sha256': ''})
        if len(rows) >= limit:
            break
    return rows


# -------------------------------------------------------------- delivery packet

def delivery_packet(task_id, project=None, task=None):
    """Compact, standardized delivery packet for one task (finished or not)."""
    hint = task if isinstance(task, dict) else {}
    try:
        run = _run_for(task_id, project)
    except ValueError as error:
        return {'schema': 1, 'status': 'error', 'task_id': task_id,
                'identity': {'task_id': task_id, 'project': project or None},
                'acceptance': {'state': 'error', 'independently_accepted': False,
                               'note': '任务身份无法解析'},
                'errors': [str(error)], 'unknowns': ['identity'], 'read_only': True}

    errors = []
    unknown = []
    record = {}
    path = run / 'task.json'
    if path.is_file():
        try:
            loaded = registry.read_task(run)
            record = loaded if isinstance(loaded, dict) else {}
            if not record:
                errors.append('task.json 结构不正确')
        except (OSError, ValueError) as error:
            errors.append('task.json 无法读取：' + _short(error))
    else:
        errors.append('task.json 不存在，身份来自面板回执而非真实记录')
    if not record:
        unknown.append('local_task_record_missing')
    identity_fault = False
    record_id = str(record.get('id') or '').strip() if record else ''
    if record and record_id != task_id:
        # The record was renamed/relabelled: its content and any review beside
        # it can no longer be attributed to the requested identity.
        identity_fault = True
        errors.append('任务记录身份不一致：task.json id=' + record_id + '，请求=' + str(task_id))

    identity_source = record or hint
    project_value = (record.get('project') or hint.get('project')
                     or registry.project_of_run(run) or project or None)
    identity = {
        'task_id': task_id,
        'title': _short(identity_source.get('title'), 160),
        'project': _short(project_value, 500),
        'status': identity_source.get('status'),
        'kind': identity_source.get('kind'),
        'engine': identity_source.get('engine'),
        'model_id': identity_source.get('model_id'),
        'model': identity_source.get('model') or identity_source.get('model_name'),
        'stage_id': identity_source.get('stage_id'),
        'goal_id': identity_source.get('goal_id'),
        'parent_task_id': identity_source.get('parent_task_id'),
        'created_at': identity_source.get('created_at'),
        'identity_source': 'task.json' if record else 'panel-hint',
    }
    if identity_fault:
        identity['record_task_id'] = record_id
        identity['identity_mismatch'] = True

    review, review_error = policy.safe_load(run)
    acceptance_source = 'review.json'
    if review_error:
        errors.append('review.json 无法读取：' + _short(review_error))
    elif review is None and not record and isinstance(hint.get('review'), dict):
        # Only when there is no local task record at all (attached waiter): the
        # panel row is real data, not a synthesized guess.  It is shown as a
        # claim only and can never become verified independent evidence.
        review = hint.get('review')
        acceptance_source = 'panel-hint'
    if review is not None and not isinstance(review, dict):
        review, review_error = None, 'review.json 结构不正确'
        errors.append(review_error)

    if review_error:
        acceptance = {'state': 'error', 'label': '验收记录不可读',
                      'independently_accepted': False, 'final_accepted': False,
                      'main_takeover': False, 'source': acceptance_source,
                      'note': '损坏/不可读保持错误，绝不当作通过'}
        unknown.append('acceptance')
    elif review is None:
        acceptance = {'state': None, 'label': '没有验收记录（未委派或旧任务）',
                      'independently_accepted': False, 'final_accepted': False,
                      'main_takeover': False, 'source': 'none',
                      'note': '缺失即为未知，不能视为通过'}
        unknown.append('acceptance')
    else:
        state = review.get('state')
        executor_block = review.get('executor') if isinstance(review.get('executor'), dict) else {}
        acceptance = {
            'state': state,
            'label': policy.STATE_LABELS.get(state, state or ''),
            'independently_accepted': state in policy.ACCEPTED_STATES,
            'final_accepted': state == policy.FINAL_ACCEPTED,
            'main_takeover': bool(review.get('final')) or state == policy.FINAL_ACCEPTED,
            'executor_status': (executor_block.get('status')
                                or review.get('executor_status')),
            'rework_count': review.get('rework_count'),
            'reviewer_notes': _short(review.get('reviewer_notes')),
            'blocked_reason': _short(review.get('blocked_reason')),
            'source': acceptance_source,
        }

    # A claimed pass is only trusted when the authoritative record is coherent
    # and matches the requested identity.  A hand-edited, renamed or merely
    # attached record stays visible but is never promoted to independent proof.
    if acceptance.get('state') in policy.ACCEPTED_STATES and isinstance(review, dict):
        reasons = []
        review_task_id = str(review.get('task_id') or '').strip()
        if review_task_id != task_id:
            reasons.append('review.json 的 task_id=' + review_task_id +
                           ' 与请求=' + str(task_id) + ' 不一致')
        if acceptance_source == 'panel-hint':
            reasons.append('验收来自面板回执而非本地 review.json，不能算独立验收证据')
        if not record:
            reasons.append('缺少本地 task.json，无法核对验收记录身份')
        if identity_fault:
            reasons.append('任务记录身份与请求不一致，验收不能成立')
        reasons.extend(policy.acceptance_problems(review))
        if reasons:
            acceptance['independently_accepted'] = False
            acceptance['final_accepted'] = False
            acceptance['trusted'] = False
            acceptance['problems'] = reasons
            errors.extend('验收证据不可信：' + item for item in reasons)
            unknown.append('acceptance_evidence')
        else:
            acceptance['trusted'] = True
    elif 'trusted' not in acceptance:
        acceptance['trusted'] = False

    criteria = []
    independent = []
    claims = []
    if isinstance(review, dict):
        criterion_rows = review.get('criteria')
        for item in (criterion_rows if isinstance(criterion_rows, list) else [])[:MAX_CRITERIA]:
            if not isinstance(item, dict):
                continue
            rows = []
            evidence_rows = item.get('evidence')
            for entry in (evidence_rows if isinstance(evidence_rows, list) else [])[:5]:
                if not isinstance(entry, dict):
                    continue
                source = str(entry.get('source') or '').lower()
                row = {'source': source, 'exit_code': entry.get('exit_code'),
                       'detail': _short(entry.get('detail'))}
                rows.append(row)
                if source in policy.INDEPENDENT_SOURCES:
                    independent.append({'criterion': item.get('id'), **row})
                elif source in policy.EXECUTOR_SOURCES:
                    claims.append({'criterion': item.get('id'), **row})
            criteria.append({'id': item.get('id'), 'text': _short(item.get('text'), 300),
                             'status': item.get('status'), 'evidence': rows,
                             'note': _short(item.get('note'))})

    # Independent-looking evidence from a claimed-but-untrusted acceptance is
    # kept separate so it can never be read as verification of this identity.
    untrusted_independent = []
    if acceptance.get('state') in policy.ACCEPTED_STATES and not acceptance.get('trusted'):
        untrusted_independent, independent = independent, []

    executor_report = None
    if isinstance(review, dict):
        executor_block = review.get('executor') if isinstance(review.get('executor'), dict) else {}
        if executor_block or review.get('executor_status'):
            test = executor_block.get('test') if isinstance(executor_block.get('test'), dict) else {}
            executor_report = {
                'status': executor_block.get('status') or review.get('executor_status'),
                'model_id': executor_block.get('model_id'),
                'changed_files': _items(executor_block.get('changed_files'), 40),
                'worker_test_exit_code': test.get('exit_code'),
                'note': '执行器自报与 worker 自测：声明，不是独立验收证据',
            }

    changed = _items(record.get('changed_files') or hint.get('changed_files'), 40)
    test_result = record.get('test_result') if isinstance(record.get('test_result'), dict) else hint.get('test_result')
    worker_exit = None
    if isinstance(test_result, dict):
        worker_exit = test_result.get('exit_code')
    if executor_report and executor_report.get('worker_test_exit_code') is not None:
        worker_exit = executor_report['worker_test_exit_code']
    execution = {
        'task_status': identity['status'],
        'executor_status': acceptance.get('executor_status') or (executor_report or {}).get('status'),
        'changed_files': changed,
        'worker_test_exit_code': worker_exit,
        'model_calls': record.get('model_calls') if isinstance(record.get('model_calls'), int) else hint.get('model_calls'),
        'note': '执行完成或 worker 测试退出码不是验收通过；验收状态只看 acceptance',
    }

    failed = [item for item in criteria if item['status'] == policy.FAIL]
    unverified = [item for item in criteria if item['status'] not in (policy.PASS, policy.FAIL)]
    if unverified:
        unknown.append('unverified_criteria')

    handover_state = {'present': None, 'valid': None, 'stale': None, 'error': ''}
    try:
        import task_handover as handover
    except Exception as error:  # pragma: no cover - import guard only
        handover = None
        handover_state['error'] = _short(error)
    if handover is not None:
        hand_record, hand_error = handover.safe_load(run)
        if hand_error:
            handover_state = {'present': None, 'valid': None, 'stale': None,
                              'error': _short(hand_error)}
            errors.append('handover.json 无法读取：' + _short(hand_error))
            unknown.append('handover')
        elif hand_record is None:
            handover_state = {'present': False, 'valid': None, 'stale': None, 'error': ''}
        else:
            try:
                freshness = handover.verify(run, hand_record)
            except Exception as error:
                freshness = {'valid': False, 'stale': True, 'changed': [], 'missing': [],
                             'message': _short(error)}
            handover_state = {
                'present': True,
                'valid': bool(freshness.get('valid')),
                'stale': bool(freshness.get('stale')),
                'changed': _items(freshness.get('changed'), 10),
                'missing': _items(freshness.get('missing'), 10),
                'message': _short(freshness.get('message')),
                'summary': _short(hand_record.get('summary'), 300),
                'summary_is_claim': True,
                'files': _file_rows(hand_record.get('files')),
                'interfaces': _items(hand_record.get('interfaces'), 10),
                'remaining_issues': _items(hand_record.get('remaining_issues'), 10),
                'verification_source': (hand_record.get('verification') or {}).get('source'),
            }
            if not freshness.get('valid'):
                unknown.append('handover_stale')

    failure = None
    if isinstance(review, dict):
        try:
            import failure_classification as classification
            finding = classification.classify({**(record or hint),
                                               'review': policy.panel_summary(review)})
            failure = {'outcome': finding.get('outcome'), 'reason': finding.get('reason'),
                       'label': finding.get('label'), 'source': finding.get('source'),
                       'verified': finding.get('verified'),
                       'counts_as_model_quality': finding.get('counts_as_model_quality')}
        except Exception:
            failure = None

    pointers = {
        'task_record': str(path),
        'review_record': str(run / policy.REVIEW_NAME),
        'handover_record': str(run / 'handover.json'),
        'commands': ['review_task.py --task ' + task_id,
                     'task_handover.py --task ' + task_id,
                     'workflow_assist.py stage-context --task ' + task_id,
                     'workflow_assist.py acceptance-plan --task ' + task_id],
    }

    return {
        'schema': 1,
        'status': 'error' if identity_fault or any('无法读取' in item or '不存在' in item for item in errors) else
                  ('partial' if errors else 'ok'),
        'task_id': task_id,
        'identity': identity,
        'execution': execution,
        'execution_result': execution_result.project(
            record, {'state': acceptance.get('state'), 'acceptance_valid': acceptance.get('trusted') is True}),
        'acceptance': acceptance,
        'criteria': criteria,
        'failed_criteria': failed,
        'unverified_criteria': unverified,
        'evidence': {
            'independent': independent[:MAX_ITEMS],
            'untrusted_independent': untrusted_independent[:MAX_ITEMS],
            'executor_claims': claims[:MAX_ITEMS],
            'executor_report': executor_report,
            'independent_count': len(independent),
            'claim_count': len(claims),
            'independent_trusted': bool(acceptance.get('trusted')),
            'note': '执行器自报/自测是声明；只有 independent 列表计为独立验收证据，'
                    'untrusted_independent 仅供排查、不能算通过',
        },
        'handover': handover_state,
        'failure': failure,
        'context_pointers': pointers,
        'unknowns': unknown,
        'errors': errors,
        'read_only': True,
        'note': '缺失或损坏的证据保持未知/错误，绝不合成通过；不含 prompt、密钥或大段日志。',
    }


# ---------------------------------------------------------- acceptance profiles

def profiles_path(path=None):
    return Path(path) if path else HERE / PROFILES_NAME


def load_profiles(path=None):
    target = profiles_path(path)
    try:
        data = json.loads(target.read_text(encoding='utf-8-sig'))
    except FileNotFoundError as error:
        raise ValueError('验收档案文件不存在：' + str(target)) from error
    except ValueError as error:
        raise ValueError('验收档案不是有效 JSON：' + str(target)) from error
    if not isinstance(data, dict) or data.get('schema') != 1 or not isinstance(data.get('profiles'), list):
        raise ValueError('验收档案结构不正确（需要 schema=1 与 profiles 数组）')
    return data


def _norm_path(value):
    text = str(value or '').strip().replace('\\', '/')
    while text.startswith('./'):
        text = text[2:]
    return text.lower()


def _match_kind(candidate, name):
    """How strongly a supplied file name matches one profile candidate.

    ``path``     the candidate and the supplied file agree on the directory
                 (or the candidate names a bare file, which is a
                 directory-agnostic subsystem label by project convention),
    ``basename`` only the basename agreed while the candidate pinned a
                 directory -- real but weaker evidence,
    ``None``     no match.
    """
    wanted = _norm_path(candidate)
    if not wanted:
        return None
    if name == wanted:
        return 'path'
    base = wanted.split('/')[-1]
    if not base or name.split('/')[-1] != base:
        return None
    if '/' not in wanted:
        return 'path'
    if name.endswith('/' + wanted):
        return 'path'
    return 'basename'


def _profile_hits(profile, names, text):
    """Return ``(hits, matched)`` for one profile.

    ``matched`` maps each supplied file to the strongest match kind, so the
    caller can report which files a profile really covers and which were only
    recognised by basename.  No command is executed here.
    """
    match = profile.get('match') if isinstance(profile.get('match'), dict) else {}
    hits = []
    matched = {}
    candidates = [str(item) for item in match.get('files') or []]
    for name in names:
        best = None
        for candidate in candidates:
            kind = _match_kind(candidate, name)
            if kind is None:
                continue
            if kind == 'path':
                hits.append('file:' + candidate)
                best = 'path'
                break
            best = best or 'basename'
            hits.append('basename:' + candidate)
        if best:
            matched[name] = best
    for candidate in match.get('keywords') or []:
        word = str(candidate).lower()
        if word and word in text:
            hits.append('keyword:' + str(candidate))
    return hits, matched


def _render_profile(profile):
    def checks(key):
        rows = []
        for item in profile.get(key) or []:
            if isinstance(item, dict):
                rows.append({'purpose': _short(item.get('purpose'), 160),
                             'argv': [str(part) for part in item.get('argv') or []]
                             if isinstance(item.get('argv'), (list, tuple)) else [],
                             'command': _short(item.get('command'), 300)})
            else:
                rows.append({'purpose': '', 'argv': [], 'command': _short(item, 300)})
        return rows
    return {
        'id': profile.get('id'),
        'title': profile.get('title'),
        'subsystem': profile.get('subsystem'),
        'worker_tests': checks('worker_tests'),
        'independent_commands': checks('independent_commands'),
        'risk_triggers': _items(profile.get('risk_triggers'), 10, 300),
    }


def acceptance_plan(files=(), keywords=(), task_id=None, project=None,
                    profiles=None, text=''):
    """Deterministically select acceptance profiles; never executes or accepts."""
    data = profiles if isinstance(profiles, dict) else load_profiles()
    notes = []
    names = [_norm_path(name) for name in files if str(name or '').strip()]
    if task_id:
        try:
            run = _run_for(task_id, project)
        except ValueError as error:
            raise ValueError(str(error)) from error
        record = {}
        if (run / 'task.json').is_file():
            try:
                loaded = registry.read_task(run)
                record = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError) as error:
                notes.append('task.json 读取失败：' + _short(error))
        if not names:
            names = [_norm_path(name) for name in
                     (record.get('changed_files') or record.get('files') or [])
                     if str(name or '').strip()]
        if not text:
            text = str(record.get('title') or '') + ' ' + str(record.get('summary') or '')
    text = (str(text) + ' ' + ' '.join(str(word) for word in keywords)).lower()

    by_id = {}
    order = []
    coverage = {}
    for profile in data['profiles']:
        if not isinstance(profile, dict) or not profile.get('id'):
            continue
        by_id[profile['id']] = profile
        hits, matched = _profile_hits(profile, names, text)
        for hit in hits:
            if profile['id'] not in order:
                order.append(profile['id'])
            notes.append(profile['id'] + ' ← ' + hit)
        for name, kind in matched.items():
            if kind == 'path' or name not in coverage:
                coverage[name] = kind

    expanded = []
    for trigger in data.get('triggers') or []:
        if not isinstance(trigger, dict):
            continue
        need = [str(item) for item in trigger.get('when_matched_profiles') or []]
        minimum = trigger.get('when_min_profiles')
        hit = bool(need) and all(item in order for item in need)
        if minimum is not None:
            try:
                hit = hit or len(order) >= int(minimum)
            except (TypeError, ValueError):
                pass
        add = trigger.get('add_profile')
        if hit and add and add in by_id and add not in order:
            order.append(add)
            expanded.append(add)
            notes.append(str(trigger.get('reason') or ('风险触发：增加 ' + str(add))))

    # A supplied file that matched no profile (or only by basename) is a real
    # coverage gap: say so and widen to the conservative fallback instead of
    # quietly reporting a narrow, fully-covered plan.
    unmatched = [name for name in names if name not in coverage]
    weak = [name for name in names if coverage.get(name) == 'basename']
    unknown_scope = (not names) or bool(unmatched) or bool(weak)
    fallback = data.get('fallback_profile')
    gap_profiles = []
    if unknown_scope and fallback in by_id and fallback not in order:
        order.append(fallback)
        gap_profiles.append(fallback)
        notes.append('文件范围未被完整覆盖（空白未匹配或仅 basename 命中），加入保守回退档案：' +
                     str(fallback))
    if not order:
        if fallback in by_id:
            order = [fallback]
            gap_profiles.append(fallback)
        notes.append('没有匹配到具体档案，按保守回退处理')

    selected = [_render_profile(by_id[pid]) for pid in order if pid in by_id]
    return {
        'schema': 1,
        'project': project or None,
        'task_id': task_id,
        'files': names[:MAX_ITEMS],
        'unknown_scope': unknown_scope,
        'conservative': bool(unknown_scope or expanded or gap_profiles),
        'selected_profiles': selected,
        'expanded_profiles': expanded,
        'gap_profiles': gap_profiles,
        'unmatched_files': unmatched[:MAX_ITEMS],
        'weakly_matched_files': weak[:MAX_ITEMS],
        'scope_coverage': [{'file': name, 'match': coverage.get(name) or 'none'}
                           for name in names[:MAX_ITEMS]],
        'notes': notes + [
            '选择是确定性的：按档案顺序匹配文件/关键词，未匹配的文件会明确标出并回退到通用档案。',
            'scope_coverage 逐文件列出命中强度：basename 只证明文件名，不证明整条路径已被覆盖。',
            '命令字段只是文本计划，本工具从不执行，也不记录验收结论。',
            'worker_tests 是执行器自测（声明）；independent_commands 才是独立验收候选。',
        ],
        'executes_commands': False,
        'marks_acceptance': False,
        'read_only': True,
    }


# ---------------------------------------------------------- delegation planning

def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', '是'}


def delegation_plan(requirements):
    """Inspectable delegation threshold aid.  Never dispatches or reroutes."""
    req = requirements if isinstance(requirements, dict) else {}
    ambiguity = str(req.get('ambiguity') or 'unknown').strip().lower()
    if ambiguity not in {'low', 'medium', 'high', 'unknown'}:
        ambiguity = 'unknown'
    consequential = {key: _as_bool(req.get(key)) for key in CONSEQUENTIAL_KEYS}
    normalized = {
        'trivial': _as_bool(req.get('trivial')),
        'scoped': _as_bool(req.get('scoped')),
        'understandable': _as_bool(req.get('understandable')),
        'ambiguity': ambiguity,
        'capability_judged': _as_bool(req.get('capability_judged')),
        'policy_allowed': _as_bool(req.get('policy_allowed')),
        'expected_benefit': _as_bool(req.get('expected_benefit')),
        **consequential,
    }

    rationale = []
    triggered = [CONSEQUENTIAL_LABELS[key] for key in CONSEQUENTIAL_KEYS if consequential[key]]
    if triggered:
        recommendation = 'gpt_decision_first'
        rationale.append('涉及高风险决策（' + '、'.join(triggered) +
                         '）；先由 GPT 确定方案、边界与授权，再考虑是否派发。')
    elif ambiguity in {'high', 'unknown'}:
        recommendation = 'gpt_decision_first'
        rationale.append('需求仍有歧义或尚未澄清（ambiguity=' + ambiguity +
                         '）；先由 GPT 澄清目标、边界与验收条件。')
    elif normalized['trivial']:
        recommendation = 'gpt_owned'
        rationale.append('工作很小且低风险：留在 GPT 直接处理通常比派发更省。')
    elif (normalized['scoped'] and normalized['understandable']
          and normalized['capability_judged'] and normalized['policy_allowed']
          and normalized['expected_benefit']):
        recommendation = 'delegate_candidate'
        rationale.append('范围清晰、可理解、已有实测能力判定、策略允许且本任务有预期收益；'
                         '可作为委派候选（仍需通过既有授权/候选/预算/次数/能力门禁）。')
    else:
        missing = [name for name in ('scoped', 'understandable', 'capability_judged',
                                     'policy_allowed', 'expected_benefit')
                   if not normalized[name]]
        recommendation = 'gpt_owned'
        rationale.append('未满足委派门槛（缺少或不满足：' +
                         ('、'.join(missing) or '未知') + '）；保守地留在 GPT。')

    checklist = [
        '先写清目标、可改文件范围与可判定的验收条件（review_task.py --init）。',
        '确认既有授权/候选模型/预算/次数/能力门禁仍然生效；本规划不替代它们。',
        '若派发：走阶段或任务记录，保持项目/目标/文件边界与最多一次自动整改。',
        '完成后由 GPT 独立验收：执行器自报与自测不算证据。',
    ]
    if recommendation == 'delegate_candidate':
        checklist.append('派发前用 acceptance-plan 选出该子系统的独立验收命令。')
    elif recommendation == 'gpt_decision_first':
        checklist.append('先给出决策记录，再决定是否交由 judged-capable worker 执行。')

    return {
        'schema': 1,
        'recommendation': recommendation,
        'inputs': normalized,
        'rationale': rationale,
        'checklist': checklist,
        'declarations_are_not_verification': True,
        'does_not_replace_gates': ['authorization', 'candidate', 'budget', 'call_limit', 'capability'],
        'context_size_is_not_ability': True,
        'read_only': True,
        'note': '人类/GPT 的风险声明只是声明，不是语义验证；本工具不派发、不写入、不静默改模型。',
    }


# ---------------------------------------------------------------- stage context

def _prerequisite_brief(entry):
    return {
        'task_id': entry.get('task_id'),
        'stage_id': entry.get('stage_id'),
        'title': _short(entry.get('title'), 160),
        'summary': _short(entry.get('summary'), 400),
        'summary_is_claim': True,
        'files': _file_rows(entry.get('files')),
        'interfaces': _items(entry.get('interfaces'), 10),
        'decisions': _items(entry.get('decisions'), 10),
        'test_evidence': [
            {'name': _short(item.get('name'), 200), 'source': item.get('source'),
             'exit_code': item.get('exit_code'), 'counts_as_evidence': item.get('counts_as_evidence')}
            for item in (entry.get('test_evidence') or []) if isinstance(item, dict)][:10],
        'remaining_issues': _items(entry.get('remaining_issues'), 10),
        'context': _items(entry.get('context'), 10),
    }


def stage_context(stage_id=None, task_id=None, project=None):
    """Short resume brief for one stage from verified handovers and real records.

    Viewing metadata and being allowed to resume are separate answers: ``status``
    says whether the brief is coherent, ``resumable`` is the permission to
    continue/dispatch from it.  A stale, missing or corrupt current-stage
    handover blocks resumption and is never presented as a verified summary.
    """
    import task_handover as handover
    import stage_store

    accepted_states = getattr(policy, 'ACCEPTED_STATES', {'accepted', 'final_accepted'})
    errors = []
    blocked_reasons = []
    identity_fault = False
    resolved_project = project
    record = {}
    task_project = task_stage_id = task_goal_id = None

    if task_id:
        try:
            run = _run_for(task_id, project)
        except ValueError as error:
            return {'schema': 1, 'status': 'error', 'resumable': False,
                    'errors': [str(error)], 'read_only': True}
        if (run / 'task.json').is_file():
            try:
                loaded = registry.read_task(run)
                record = loaded if isinstance(loaded, dict) else {}
                if not record:
                    errors.append('task.json 结构不正确')
            except (OSError, ValueError) as error:
                errors.append('task.json 无法读取：' + _short(error))
        else:
            errors.append('task.json 不存在，无法用真实项目解析任务身份')
        if record:
            record_id = str(record.get('id') or '').strip()
            if record_id and record_id != task_id:
                identity_fault = True
                errors.append('任务记录身份不一致：task.json id=' + record_id +
                              '，请求=' + str(task_id))
            task_project = str(record.get('project') or '').strip() or None
            task_stage_id = record.get('stage_id') or None
            task_goal_id = record.get('goal_id') or None
        if stage_id and task_stage_id and str(stage_id) != str(task_stage_id):
            identity_fault = True
            errors.append('请求阶段与任务记录的 stage_id 不一致，拒绝自选阶段身份')
        if not stage_id:
            stage_id = task_stage_id
        if not stage_id:
            return {'schema': 1, 'status': 'error', 'resumable': False,
                    'task_id': task_id, 'errors': errors + [
                        '该任务没有 stage_id，无法给出阶段上下文；请改用 '
                        'workflow_assist.py packet --task ' + task_id],
                    'read_only': True}
        if task_project:
            resolved_project = task_project
        if project and task_project:
            try:
                same_project = project_storage.key(project) == project_storage.key(task_project)
            except (ValueError, OSError, TypeError):
                same_project = False
            if not same_project:
                identity_fault = True
                errors.append('任务记录的项目与请求项目不一致，拒绝跨项目读取')
    if not stage_id:
        raise ValueError('需要 --stage 或带阶段的任务 --task')

    try:
        stage = stage_store.get_stage(stage_id, resolved_project)
    except Exception as error:
        return {'schema': 1, 'status': 'error', 'resumable': False, 'stage_id': stage_id,
                'project': resolved_project,
                'errors': errors + ['阶段读取失败：' + _short(error)], 'read_only': True}
    if stage is None:
        return {'schema': 1, 'status': 'error', 'resumable': False, 'stage_id': stage_id,
                'project': resolved_project,
                'errors': errors + ['阶段不存在或不在该项目：' + str(stage_id)],
                'read_only': True}
    if task_stage_id and str(task_stage_id) != str(stage.get('id')):
        identity_fault = True
        errors.append('任务记录的 stage_id 与解析到的阶段不一致，拒绝读取')
    if task_goal_id and stage.get('goal_id') and str(task_goal_id) != str(stage.get('goal_id')):
        identity_fault = True
        errors.append('任务记录的目标与阶段目标不一致，拒绝读取')
    if identity_fault:
        return {'schema': 1, 'status': 'error', 'resumable': False, 'stage_id': stage_id,
                'project': resolved_project, 'task_id': task_id,
                'errors': errors, 'blocked_reasons': errors, 'read_only': True,
                'note': '任务/项目/目标/阶段身份互相矛盾，已拒绝给出可信上下文。'}

    goal = None
    if stage.get('goal_id'):
        try:
            goal = stage_store.get_goal(stage['goal_id'], resolved_project)
        except Exception as error:
            errors.append('目标读取失败：' + _short(error))
    readiness_error = ''
    try:
        ready = stage_store.readiness(stage_id, resolved_project)
    except Exception as error:
        ready = None
        readiness_error = _short(error)
        errors.append('就绪状态读取失败：' + readiness_error)

    independent = []
    try:
        blocks = handover.prerequisite_handovers(stage_id, resolved_project)
        prerequisites = {'status': 'ok', 'items': [_prerequisite_brief(item) for item in blocks]}
        for item in blocks:
            for entry in item.get('test_evidence') or []:
                if isinstance(entry, dict) and entry.get('counts_as_evidence'):
                    independent.append({'task_id': item.get('task_id'),
                                        'name': _short(entry.get('name'), 200),
                                        'source': entry.get('source'),
                                        'exit_code': entry.get('exit_code'),
                                        'origin': 'prerequisite_handover'})
    except (ValueError, OSError) as error:
        message = _short(error, 500)
        prerequisites = {'status': 'blocked', 'items': [], 'error': message}
        blocked_reasons.append('前置成果交接不可用，拒绝依赖：' + message)

    stage_tasks = []
    own_gap = []
    own_fresh_accepted = False
    for tid in list(reversed(list(stage.get('task_ids') or [])))[:5]:
        try:
            run = registry.task_dir(tid, resolved_project)
        except (ValueError, OSError):
            continue
        review, review_error = policy.safe_load(run)
        hand_record, hand_error = handover.safe_load(run)
        accepted = isinstance(review, dict) and review.get('state') in accepted_states
        row = {'task_id': tid, 'review_state': (review or {}).get('state'),
               'review_error': _short(review_error), 'handover': None,
               'handover_error': _short(hand_error)}
        fresh = None
        if hand_record is not None:
            freshness = handover.verify(run, hand_record)
            fresh = bool(freshness.get('valid'))
            claim = _short(hand_record.get('summary'), 400)
            row['handover'] = {
                'fresh': fresh,
                'stale': bool(freshness.get('stale')),
                'trusted': bool(fresh and accepted),
                # A stale/invalid deliverable must not be served as the resume
                # summary; its text is kept only as an explicitly-labelled claim.
                'summary': claim if fresh else '',
                'stale_summary': '' if fresh else claim,
                'summary_is_claim': True,
                'interfaces': _items(hand_record.get('interfaces'), 10),
                'decisions': _items(hand_record.get('decisions'), 10),
                'remaining_issues': _items(hand_record.get('remaining_issues'), 10),
                'context': _items(hand_record.get('context'), 10),
                'files': _file_rows(hand_record.get('files')),
            }
        if accepted and not fresh:
            own_gap.append(tid)
            detail = _short(hand_error) or '成果交接缺失，或文件已改动而失效'
            blocked_reasons.append('当前阶段已验收任务 ' + tid + ' 的成果交接不可用：' + detail)
        elif accepted and fresh:
            own_fresh_accepted = True
            for entry in hand_record.get('test_evidence') or []:
                if isinstance(entry, dict) and entry.get('counts_as_evidence'):
                    independent.append({'task_id': tid, 'name': _short(entry.get('name'), 200),
                                        'source': entry.get('source'),
                                        'exit_code': entry.get('exit_code')})
        # Review evidence only when it is accepted, coherent and still matches
        # the hashed deliverable; an incoherent or stale record can never back a
        # "verified" summary.
        if accepted and fresh and not policy.acceptance_problems(review):
            for item in review.get('criteria') or []:
                for entry in item.get('evidence') or []:
                    if (isinstance(entry, dict)
                            and str(entry.get('source') or '').lower() in policy.INDEPENDENT_SOURCES):
                        independent.append({'task_id': tid, 'criterion': item.get('id'),
                                            'source': entry.get('source'),
                                            'exit_code': entry.get('exit_code')})
        stage_tasks.append(row)

    stage_accepted = stage.get('status') in accepted_states
    if stage_accepted and not own_fresh_accepted:
        blocked_reasons.append('阶段已验收通过但没有可复用的新鲜成果交接，不能据此续作')

    hard_blocked = (prerequisites['status'] == 'blocked' or bool(own_gap)
                    or (stage_accepted and not own_fresh_accepted) or bool(readiness_error))
    if readiness_error:
        blocked_reasons.append('阶段就绪状态不可读，不能自动续作：' + readiness_error)
    readiness_reasons = list((ready or {}).get('reasons') or []) if isinstance(ready, dict) else []
    seen_reasons = []
    for reason in blocked_reasons + [str(item) for item in readiness_reasons]:
        if reason and reason not in seen_reasons:
            seen_reasons.append(reason)

    resumable = (not hard_blocked) and isinstance(ready, dict) and bool(ready.get('ready'))
    return {
        'schema': 1,
        'status': 'blocked' if hard_blocked else 'ok',
        'resumable': bool(resumable),
        'dispatchable': bool(resumable),
        'project': resolved_project,
        'task_id': task_id,
        'goal': ({'id': goal.get('id'), 'title': _short(goal.get('title'), 200),
                  'objective': _short(goal.get('objective'), 500), 'status': goal.get('status')}
                 if isinstance(goal, dict) else None),
        'stage': {
            'id': stage.get('id'), 'title': _short(stage.get('title'), 200),
            'objective': _short(stage.get('objective'), 500), 'status': stage.get('status'),
            'kind': stage.get('kind'), 'files': _items(stage.get('files'), 30),
            'acceptance': _items(stage.get('acceptance'), 20, 300),
            'depends_on': _items(stage.get('depends_on'), 20),
            'candidates': _items(stage.get('candidates'), 20),
            'fixed_model': stage.get('fixed_model'),
        },
        'readiness': ready,
        'prerequisites': prerequisites,
        'stage_handovers': stage_tasks,
        'stale_current_handovers': own_gap,
        'blocked_reasons': seen_reasons[:MAX_ITEMS],
        'independent_evidence': independent[:MAX_ITEMS],
        'errors': errors,
        'read_only': True,
        'note': '只使用已独立验收且哈希校验的交接；缺失/失效/损坏明确拒绝，不编造摘要，也不需要整段聊天历史。',
    }


# ------------------------------------------------------- calibration (read-only)

def _project_history(project):
    """One project's real records + review summaries (never a global scan).

    ``review.json`` is the authoritative acceptance record.  When it is corrupt
    or absent, a verdict embedded in ``task.json`` is a stale snapshot and must
    not be promoted back into evidence, so it is stripped and replaced with an
    explicit ``review_unreadable`` marker; classification then keeps the record
    unknown instead of counting it as a verified success.
    """
    rows, unreadable, stale_embeds, incoherent = [], 0, 0, 0
    for entry in project_storage.iter_task_dirs({'mode': 'project', 'project': project}):
        run = Path(entry['run'])
        if not (run / 'task.json').is_file():
            continue
        try:
            record = registry.read_task(run)
        except (OSError, ValueError):
            unreadable += 1
            continue
        if not isinstance(record, dict) or record.get('id', run.name) != run.name:
            unreadable += 1
            continue
        had_embed = ('review' in record or 'review_state' in record)
        record = {key: value for key, value in record.items()
                  if key not in ('review', 'review_state', 'review_updated_at')}
        review_file = run / policy.REVIEW_NAME
        review, review_error = policy.safe_load(run)
        if review_error:
            unreadable += 1
            record['review_unreadable'] = 'review.json 损坏：' + _short(review_error, 300)
        elif not isinstance(review, dict):
            if review is None and not review_file.is_file() and had_embed:
                stale_embeds += 1
                record['review_unreadable'] = ('review.json 缺失，任务内嵌的旧验收结论不能代替权威记录')
            elif review is None and review_file.is_file():
                unreadable += 1
                record['review_unreadable'] = 'review.json 存在但不可解析'
        else:
            state = review.get('state')
            problems = policy.acceptance_problems(review) if state in policy.ACCEPTED_STATES else []
            if problems:
                incoherent += 1
                record['review_unreadable'] = '验收通过记录不完整：' + '；'.join(problems)
            else:
                summary = policy.panel_summary(review)
                if isinstance(summary, dict):
                    record = {**record, 'review': summary}
        rows.append(record)
    return rows, unreadable, stale_embeds, incoherent


def _compact_risk(risk):
    samples = []
    for item in risk.get('samples') or []:
        if isinstance(item, dict):
            samples.append({'family': item.get('family'), 'task_id': item.get('task_id'),
                            'outcome': item.get('outcome'),
                            'source': item.get('source'), 'reason': item.get('reason')})
    verified = risk.get('verified_samples') or 0
    return {
        'model_id': risk.get('model_id'),
        'category': risk.get('category'),
        'families': risk.get('families'),
        'verified_samples': verified,
        'quality_rework_families': risk.get('quality_rework_families'),
        'quality_rework_rate': risk.get('quality_rework_rate'),
        'confidence': risk.get('confidence'),
        'insufficient_samples': verified < 5,
        'basis': risk.get('basis'),
        'retry_factor_range': [risk.get('retry_factor_low'), risk.get('retry_factor_high')],
        'excluded': risk.get('excluded'),
        'representative_families': samples,
        'notes': _items(risk.get('notes'), 10, 300),
    }


def calibration_summary(project, model_id=None, category=None):
    """Read-only calibration view over one project's reviewed rework families."""
    import failure_classification as classification
    import model_router

    if not project:
        raise ValueError('calibration 需要显式 --project；默认不扫描所有项目')
    project = project_storage.normalize(project)
    history, unreadable, stale_embeds, incoherent = _project_history(project)
    report = classification.classify_history(history)

    scope_keys = set()
    unscoped = 0
    for finding in report['findings']:
        mid = str(finding.get('model_id') or '').strip()
        cat = str(finding.get('category') or '').strip()
        if not mid or not cat:
            unscoped += 1
            continue
        if model_id and mid != model_id:
            continue
        if category and cat != category:
            continue
        scope_keys.add((mid, cat))

    scopes = []
    for mid, cat in sorted(scope_keys):
        risk = model_router.rework_risk(history, mid, cat, report=report)
        scopes.append(_compact_risk(risk))

    takeover = sorted({finding['family'] for finding in report['findings']
                       if finding.get('reason') == 'main_takeover_final_accept'})
    quality_ids, success_ids = [], []
    for key, family in sorted((report.get('families') or {}).items()):
        members = family.get('members') or []
        if not members:
            continue
        if family.get('outcome') == classification.OUTCOME_QUALITY:
            quality_ids.append(members[0])
        elif family.get('outcome') == classification.OUTCOME_SUCCESS and family.get('verified'):
            success_ids.append(members[0])

    counts = report.get('counts_by_outcome') or {}
    return {
        'schema': 1,
        'project': project,
        'model_id': model_id,
        'category': category,
        'source': '项目内 task.json + review.json（delegation_policy.panel_summary）经 failure_classification 分类',
        'read_only': True,
        'records': report.get('records'),
        'unreadable_records': unreadable,
        'stale_embedded_review_records': stale_embeds,
        'incoherent_accepted_reviews': incoherent,
        'totals_scope': 'project_wide_all_models_and_categories',
        'filter': {'model_id': model_id, 'category': category,
                   'applies_to': 'scopes only; totals above stay project-wide'},
        'families_total': report.get('families_total'),
        'verified_families': report.get('verified_families'),
        'quality_rework_families': report.get('quality_rework_families'),
        'system_excluded_families': report.get('system_excluded_families'),
        'unknown_families': report.get('unknown_families'),
        'unscoped_findings': unscoped,
        'counts_by_outcome': counts,
        'excluded_from_quality_denominator': ['system', 'cancelled', 'unknown'],
        'main_takeover_families': takeover,
        'main_takeover_count': len(takeover),
        'main_takeover_note': '主脑接管后的 final_accepted 已单独归类，不计入执行器成功样本。',
        'scopes': scopes,
        'quality_representative_task_ids': quality_ids[:10],
        'executor_success_representative_task_ids': success_ids[:10],
        'attribution_note': report.get('attribution_note'),
        'notes': [
            '只读汇总：不是评分、节省率、配额统计或训练/基准推断。',
            '质量分母只含独立验收过的家族；网络/资源/预算/取消/未知均不计入。',
            '样本不足 (<5) 时标 insufficient_samples，不据此自动写预设或改路由。',
            'records/families_total 等总计是项目全量；model_id/category 过滤只作用于 scopes。',
            'review.json 缺失/损坏或不完整时保持未知，不用 task.json 内嵌的旧结论代替。',
        ],
    }


# ----------------------------------------------------------------------- render

def _render(value, indent=0):
    lines, pad = [], '  ' * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if item in (None, '', [], {}):
                continue
            if isinstance(item, (dict, list)):
                lines.append('%s%s:' % (pad, key))
                lines.extend(_render(item, indent + 1))
            else:
                lines.append('%s%s: %s' % (pad, key, item))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                lines.extend(_render(item, indent))
            else:
                lines.append('%s- %s' % (pad, item))
    else:
        lines.append('%s%s' % (pad, value))
    return lines


def _emit(value, as_json=False):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        print('\n'.join(_render(value)))


# -------------------------------------------------------------------------- CLI

def _requirements_file(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('需求文件需要是 JSON 对象')
    return data


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        prog='workflow_assist.py',
        description='Delivery packet, acceptance plan, delegation aid, stage context, calibration.')
    sub = parser.add_subparsers(dest='command', required=True)

    packet = sub.add_parser('packet', help='完成任务的紧凑交付包（只读）')
    packet.add_argument('--task', required=True)
    packet.add_argument('--project')
    packet.add_argument('--json', action='store_true')

    plan = sub.add_parser('acceptance-plan', help='确定性选择验收档案（不执行命令、不记验收）')
    plan.add_argument('--task', help='从真实任务记录取文件范围')
    plan.add_argument('--files', nargs='+', default=[])
    plan.add_argument('--keyword', action='append', default=[])
    plan.add_argument('--text', default='')
    plan.add_argument('--project')
    plan.add_argument('--profiles', type=Path)
    plan.add_argument('--json', action='store_true')

    delegate = sub.add_parser('delegate', help='委派门槛规划援助（不派发、不改模型）')
    delegate.add_argument('--requirements-file', type=Path)
    delegate.add_argument('--trivial', action='store_true')
    delegate.add_argument('--scoped', action='store_true')
    delegate.add_argument('--understandable', action='store_true')
    delegate.add_argument('--ambiguity', choices=['low', 'medium', 'high', 'unknown'], default=None)
    delegate.add_argument('--architecture', action='store_true')
    delegate.add_argument('--security', action='store_true')
    delegate.add_argument('--authorization', action='store_true')
    delegate.add_argument('--project-isolation', action='store_true')
    delegate.add_argument('--capability-judged', action='store_true')
    delegate.add_argument('--policy-allowed', action='store_true')
    delegate.add_argument('--expected-benefit', action='store_true')
    delegate.add_argument('--json', action='store_true')

    context = sub.add_parser('stage-context', help='阶段续作简报（只读、拒绝失效交接）')
    context.add_argument('--stage')
    context.add_argument('--task')
    context.add_argument('--project')
    context.add_argument('--json', action='store_true')

    calibration = sub.add_parser('calibration', help='只读返工家族校准汇总（单项目）')
    calibration.add_argument('--project', required=True)
    calibration.add_argument('--model-id')
    calibration.add_argument('--category')
    calibration.add_argument('--json', action='store_true')

    args = parser.parse_args(argv)
    try:
        if args.command == 'packet':
            _emit(delivery_packet(args.task, args.project), args.json)
        elif args.command == 'acceptance-plan':
            _emit(acceptance_plan(files=args.files, keywords=args.keyword, task_id=args.task,
                                  project=args.project, profiles=load_profiles(args.profiles)),
                  args.json)
        elif args.command == 'delegate':
            requirements = _requirements_file(args.requirements_file) if args.requirements_file else {}
            for key in ('trivial', 'scoped', 'understandable', 'capability_judged',
                        'policy_allowed', 'expected_benefit', 'architecture', 'security',
                        'authorization'):
                if getattr(args, key):
                    requirements[key] = True
            if args.project_isolation:
                requirements['project_isolation'] = True
            if args.ambiguity is not None:
                requirements['ambiguity'] = args.ambiguity
            requirements.setdefault('ambiguity', 'unknown')
            _emit(delegation_plan(requirements), args.json)
        elif args.command == 'stage-context':
            _emit(stage_context(stage_id=args.stage, task_id=args.task, project=args.project),
                  args.json)
        elif args.command == 'calibration':
            _emit(calibration_summary(args.project, model_id=args.model_id,
                                      category=args.category), args.json)
        else:  # pragma: no cover - argparse enforces the choices
            parser.error('未知命令')
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
