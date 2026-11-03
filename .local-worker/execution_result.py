"""Versioned, read-only result projection shared by every brain and executor.

Stage evidence is written by controllers, never inferred from answer/error prose.
This module does no IO, calls no tools and cannot accept, resume or retry work.
Artifacts describe the recorded declared-file scan, not the current filesystem.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re

SCHEMA = 1
STAGES = {'prepare': '准备', 'execute': '执行', 'verify': '验证与自测',
          'temporary_cleanup': '临时清理', 'session_close': '会话关闭',
          'artifacts': '成果核对', 'record': '结果记录'}
STATES = {'unknown', 'running', 'completed', 'failed', 'cancelled',
          'not_run', 'not_requested', 'not_applicable'}
SOURCES = {'controller', 'controller_test', 'adapter', 'adapter_tool',
           'protection_verification', 'persistence', 'child_process'}

# Additive schema-1 display semantics. These are evidence labels, never retry
# permissions. Only fixed controller codes at their real boundaries qualify;
# arbitrary category/message/kind fields cannot declare a recoverable outcome.
PROBLEM_LABELS = {
    'permission_denied': '权限或保护规则拒绝',
    'outcome_unknown': '原生操作结果未知',
    'verification_failed': '指定自测未通过',
    'verification_unconfirmed': '验证证据不足',
    'cleanup_unconfirmed': '会话收尾未确认',
    'preparation_error': '执行前准备失败',
    'execution_error': '原生执行未完成',
    'record_error': '结果记录异常',
    'artifact_unconfirmed': '成果核对未确认',
    'unknown': '原因尚未分类',
}


def guard_problem(proof):
    """Only a bound adapter proof may assert a real guard refusal; no prose."""
    if not isinstance(proof,dict):
        raise ValueError('执行端保护证据格式无效')
    if 'guard_denial' not in proof:
        return None
    item=proof['guard_denial']
    if (not isinstance(item,dict) or set(item)!={'schema','code','tool','reason_code','rule_id'} or
            type(item.get('schema')) is not int or item['schema']!=1 or item.get('code')!='TOOL_PERMISSION_DENIED' or
            not isinstance(item.get('reason_code'),str) or item['reason_code'] not in {'protected_data','outside_project','undeclared_write','readonly_file',
                                         'prior_denial','tool_not_allowed','guard_denied'} or
            not isinstance(item.get('rule_id'),str) or not re.fullmatch('[a-z_]{1,60}',item['rule_id']) or
            not isinstance(item.get('tool'),str) or item['tool'] not in {'unknown','read','Read','grep','Grep','find','Glob','ls','NotebookRead',
                'write','Write','edit','Edit','MultiEdit','NotebookEdit','str_replace_editor',
                'bash','Bash','powershell','PowerShell','pwsh'}):
        raise ValueError('执行端拒绝证据格式无效，不能视为完成')
    return dict(item)


def native_selftest(task, events, source, *, native=True):
    """Match only completed terminal evidence; do not infer from answer text.

    An exact miss is unconfirmed, not a proven execution failure. schema=2
    separates that gap from the executor outcome; older snapshots stay strict.
    A real nonzero final test result still fails verification on every version.
    """
    command=task.get('test_command')
    if not isinstance(command,str) or not command:
        raise ValueError('缺少指定测试命令')
    expected=hashlib.sha256(command.encode('utf-8')).hexdigest()
    rows=[row for row in events if isinstance(row,dict) and row.get('command_hash')==expected
          and (not native or row.get('tool') in {'Bash','PowerShell','bash','pwsh'})
          and (not native or row.get('event') in {'PostToolUse','PostToolUseFailure'})]
    last=rows[-1] if rows else {}
    code=last.get('exit_code') if type(last.get('exit_code')) is int else None
    result={'source':source,'command_sha256':expected,'exit_code':code,
            'verified':last.get('ok') is True and last.get('allowed') is not False and code==0
                       and (not native or last.get('event')=='PostToolUse'),
            'call_id':last.get('call_id')}
    import endpoint_access
    contract=endpoint_access.task_policy(task)
    blocking=not result['verified'] and (code is not None and code!=0 or not native or not contract or contract['schema']<2)
    return result,bool(blocking)


def _problem_kind(stage, code, source):
    if code == 'TOOL_PERMISSION_DENIED' and source in {'adapter', 'adapter_tool'} and stage in {'execute', 'verify', 'temporary_cleanup'}:
        return 'permission_denied'
    if code == 'OUTCOME_UNKNOWN' and source == 'adapter' and stage in {'prepare', 'execute'}:
        return 'outcome_unknown'
    if stage == 'verify':
        if code == 'TEST_FAILED' and source in {'controller_test', 'adapter_tool'}:
            return 'verification_failed'
        if ((code == 'TEST_UNCONFIRMED' and source == 'adapter_tool') or
                (code == 'PROTECTION_EVIDENCE_UNCONFIRMED' and source == 'protection_verification') or
                (code == 'VERIFICATION_ERROR' and source in {'adapter', 'protection_verification'}) or
                (code == 'TEST_EXECUTION_ERROR' and source == 'controller_test')):
            return 'verification_unconfirmed'
    if stage == 'session_close' and code == 'SESSION_CLOSE_UNCONFIRMED' and source in {'adapter', 'child_process'}:
        return 'cleanup_unconfirmed'
    if stage == 'record' and code in {'RECORD_WRITE_FAILED', 'REVIEW_RECORD_FAILED'} and source == 'persistence':
        return 'record_error'
    if stage == 'artifacts' and ((code == 'ARTIFACT_SCAN_FAILED' and source == 'controller') or
                                 (code == 'ARTIFACT_CHECK_FAILED' and source == 'adapter')):
        return 'artifact_unconfirmed'
    if stage == 'prepare' and code == 'PREPARATION_FAILED' and source in {'controller', 'adapter'}:
        return 'preparation_error'
    if stage == 'execute' and code in {'NATIVE_EXECUTION_FAILED', 'EXECUTOR_ERROR', 'EMPTY_RESPONSE'} and source in {'adapter', 'controller'}:
        return 'execution_error'
    return 'unknown'


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value, limit=800):
    return value[:limit] if isinstance(value, str) else ''


def _issue(value):
    value = _dict(value)
    stage, source = value.get('stage'), value.get('source')
    if not isinstance(stage, str) or stage not in STAGES or not isinstance(source, str) or source not in SOURCES:
        return None
    code = _text(value.get('code'), 100) or 'UNKNOWN'
    kind = _problem_kind(stage, code, source)
    return {'stage': stage, 'code': code,
            'message': _text(value.get('message')), 'source': source,
            'kind': kind, 'kind_label': PROBLEM_LABELS[kind],
            'category': _text(value.get('category'), 80) or 'unknown',
            'rule_id': _text(value.get('rule_id'), 120),
            'at': _text(value.get('at'), 60)}


class Tracker:
    """In-memory controller evidence. Callers persist snapshots with task state."""
    def __init__(self, engine):
        self.engine = engine
        self.stages = {name: {'state': 'not_run', 'source': 'controller'} for name in STAGES}
        self.stages['temporary_cleanup'] = {'state': 'unknown', 'source': 'controller'}
        self.issues = []
        self.current = 'prepare'

    def mark(self, stage, state, detail='', source='controller'):
        if stage not in STAGES or state not in STATES or source not in SOURCES:
            raise ValueError('Invalid execution stage evidence')
        self.current = stage
        self.stages[stage] = {'state': state, 'source': source, 'detail': _text(detail),
                              'at': datetime.now(timezone.utc).isoformat()}

    def fail(self, stage, code, message, *, source='controller', category='unknown', rule_id=''):
        self.mark(stage, 'failed', message, source)
        item = _issue({'stage': stage, 'code': code, 'message': message, 'source': source,
                       'category': category, 'rule_id': rule_id, 'at': self.stages[stage]['at']})
        self.issues.append(item)

    def snapshot(self):
        return deepcopy({'schema_version': SCHEMA, 'engine': self.engine,
                         'stages': self.stages, 'issues': self.issues[-20:]})


def _files(value):
    if not isinstance(value, list):
        return []
    # Do not publish new absolute/private paths from corrupt task metadata.
    result = []
    for name in value[:100]:
        if not isinstance(name, str) or len(name) > 600:
            continue
        normalized = name.replace('\\', '/')
        if (normalized.startswith('/') or re.match(r'^[A-Za-z]:', normalized)
                or any(p in ('..', '.model-desk') for p in normalized.split('/'))):
            continue
        if name not in result:
            result.append(name)
    return result


def project(task, review=None, *, review_error='', integration=None):
    """Project task evidence; ``review`` must be caller-loaded authoritative data.

    Deliberately ignores task.review/review_state and all free-text phase/error
    keywords. Missing/unsupported evidence remains legacy/unknown. Never writes.
    """
    task = _dict(task)
    evidence = _dict(task.get('execution_evidence'))
    valid = (type(evidence.get('schema_version')) is int
             and evidence.get('schema_version') == SCHEMA
             and isinstance(evidence.get('engine'), str)
             and evidence.get('engine') == task.get('engine'))
    raw_stages = _dict(evidence.get('stages')) if valid else {}
    stages = []
    for name, label in STAGES.items():
        raw = _dict(raw_stages.get(name))
        supported = (isinstance(raw.get('state'), str) and raw['state'] in STATES
                     and isinstance(raw.get('source'), str) and raw['source'] in SOURCES)
        stages.append({'id': name, 'label': label,
                       'state': raw.get('state') if supported else 'unknown',
                       'source': raw.get('source') if supported else 'legacy',
                       'detail': _text(raw.get('detail')) if supported else ''})
        if task.get('status') not in ('queued', 'running', 'dispatching') and stages[-1]['state'] == 'running':
            stages[-1].update(state='unknown', detail='任务已结束，但未记录此阶段的最终结果')
    stage_index = {s['id']: s for s in stages}
    raw_issues = evidence.get('issues') if valid else None
    issues = [i for i in (_issue(v) for v in raw_issues[:20]) if i] if isinstance(raw_issues, list) else []
    failure = deepcopy(issues[0]) if issues else None
    if failure and failure['code'] == 'NATIVE_EXECUTION_FAILED':
        # A correlated guard denial refines the generic native terminal error.
        # Both original issues remain visible; prose never determines a stage.
        failure = deepcopy(next((i for i in issues if i['code'] == 'TOOL_PERMISSION_DENIED'), failure))
    if failure is None and task.get('error'):
        failure = {'stage': 'unknown', 'code': 'LEGACY_ERROR' if not valid else 'UNCLASSIFIED_ERROR',
                   'message': _text(task.get('error')), 'source': 'legacy' if not valid else 'task_record',
                   'category': 'unknown', 'rule_id': '',
                   'kind': 'unknown', 'kind_label': PROBLEM_LABELS['unknown']}

    test = _dict(task.get('test_result'))
    code = test.get('exit_code')
    code = code if type(code) is int else None
    test_state = 'unknown'
    source = _text(test.get('source'), 80) or ('recorded_test' if test else 'none')
    if code is not None:
        test_state = 'passed' if code == 0 else 'failed'
        if source in ('workbuddy-native-tools', 'zcode-native-tools', 'zcode-mcp-command') and test.get('verified') is not True and code == 0:
            test_state = 'unknown'
    elif not task.get('test_command') and not test:
        test_state = 'not_requested'
    elif valid and stage_index['verify']['state'] in ('not_run', 'not_requested'):
        test_state = stage_index['verify']['state']
    if not valid and test_state in ('passed', 'failed'):
        stage_index['verify'].update(state='completed' if test_state == 'passed' else 'failed',
                                      source=source, detail='历史记录中的测试退出码；不是独立验收')
    cleanup = _dict(task.get('workbuddy')).get('cleanup')
    if not valid and task.get('engine') == 'workbuddy' and cleanup in ('closed', 'unknown', 'not_created'):
        stage_index['session_close'].update(
            state={'closed': 'completed', 'unknown': 'unknown', 'not_created': 'not_applicable'}[cleanup],
            source='adapter', detail='原生会话关闭记录')

    files = _files(task.get('changed_files'))
    artifact_state = 'changed' if files else 'none' if task.get('changed_files') == [] else 'unknown'
    manifest=_dict(task.get('artifact_manifest'))
    committed=(manifest.get('source')=='controller_git_diff' and manifest.get('commit')
               and manifest.get('commit')==_dict(task.get('workspace')).get('commit'))
    scope_label=('按成果提交核对业务文件；忽略文件与外部路径未纳入统计，仍需独立验收' if committed else
                 '仅核对任务声明文件；是记录时的改动，不代表当前版本已验收')
    if _dict(task.get('task_policy')).get('write_scope')=='workspace' and not committed:
        scope_label='仅核对预期文件；辅助文件、忽略文件和外部路径未完整统计，仍需独立验收'
        if not files:artifact_state='unknown'
    if stage_index['artifacts']['state'] == 'failed':
        artifact_state = 'unknown'
    attribution = 'task_commit' if committed else 'observed_only'
    if task.get('kind') == 'chat':
        scope_label = '只读任务观察窗口内的文件变化，不能据此归因为执行器写入；不是执行器交付清单'
        attribution = 'observed_only'
    authoritative = _dict(review) if not review_error else {}
    import task_finalization
    settlement = task_finalization.view(task)
    state = authoritative.get('state')
    accepted = (state in ('accepted', 'final_accepted') and authoritative.get('acceptance_valid') is True
                and settlement['state'] in {'legacy', 'settled'})
    result = {'schema_version': SCHEMA, 'legacy': not valid, 'read_only': True,
            'execution': {'status': _text(task.get('status'), 40), 'engine': _text(task.get('engine'), 40)},
            'stages': stages, 'failure': failure, 'issues': issues,
            'controller_finalization': settlement,
            'artifacts': {'state': artifact_state, 'files': files,
                          'scope_label': scope_label, 'attribution': attribution},
            'verification': {'state': test_state, 'source': source, 'exit_code': code, 'independent': False},
            'cleanup': {'session': stage_index['session_close']['state'],
                        'temporary_files': stage_index['temporary_cleanup']['state']},
            'review': {'state': state, 'accepted': bool(accepted),
                       'unreadable': bool(review_error or authoritative.get('state') == 'unreadable'),
                       'reason': _text(authoritative.get('blocked_reason') if state == 'blocked'
                                       else authoritative.get('problems') if state == 'returned' else '', 600),
                       'source': 'independent_review' if authoritative else 'unknown'}}
    result['integration'] = integration if isinstance(integration, dict) else {
        'state': 'not_applicable' if task.get('kind') == 'chat' else 'unavailable', 'records': []}
    import task_status
    result['summary'] = task_status.task_summary(task, result)
    return result


def project_with_context(task, review=None, *, review_error='', integration_cache=None,
                         context_project=None, run=None):
    """Add one bounded integration snapshot; cache is local to a caller request."""
    import integration_status
    task = _dict(task)
    # The presentation layer must not turn a cached task.project into new
    # cross-project read authority. Use a validated caller binding or the real
    # project-owned run directory; an unscoped legacy record stays unknown.
    from pathlib import Path
    project_name = ''
    if isinstance(context_project, str) and Path(context_project).is_absolute():
        project_name = context_project
    elif context_project is None and run is not None:
        directory = Path(run).resolve()
        if directory.parent.name == 'runs' and directory.parent.parent.name == '.model-desk':
            project_name = str(directory.parent.parent.parent)
    snapshot = None
    if task.get('kind') != 'chat' and project_name:
        if integration_cache is not None:
            if project_name not in integration_cache:
                integration_cache[project_name] = integration_status.read_project(project_name)
            snapshot = integration_cache[project_name]
        else:
            snapshot = integration_status.read_project(project_name)
    integration = integration_status.for_task(snapshot or {}, task)
    return project(task, review, review_error=review_error, integration=integration)
