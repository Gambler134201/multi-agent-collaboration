"""Read-only failure classification with explicit source, evidence and attribution.

Public API (schema 1, standard library only):

    classify(record)            -> one finding dict for a task record
    classify_history(history)   -> findings, rework families and counts
    family_index(history)       -> task id -> rework-family root id
    family_outcome(findings)    -> one outcome for a whole rework family
    is_model_quality(finding)   -> bool
    is_system_excluded(finding) -> bool
    SYSTEM_REASONS / QUALITY_REASONS / REASON_LABELS

Design rules (mirrored by the negative tests):

* Every finding names its ``source`` and the concrete ``evidence`` excerpt, so a
  statistic can be explained after the fact.
* ``counts_as_model_quality`` is True only for a *verified* quality gap: an
  independent review returned an artifact that the executor had actually
  delivered, for a concrete failed condition.  ``executor completed`` alone is
  never a quality success and never a quality failure.
* Network/429/server faults, permission or Windows-record save failures,
  cancellation, user money/call limits, wrong or conservative token estimates,
  output truncation and server-side window overflow, and local
  resource/executor faults are classified as system causes and are excluded
  from model-quality attribution.
* Unknown stays unknown: a bare ``status: failed`` without evidence is never
  assumed to be a model-quality failure.
* Reading history is pure.  Nothing here writes a task record, review record or
  ledger; every result carries ``read_only: True``.
"""
import re

SCHEMA = 1

OUTCOME_SUCCESS = 'success'
OUTCOME_QUALITY = 'quality'
OUTCOME_SYSTEM = 'system'
OUTCOME_CANCELLED = 'cancelled'
OUTCOME_UNKNOWN = 'unknown'
OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_QUALITY, OUTCOME_SYSTEM, OUTCOME_CANCELLED, OUTCOME_UNKNOWN)

EXCLUDED_OUTCOMES = (OUTCOME_SYSTEM, OUTCOME_CANCELLED)
ACCEPTED_REVIEW_STATES = ('accepted', 'final_accepted')
# Main-mind final acceptance is a real, independent decision *about the
# executor's failure*: the work only passed after GPT took over.  It must never
# be counted as an executor quality success, so it is a separate bucket.
FINAL_ACCEPTED_REVIEW_STATES = ('final_accepted',)
RETURNED_REVIEW_STATES = ('returned',)
PENDING_REVIEW_STATES = ('dispatched', 'executor_completed', 'executor_failed', 'blocked')

MAX_EXCERPT = 200

REASON_LABELS = {
    # verified quality
    'verified_quality_gap': '独立验收退回：已交付成果存在质量缺口',
    # system / capacity
    'cancelled': '任务被取消，不计模型质量',
    'network_error': '网络/连接故障，不计模型质量',
    'server_rate_limited': '服务端限流（429/并发），不计模型质量',
    'server_error': '服务端错误，不计模型质量',
    'server_context_overflow': '服务端上下文窗口超限，按容量故障处理',
    'output_truncated': '输出被截断（单次请求输出额度不足），按资源/容量故障处理',
    'permission_denied': '权限/沙箱拒绝，不计模型质量',
    'windows_task_save': 'Windows 任务或记录保存失败，不计模型质量',
    'record_write_error': '控制器记录写入失败，不计模型质量',
    'session_close_error': '任务会话关闭未确认，不计模型质量',
    'user_budget_limit': '用户金额上限，不计模型质量',
    'user_call_limit': '用户请求次数上限，不计模型质量',
    'token_estimate_gate': '本地按保守/近似 token 估计提前拦截，不计模型质量',
    'local_resource': '本地资源/执行器容量故障，不计模型质量',
    'explicit_system': '记录显式标记为系统/资源故障',
    # unknown
    'executor_reported_quality_unverified': '执行器自报质量问题，未经独立验收，暂不计模型质量',
    'returned_without_delivery': '退回时没有可验收的交付物，无法归因模型质量',
    'review_pending': '独立验收尚未给出结论',
    'blocked_needs_decision': '受阻待主脑判断，未归因模型质量',
    'executor_failed_unclassified': '执行器未完成且无系统证据，保持未知',
    'completed_unverified': '执行器完成但尚无独立验收结论',
    'main_takeover_final_accept': '主脑接管后的最终验收：执行器未独立通过，不计执行器质量成功',
    'review_unreadable': '权威验收记录缺失、损坏或不完整，保持未知，不用任务内嵌的旧结论代替',
    'no_evidence': '没有可用证据，保持未知',
}

# ---- ordered system rules -------------------------------------------------
# Order matters: a conservative-estimate gate must win over the plain
# "context window" wording it contains, and truncation must be recognised as a
# resource fault rather than a model-quality fault.
_SYSTEM_RULES = (
    ('cancelled', (
        r'cancel', r'已取消', r'取消', r'已终止', r'aborted', r'interrupted', r'用户中断', r'中断',
    )),
    ('user_call_limit', (
        r'请求次数上限', r'调用次数上限', r'次数上限', r'call limit', r'request[-_ ]?count limit',
        r'max[_ ]?model[_ ]?calls', r'maximum number of (?:model )?calls',
    )),
    ('user_budget_limit', (
        r'保守费用会超出', r'费用会超出', r'预算不足', r'超出.{0,12}预算', r'预算.{0,10}(?:已到|耗尽|超限)',
        r'budget (?:exceeded|limit|exhausted)', r'insufficient_quota', r'quota exceeded',
        r'余额不足', r'额度不足', r'金额上限', r'超支',
    )),
    ('token_estimate_gate', (
        r'保守估计超过上下文窗口', r'保守估计', r'预计数', r'token estimate', r'estimated .{0,30}exceed',
        r'估算.{0,10}超过', r'近似.{0,10}超过',
    )),
    ('server_rate_limited', (
        r'\b429\b', r'too many requests', r'rate ?limit', r'限流', r'请求过于频繁', r'overloaded',
        r'concurrency limit', r'并发上限',
    )),
    ('output_truncated', (
        r'max_tokens', r'max tokens', r'max[_ ]output[_ ]tokens', r'输出截断', r'截断', r'truncat',
        r'finish_reason.{0,4}length', r'length limit', r'提前结束', r'输出额度不足',
    )),
    ('server_context_overflow', (
        r'context[_ ]length[_ ]exceeded', r'maximum context length', r'context window', r'超出上下文',
        r'上下文窗口', r'窗口超限', r'prompt is too long', r'输入过长', r'too many tokens',
    )),
    ('network_error', (
        r'connection error', r'connection reset', r'econnreset', r'connection refused', r'connection aborted',
        r'timed? ?out', r'timeout', r'网络', r'连接失败', r'连接错误', r'socket', r'\bdns\b', r'\btls\b',
        r'\bssl\b', r'remote end closed', r'502 bad gateway', r'503 service unavailable',
    )),
    ('server_error', (
        r'internal error', r'server error', r'服务端错误', r'服务不可用', r'bad gateway', r'gateway timeout',
        r'http[^\n]{0,12}\b5\d\d\b', r'status[^\n]{0,10}\b5\d\d\b', r'upstream error',
    )),
    ('permission_denied', (
        r'permission denied', r'access is denied', r'\beperm\b', r'\beacces\b', r'拒绝访问', r'权限不足',
        r'sandbox.{0,24}(?:deny|denied|block)', r'沙箱.{0,10}(?:拒绝|禁止)',
    )),
    ('windows_task_save', (
        r'文件被占用', r'sharing violation', r'winerror', r'无法写入', r'保存失败', r'任务保存', r'写入失败',
        r'no such file', r'被占用',
    )),
    ('local_resource', (
        r'dsh 提前结束', r'dsh 未公布所选模型', r'dsh 未返回正文', r'执行器', r'executor', r'本地资源',
        r'内存不足', r'out of memory', r'\boom\b', r'磁盘', r'\bdisk\b', r'no space', r'资源不足',
        r'容量', r'capacity', r'进程退出',
    )),
)

SYSTEM_REASONS = frozenset(reason for reason, _ in _SYSTEM_RULES) | {'explicit_system', 'record_write_error', 'session_close_error'}
QUALITY_REASONS = frozenset({'verified_quality_gap'})

_CANCELLED_REASONS = frozenset({'cancelled'})


_COMPILED = {reason: tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)
             for reason, patterns in _SYSTEM_RULES}


def _text(value):
    return str(value if value is not None else '')


def _excerpt(text, limit=MAX_EXCERPT):
    return ' '.join(_text(text).split())[:limit]


def _match_system(text):
    """Return (reason, matched text) for the first matching system rule."""
    if not text:
        return None, ''
    for reason, patterns in _SYSTEM_RULES:
        for pattern in _COMPILED[reason]:
            found = pattern.search(text)
            if found:
                return reason, found.group(0)
    return None, ''


def _dict(value):
    return value if isinstance(value, dict) else {}


def _id(record):
    return _text(record.get('id') or record.get('task_id') or _dict(record.get('review')).get('task_id')).strip()


def _parent(record):
    delegation = _dict(record.get('delegation'))
    review = _dict(record.get('review'))
    for value in (record.get('parent_task_id'), delegation.get('parent_task_id'),
                  review.get('parent_task_id'), record.get('rework_of')):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def _evidence(source, field, value):
    return {'source': source, 'field': field, 'excerpt': _excerpt(value)}


def _reason_label(reason):
    return REASON_LABELS.get(reason, reason)


def _category(record):
    requirements = _dict(_dict(record.get('route')).get('requirements'))
    return _text(requirements.get('category')).strip()


def _finding(record, task_id, outcome, reason, source, confidence, evidence, verified, note=''):
    return {'schema': SCHEMA,
            'task_id': task_id,
            'family': task_id,
            'title': _excerpt(record.get('title'), 120),
            'model_id': _text(record.get('model_id')) or _text(record.get('model')) or '',
            'category': _category(record),
            'outcome': outcome,
            'reason': reason,
            'label': _reason_label(reason),
            'counts_as_model_quality': outcome == OUTCOME_QUALITY,
            'system_excluded': outcome in EXCLUDED_OUTCOMES,
            'verified': bool(verified),
            'confidence': confidence,
            'source': source,
            'evidence': list(evidence),
            'note': note,
            'read_only': True}


def _explicit_class(record):
    raw = record.get('failure_class')
    if raw is None:
        raw = _dict(record.get('failure')).get('kind')
    if raw is None:
        return None
    value = _text(raw).strip().lower().replace('-', '_')
    if value in {'system', 'system_failure', 'resource', 'capacity', 'infra', 'infrastructure'}:
        return OUTCOME_SYSTEM, 'explicit_system'
    if value in {'cancelled', 'canceled', 'cancel'}:
        return OUTCOME_CANCELLED, 'cancelled'
    if value in {'quality', 'quality_failure', 'model'}:
        # An executor self-report is not independent evidence: stay unknown.
        return OUTCOME_UNKNOWN, 'executor_reported_quality_unverified'
    return None


def classify(record):
    """Classify one task history record without changing it.

    The record may be a raw ``task.json`` dict or a ``public_tasks()`` row; the
    only fields used are ``status``, ``error``/``budget_error``, ``review``
    (``delegation_policy.panel_summary``), ``executor``, ``delegation``,
    ``changed_files``, ``test_result``, ``route`` and ``summary``.
    """
    record = _dict(record)
    review = _dict(record.get('review'))
    executor = _dict(record.get('executor'))
    delegation = _dict(record.get('delegation'))
    status = _text(record.get('status')).strip().lower()
    review_state = _text(review.get('state') or record.get('review_state')).strip().lower()
    executor_status = _text(review.get('executor_status') or executor.get('status')
                            or delegation.get('executor_status')).strip().lower()
    task_id = _id(record)
    error_text = '；'.join(part for part in (
        _text(record.get('error')), _text(record.get('budget_error')), _text(record.get('failure_message')))
        if part and part.strip() and part.strip().lower() != 'none')

    # 1) explicit structured classification on the record
    explicit = _explicit_class(record)
    if explicit is not None:
        outcome, reason = explicit
        return _finding(record, task_id, outcome, reason, 'executor_field', 'medium',
                        [_evidence('executor_field', 'failure_class', record.get('failure_class') or
                                   _dict(record.get('failure')).get('kind'))],
                        verified=outcome != OUTCOME_UNKNOWN, note='记录自带的显式分类')

    # 2) recorded cancellation
    if status in ('cancelled', 'canceled', 'cancelling') or review_state in ('cancelled', 'canceled'):
        return _finding(record, task_id, OUTCOME_CANCELLED, 'cancelled', 'task_status', 'high',
                        [_evidence('task_status', 'status', record.get('status'))], verified=True)

    # 3) the task's own structured error / termination text (never reviewer prose)
    import execution_result
    result_view = execution_result.project(record)
    structured = not result_view['legacy']
    stage_failure = result_view['failure'] or {}
    stable_rules = {
        ('TOOL_PERMISSION_DENIED', 'adapter_tool'): 'permission_denied',
        ('RECORD_WRITE_FAILED', 'persistence'): 'record_write_error',
        ('REVIEW_RECORD_FAILED', 'persistence'): 'record_write_error',
        ('SESSION_CLOSE_UNCONFIRMED', 'adapter'): 'session_close_error',
        ('SESSION_CLOSE_UNCONFIRMED', 'child_process'): 'session_close_error',
    }
    stable_reason = stable_rules.get((stage_failure.get('code'), stage_failure.get('source')))
    reason, hit = (None, None) if structured else _match_system(error_text)
    completed_review = (executor_status == 'completed'
                        and review_state in ('accepted', 'returned')
                        and not record.get('review_unreadable'))
    if stable_reason and not completed_review:
        return _finding(record, task_id, OUTCOME_SYSTEM, stable_reason, 'controller_stage', 'high',
                        [_evidence('controller_stage', 'execution_evidence.issues', stage_failure.get('code'))],
                        verified=True, note='控制器/原生工具的固定规则编号；不是错误文案推断')
    if reason and not completed_review:
        outcome = OUTCOME_CANCELLED if reason in _CANCELLED_REASONS else OUTCOME_SYSTEM
        return _finding(record, task_id, outcome, reason, 'executor_field', 'medium',
                        [_evidence('executor_field', 'error', hit or error_text)], verified=False,
                        note='历史错误文本规则推断；不是结构化故障证据')

    # 3b) the authoritative review record could not be trusted (corrupt, or
    #     missing while the task carried a stale embedded verdict).  The caller
    #     sets ``review_unreadable`` after removing any embedded snapshot, so an
    #     old embedded ``accepted`` can never become a verified success here.
    unreadable_review = _text(record.get('review_unreadable')).strip()
    if review_state in ACCEPTED_REVIEW_STATES and review.get('acceptance_valid') is False:
        unreadable_review = unreadable_review or '验收记录证据不完整'
    if unreadable_review:
        return _finding(record, task_id, OUTCOME_UNKNOWN, 'review_unreadable',
                        'independent_review', 'high',
                        [_evidence('independent_review', 'review_unreadable', unreadable_review)],
                        verified=False,
                        note='权威 review.json 缺失/损坏/不完整，保持未知')

    # 4) independent review verdict
    if review_state in FINAL_ACCEPTED_REVIEW_STATES:
        # Main-mind final acceptance is independent verification *that the
        # executor did not deliver*, so it stays out of the executor-success
        # denominator instead of being counted as a verified success.
        return _finding(record, task_id, OUTCOME_UNKNOWN, 'main_takeover_final_accept',
                        'independent_review', 'high',
                        [_evidence('independent_review', 'review.state', review_state),
                         _evidence('independent_review', 'executor_status', executor_status)],
                        verified=False,
                        note='主脑接管后的最终验收保留执行器失败，不计执行器质量成功')
    if review_state in ACCEPTED_REVIEW_STATES:
        return _finding(record, task_id, OUTCOME_SUCCESS, 'completed_unverified', 'independent_review', 'high',
                        [_evidence('independent_review', 'review.state', review_state)], verified=True,
                        note='独立验收通过')
    if review_state in RETURNED_REVIEW_STATES:
        delivered = executor_status == 'completed' or bool(record.get('changed_files')) or bool(record.get('test_result'))
        evidence = [_evidence('independent_review', 'review.state', review_state),
                    _evidence('independent_review', 'executor_status', executor_status)]
        criteria = review.get('returned_criteria')
        if isinstance(criteria, (list, tuple)) and criteria:
            evidence.append(_evidence('independent_review', 'returned_criteria', '；'.join(_text(c) for c in criteria)))
        if delivered and executor_status == 'completed':
            return _finding(record, task_id, OUTCOME_QUALITY, 'verified_quality_gap', 'independent_review', 'high',
                            evidence, verified=True,
                            note='独立验收退回已交付成果的质量缺口')
        return _finding(record, task_id, OUTCOME_UNKNOWN, 'returned_without_delivery', 'independent_review', 'medium',
                        evidence, verified=False,
                        note='退回但没有可验收的已完成交付物，未知不假定质量失败')
    if review_state in PENDING_REVIEW_STATES:
        reason_pending = 'blocked_needs_decision' if review_state == 'blocked' else 'review_pending'
        return _finding(record, task_id, OUTCOME_UNKNOWN, reason_pending, 'independent_review', 'low',
                        [_evidence('independent_review', 'review.state', review_state)], verified=False)

    # 5) task / executor status without an independent verdict
    summary = _text(record.get('summary') or record.get('log_summary') or record.get('log_tail'))
    if not structured and status in ('failed', 'error') and not error_text and summary:
        reason, hit = _match_system(summary)
        if reason:
            outcome = OUTCOME_CANCELLED if reason in _CANCELLED_REASONS else OUTCOME_SYSTEM
            return _finding(record, task_id, outcome, reason, 'summary_text', 'low',
                            [_evidence('summary_text', 'summary', hit or summary)], verified=False)
    if status in ('failed', 'error', 'failure'):
        if executor_status == 'failed':
            return _finding(record, task_id, OUTCOME_UNKNOWN, 'executor_failed_unclassified', 'task_status', 'low',
                            [_evidence('task_status', 'status', record.get('status'))], verified=False)
        return _finding(record, task_id, OUTCOME_UNKNOWN, 'no_evidence', 'task_status', 'low',
                        [_evidence('task_status', 'status', record.get('status'))], verified=False)
    if status in ('completed', 'success', 'succeeded', 'done'):
        return _finding(record, task_id, OUTCOME_SUCCESS, 'completed_unverified', 'task_status', 'low',
                        [_evidence('task_status', 'status', record.get('status'))], verified=False,
                        note='执行器完成不等于质量通过')

    # 6) free text only as a last resort, and only for a finished failure
    summary = _text(record.get('summary') or record.get('log_summary') or record.get('log_tail'))
    if not structured and status in ('failed', 'error') and summary:
        reason, hit = _match_system(summary)
        if reason:
            outcome = OUTCOME_CANCELLED if reason in _CANCELLED_REASONS else OUTCOME_SYSTEM
            return _finding(record, task_id, outcome, reason, 'summary_text', 'low',
                            [_evidence('summary_text', 'summary', hit or summary)], verified=False)
    return _finding(record, task_id, OUTCOME_UNKNOWN, 'no_evidence', 'none', 'none', [], verified=False)


def is_model_quality(finding):
    return bool(_dict(finding).get('counts_as_model_quality'))


def is_system_excluded(finding):
    return bool(_dict(finding).get('system_excluded'))


def family_index(history):
    """Map every task id to its rework-family root (parent chain resolved)."""
    records = [r for r in (history or []) if isinstance(r, dict)]
    parents = {}
    for record in records:
        task_id = _id(record)
        parent = _parent(record)
        if task_id and parent and parent != task_id:
            parents[task_id] = parent

    def root(task_id):
        seen = set()
        current = task_id
        while current in parents and current not in seen:
            seen.add(current)
            current = parents[current]
        return current

    return {_id(record): root(_id(record)) for record in records if _id(record)}


def family_outcome(findings):
    """One outcome for a rework family (deduplicated quality attribution)."""
    findings = [f for f in (findings or []) if isinstance(f, dict)]
    if not findings:
        return OUTCOME_UNKNOWN
    if any(f.get('outcome') == OUTCOME_QUALITY and f.get('verified') for f in findings):
        return OUTCOME_QUALITY
    outcomes = {f.get('outcome') for f in findings}
    if outcomes and outcomes <= set(EXCLUDED_OUTCOMES):
        return OUTCOME_SYSTEM
    if OUTCOME_SUCCESS in outcomes:
        return OUTCOME_SUCCESS
    return OUTCOME_UNKNOWN


def _family_verified(findings):
    return any(f.get('source') == 'independent_review' and f.get('outcome') in (OUTCOME_QUALITY, OUTCOME_SUCCESS)
               for f in findings)


def classify_history(history):
    """Classify a whole history and group it into rework families.

    Pure read: the returned structure contains new dicts only, and the input
    records are never modified.
    """
    records = [r for r in (history or []) if isinstance(r, dict)]
    index = family_index(records)
    findings = []
    for record in records:
        finding = classify(record)
        finding['family'] = index.get(finding['task_id']) or finding['task_id']
        findings.append(finding)

    grouped = {}
    for finding in findings:
        grouped.setdefault(finding['family'], []).append(finding)

    families = {}
    for key, members in grouped.items():
        outcome = family_outcome(members)
        families[key] = {'family': key,
                         'outcome': outcome,
                         'verified': _family_verified(members),
                         'members': [m['task_id'] for m in members],
                         'reasons': sorted({m['reason'] for m in members}),
                         'counts_as_model_quality': outcome == OUTCOME_QUALITY}
    counts = {outcome: 0 for outcome in OUTCOMES}
    for finding in findings:
        counts[finding['outcome']] = counts.get(finding['outcome'], 0) + 1
    return {'schema': SCHEMA,
            'findings': findings,
            'families': families,
            'counts_by_outcome': counts,
            'records': len(findings),
            'families_total': len(families),
            'verified_families': sum(1 for f in families.values() if f['verified']),
            'quality_rework_families': sum(1 for f in families.values() if f['outcome'] == OUTCOME_QUALITY),
            'system_excluded_families': sum(1 for f in families.values() if f['outcome'] in EXCLUDED_OUTCOMES),
            'unknown_families': sum(1 for f in families.values() if f['outcome'] == OUTCOME_UNKNOWN),
            'quality_records': counts.get(OUTCOME_QUALITY, 0),
            'system_excluded_records': counts.get(OUTCOME_SYSTEM, 0) + counts.get(OUTCOME_CANCELLED, 0),
            'attribution_note': '仅独立验收退回的已交付质量缺口计入模型质量；系统/取消/未知均不计入',
            'read_only': True}
