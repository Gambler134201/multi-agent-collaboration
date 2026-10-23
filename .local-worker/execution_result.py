"""Versioned, read-only result projection shared by every brain and executor.

Stage evidence is written by controllers, never inferred from answer/error prose.
This module does no IO, calls no tools and cannot accept, resume or retry work.
Artifacts describe the recorded declared-file scan, not the current filesystem.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re

SCHEMA = 1
STAGES = {'prepare': '准备', 'execute': '执行', 'verify': '验证与自测',
          'temporary_cleanup': '临时清理', 'session_close': '会话关闭',
          'artifacts': '成果核对', 'record': '结果记录'}
STATES = {'unknown', 'running', 'completed', 'failed', 'cancelled',
          'not_run', 'not_requested', 'not_applicable'}
SOURCES = {'controller', 'controller_test', 'adapter', 'adapter_tool',
           'protection_verification', 'persistence', 'child_process'}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value, limit=800):
    return value[:limit] if isinstance(value, str) else ''


def _issue(value):
    value = _dict(value)
    stage, source = value.get('stage'), value.get('source')
    if not isinstance(stage, str) or stage not in STAGES or not isinstance(source, str) or source not in SOURCES:
        return None
    return {'stage': stage, 'code': _text(value.get('code'), 100) or 'UNKNOWN',
            'message': _text(value.get('message')), 'source': source,
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


def project(task, review=None, *, review_error=''):
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
                   'category': 'unknown', 'rule_id': ''}

    test = _dict(task.get('test_result'))
    code = test.get('exit_code')
    code = code if type(code) is int else None
    test_state = 'unknown'
    source = _text(test.get('source'), 80) or ('recorded_test' if test else 'none')
    if code is not None:
        test_state = 'passed' if code == 0 else 'failed'
        if source == 'workbuddy-native-tools' and test.get('verified') is not True and code == 0:
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
    if stage_index['artifacts']['state'] == 'failed':
        artifact_state = 'unknown'
    authoritative = _dict(review) if not review_error else {}
    state = authoritative.get('state')
    accepted = state in ('accepted', 'final_accepted') and authoritative.get('acceptance_valid') is True
    return {'schema_version': SCHEMA, 'legacy': not valid, 'read_only': True,
            'execution': {'status': _text(task.get('status'), 40), 'engine': _text(task.get('engine'), 40)},
            'stages': stages, 'failure': failure, 'issues': issues,
            'artifacts': {'state': artifact_state, 'files': files,
                          'scope_label': '仅核对任务声明文件；是记录时的改动，不代表当前版本已验收'},
            'verification': {'state': test_state, 'source': source, 'exit_code': code, 'independent': False},
            'cleanup': {'session': stage_index['session_close']['state'],
                        'temporary_files': stage_index['temporary_cleanup']['state']},
            'review': {'state': state, 'accepted': bool(accepted),
                       'source': 'independent_review' if authoritative else 'unknown'}}
