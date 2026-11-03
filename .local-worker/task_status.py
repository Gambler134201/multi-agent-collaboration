"""Read-only Chinese status summaries. No persistence, inference or scheduling."""

RAW_LABELS = {'queued': '等待执行', 'dispatching': '准备执行', 'running': '正在执行',
              'completed': '执行结束', 'failed': '执行失败', 'cancelled': '已停止',
              'recovery_pending': '等待恢复核对'}
INTEGRATION_LABELS = {'none': '未记录集成', 'not_applicable': '不适用',
    'awaiting_review': '等待集成验收', 'accepted': '集成已验收',
    'conflict': '集成存在冲突', 'check_failed': '集成检查未通过',
    'multiple': '多份集成需核对', 'stale': '集成记录已过期',
    'unavailable': '集成状态待核对'}
INTEGRATION_LABELS.update(queued='等待集成',running='正在集成',blocked='集成准备受阻',
    recovery_required='集成结果待核对',cancelled='集成已取消')


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value, limit=300):
    return value[:limit] if isinstance(value, str) else ''


def _summary(code, label, tone, detail, next_step, raw_status, facts=()):
    return {'schema': 1, 'source': 'controller_projection', 'code': code,
            'label': label, 'tone': tone, 'detail': detail, 'next_step': next_step,
            'raw_status': _text(raw_status, 40), 'facts': list(facts)}


def task_summary(task, result):
    """Use authoritative result fields, never phase/error keyword guessing."""
    task, result = _dict(task), _dict(result)
    raw = _text(task.get('status'), 40)
    review = _dict(result.get('review'))
    rstate = _text(review.get('state'), 40)
    settle = _text(_dict(result.get('controller_finalization')).get('state'), 40)
    integration = _dict(result.get('integration'))
    istate = _text(integration.get('state'), 40) or 'unavailable'
    facts = [{'label': '执行', 'value': RAW_LABELS.get(raw, '执行状态未核实')},
             {'label': '验收', 'value': '独立验收通过' if review.get('accepted') is True else
                 '验收记录待核对' if review.get('unreadable') else
                 {'returned': '已退回整改', 'blocked': '需要处理'}.get(rstate, '尚未独立验收')},
             {'label': '收尾', 'value': {'settled': '已确认', 'pending': '处理中',
                 'attention': '待核对', 'legacy': '历史记录未保存'}.get(settle, '未核实')},
             {'label': '集成', 'value': INTEGRATION_LABELS.get(istate, '集成状态待核对')}]

    def show(code, label, tone, detail, next_step):
        return _summary(code, label, tone, detail, next_step, raw, facts)

    terminal = raw in {'completed', 'failed', 'cancelled', 'recovery_pending'}
    if terminal and review.get('accepted') is True:
        takeover = rstate == 'final_accepted'
        base = '主脑接管后已验收' if takeover else '已独立验收'
        if istate in {'awaiting_review', 'accepted', 'conflict', 'check_failed', 'multiple', 'stale',
                      'queued','running','blocked','recovery_required','cancelled'}:
            return show('integration_' + istate, INTEGRATION_LABELS[istate],
                'success' if istate == 'accepted' else 'warning',
                base + '；' + _text(integration.get('detail')),
                '查看集成记录，由主脑核对下一步；不会自动覆盖原项目。')
        return show('accepted_takeover' if takeover else 'accepted', base, 'success',
            '原执行记录保留；验收结论来自主脑独立证据。' if takeover else '执行成果已通过主脑独立检查。',
            '结果可供后续使用；需要合并时另行安排受控集成。')

    native = _dict(task.get(task.get('engine'))) if isinstance(task.get('engine'), str) else {}
    issues = result.get('issues') if isinstance(result.get('issues'), list) else []
    unknown = (native.get('outcome_unknown') is True or
               any(_dict(row).get('kind') == 'outcome_unknown' or
                   _dict(row).get('code') == 'OUTCOME_UNKNOWN' for row in issues))
    if unknown:
        return show('outcome_unknown', '结果未知，等待核对', 'warning',
            '现有记录不能确认原生执行是否结束或产生了哪些副作用。',
            '核对原任务与会话；不要自动重派或强行释放占用。')
    if terminal and settle == 'attention':
        return show('cleanup_attention', '收尾待确认', 'warning',
            '执行结果与会话/成果/资源收尾需要分别确认。',
            '由原责任会话核对收尾，不重新执行任务。')
    if terminal and settle == 'pending':
        return show('finalizing', '正在收尾', 'accent',
            '执行器已结束，控制器正在核对成果并释放资源。',
            '等待收尾完成后再验收；无需重复派发。')
    if raw == 'recovery_pending':
        return show('recovery_pending', '等待恢复核对', 'warning',
            '恢复条件尚未确认，原任务记录和已有成果保留。',
            '先核对原任务，不自动重派。')
    if raw == 'running':
        return show('running', '正在执行', 'accent', '执行端正在处理任务。', '等待执行结果。')
    if raw == 'dispatching':
        return show('dispatching', '正在准备执行', 'accent', '控制器正在准备任务副本与会话。', '等待准备完成。')
    if raw == 'queued':
        wait = _dict(task.get('lease_wait'))
        if isinstance(wait.get('holder'), str) and wait['holder'] and type(wait.get('count')) is int and wait['count'] > 0:
            return show('waiting_resource', '等待执行资源', 'neutral',
                '已记录共享模型或后端资源占用，任务尚未开始。', '等待原占用释放，无需重复提交。')
        return show('queued', '等待执行', 'neutral', '任务已排队，具体等待原因未记录。', '等待调度，不重复提交。')
    if review.get('unreadable') or rstate in {'accepted', 'final_accepted'}:
        return show('review_unknown', '验收状态待核对', 'warning',
            '验收记录无法读取或与当前成果不匹配，不能当作已通过。', '先核对真实验收记录与成果。')
    if rstate == 'returned':
        return show('returned', '需要整改', 'warning', _text(review.get('reason')) or '主脑已退回本次交付。', '按已记录的问题和范围安排整改。')
    if rstate == 'blocked':
        return show('blocked', '需要处理', 'warning', _text(review.get('reason')) or '主脑已记录受阻，尚未接受交付。', '查看阻塞原因，等待责任人决定。')
    if raw == 'failed':
        return show('failed', '执行未成功', 'danger', '原始执行失败记录保留；已有文件不等于任务已通过。', '查看具体失败阶段，再决定处理方式。')
    if raw == 'cancelled':
        return show('cancelled', '已停止', 'neutral', '任务已取消，不会自动恢复或重派。', '保留已有结果，需要继续时另行决定。')
    if raw == 'completed':
        if (result.get('legacy') is True and not task.get('delegation')
                and 'controller_finalization' not in task and not rstate):
            return show('review_unrecorded', '执行结束，验收未记录', 'neutral',
                '现有记录只证明执行结束，不能追认为已通过，也不自动加入新任务待办。',
                '需要使用该成果时先核对记录。')
        return show('awaiting_review', '等待独立验收', 'warning',
            '执行器已结束，但主脑尚未确认成果符合要求。', '主脑检查实际文件和测试后记录验收。')
    return show('unknown', '状态待核对', 'neutral', '缺少足够的结构化状态证据。', '检查原任务记录，不根据文字猜测成功或失败。')


def stage_summary(stage, readiness, tasks):
    stage, readiness = _dict(stage), _dict(readiness)
    status = _text(stage.get('status'), 40)
    if status == 'cancelled':
        return _summary('cancelled', '已停止', 'neutral', '阶段已取消。', '不会自动恢复。', status)
    if status in {'accepted', 'final_accepted'}:
        return _summary('accepted', '阶段已验收', 'success', '前置验收事实保留。', '后续阶段仍需符合依赖和派发授权。', status)
    for task in tasks if isinstance(tasks, list) else []:
        task = _dict(task)
        if task.get('task_id') == stage.get('active_task_id'):
            summary = _dict(_dict(task.get('execution_result')).get('summary'))
            if summary:
                return dict(summary)
    blocked = readiness.get('blocked_by')
    if isinstance(blocked, list) and blocked:
        states = _dict(readiness.get('dependency_states'))
        bad = any(_text(states.get(dep), 40) in {'failed', 'blocked', 'cancelled', 'recovery_pending'}
                  for dep in blocked if isinstance(dep, str))
        reasons = readiness.get('reasons')
        detail = '；'.join(_text(x, 120) for x in reasons[:3]) if isinstance(reasons, list) else ''
        return _summary('waiting_dependency', '等待前置处理' if bad else '等待前置验收',
            'warning' if bad else 'neutral', detail or '尚有前置阶段未独立验收。',
            '先处理前置阶段；不越过依赖直接派发。', status)
    if status in {'executor_completed', 'returned', 'blocked', 'failed', 'recovery_pending'}:
        labels = {'executor_completed': '等待独立验收', 'returned': '需要整改', 'blocked': '需要处理',
                  'failed': '执行未成功', 'recovery_pending': '等待恢复核对'}
        return _summary(status, labels[status], 'warning', '查看本阶段任务与验收记录。', '不自动重派。', status)
    if readiness.get('ready') is True:
        return _summary('ready', '可以安排执行', 'neutral', '已记录的前置依赖已满足。', '就绪不等于已经派发或获得额外权限。', status)
    return _summary('stage_pending', '等待安排或核对', 'neutral', '阶段尚未进入可执行状态。', '查看依赖及目标状态。', status)
