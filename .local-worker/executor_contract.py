"""Minimal executor lifecycle and capability contract (host-neutral core).

The 2026-10-25 execution-side plan (§4) fixes one lifecycle for every executor
chain and an honest per-engine capability declaration, without unifying tools
or rewriting the four workers:

    probe → prepare execution root → create native session → send the task
    once → receive events → collect the outcome → close this session →
    controller finalization → independent acceptance

This module is deliberately a description-and-projection layer, not a second
supervisor:

* No dispatch, no session creation, no record writes, no retries.  Importing
  this module never starts services or model work.
* ``describe(engine)`` freezes, per executor, how each lifecycle step maps onto
  the controller-visible ``execution_result`` stages, which *existing* record
  fields and evidence files already carry it, and the capability tri-state
  (supported/unsupported/unverified) with its real delegate names.
* ``native_view(engine, task_state)`` projects one task's recorded executor
  fields into a canonical read-only shape.  Missing values stay ``None``
  (unknown); nothing is inferred from answer or error prose.
* ``resend_decision(engine, task_state)`` is the shared conservative gate for
  "an unknown send is never resent".  It is the contract the workers' own
  recovery guards must agree with (see ``tests/test_executor_contract.py``);
  the workers keep enforcing their own guards, so this module is never the
  single point a caller could bypass.

Compatible-shell rule: capability declarations for all four engines stay in
``executor_adapter`` (one table, honest bindings); this module adds the
lifecycle mapping, the record projection and the resend gate on top.  Old
records never migrate to new field names, and no engine gains a capability it
has not measured.  ``dsh``/``pi`` records carry no per-send certificate, so
their resend verdict is ``unproven`` by design: the controller's atomic claim
gate stays responsible for them.
"""
from copy import deepcopy

SCHEMA = 1

# Canonical lifecycle steps in execution order.  ``stage`` names the shared
# ``execution_result`` stage whose evidence carries the step; ``None`` marks
# the brain's independent acceptance, which is outside worker execution.
LIFECYCLE = (
    {'step': 'probe', 'label': '探测连接与安装身份', 'stage': 'prepare'},
    {'step': 'execution_root', 'label': '准备执行根（原树或任务副本）', 'stage': 'prepare'},
    {'step': 'session_create', 'label': '创建专用原生会话', 'stage': 'prepare'},
    {'step': 'prompt_send', 'label': '发送一次任务提示词', 'stage': 'execute'},
    {'step': 'events', 'label': '接收执行事件', 'stage': 'execute'},
    {'step': 'outcome_collect', 'label': '收集成果与自测证据', 'stage': 'verify'},
    {'step': 'session_close', 'label': '关闭本会话', 'stage': 'session_close'},
    {'step': 'finalize', 'label': '控制器收尾与资源释放', 'stage': 'record'},
    {'step': 'acceptance', 'label': '独立验收（主脑）', 'stage': None},
)

# One vocabulary for native session closure, taken from the ZCode field the
# controller already persists; other engines' states project onto it.
CLOSED = 'closed'
PENDING = 'pending'
NOT_CREATED = 'not_created'
UNKNOWN = 'unknown'
SESSION_STATES = (CLOSED, PENDING, NOT_CREATED, UNKNOWN)

# Resend verdicts.  ``unproven`` means this engine's record cannot decide and
# the controller's atomic claim gate stays responsible; it never means "allow".
RESEND_ALLOWED = 'allowed'
RESEND_DENIED = 'denied'
RESEND_UNPROVEN = 'unproven'

# How the per-send certificate is recorded.  ``recorded``/``absent`` refer to
# the native state block this engine persists; ``not_recorded`` means the
# engine has no such field at all (dsh/pi rely on the controller claim gate).
CERT_RECORDED = 'recorded'
CERT_ABSENT = 'absent'
CERT_NOT_RECORDED = 'not_recorded'

_SANDBOX_DISCLAIMER = '不是操作系统沙箱'

DESCRIPTIONS = {
    'dsh': {
        'worker': 'dsh_worker.run_task',
        'runtime': 'agent-runtime/run-dsh.mjs',
        'session': {'id_fields': ('dsh_session_id', 'dsh_session_key'),
                    'fingerprint_field': 'connection_fingerprint',
                    'resume': 'same_session_key', 'close_field': None},
        'evidence': {'job': 'dsh-job.json', 'record_proof': 'record-proof.json',
                     'preset_proof': 'preset-proof.json', 'log': 'dsh.log',
                     'cancel_flag': 'cancelled.flag'},
        'isolation': {'permission_modes': ('approve-all', 'approve-reads'),
                      'boundary': '记录保护插件 + 访问快照的工具参数保护；' + _SANDBOX_DISCLAIMER},
        # Lifecycle recovery: the native session lives inside the runtime child
        # process, so the controller observing the child exit IS the close
        # evidence; there is no separate native-close entrypoint to expose.
        'recovery': {'close': None, 'evidence': 'controller child-process identity (worker/child pid)',
                     'instance_change': 'process identity match; unknown stays retained'},
        'native_state_field': None,
        'prompt_attempted_field': None,
        'outcome_unknown_field': None,
    },
    'pi': {
        'worker': 'pi_worker.run_task',
        'runtime': 'agent-runtime/run-pi.mjs',
        'session': {'id_fields': ('pi_session_id', 'pi_session_file'),
                    'fingerprint_field': 'connection_fingerprint',
                    'resume': 'same_session_file', 'close_field': None},
        'evidence': {'job': 'pi-job.json', 'proof': 'pi-proof.json', 'log': 'pi.log',
                     'cancel_flag': 'cancelled.flag'},
        'isolation': {'permission_modes': ('danger-full-access', 'read-only'),
                      'boundary': 'Pi 扩展内工具参数保护；' + _SANDBOX_DISCLAIMER},
        'recovery': {'close': None, 'evidence': 'controller child-process identity (worker/child pid)',
                     'instance_change': 'process identity match; unknown stays retained'},
        'native_state_field': None,
        'prompt_attempted_field': None,
        'outcome_unknown_field': None,
    },
    'workbuddy': {
        'worker': 'workbuddy_worker.run_task',
        'runtime': 'workbuddy_executor.NativeChatClient（ACP 进程内传输）',
        'session': {'id_fields': ('workbuddy_session_id',),
                    'native_state_field': 'workbuddy', 'resume': 'none',
                    'close_field': ('workbuddy', 'cleanup')},
        'evidence': {'native_job': 'workbuddy-native-job.json',
                     'native_proof': 'workbuddy-native-proof.json',
                     'native_events': 'workbuddy-native-events.jsonl',
                     'denied_flag': 'workbuddy-native-denied.flag',
                     'legacy_job': 'workbuddy-tools-job.json',
                     'legacy_proof': 'workbuddy-tools-proof.json',
                     'cancel_flag': 'cancelled.flag'},
        'isolation': {'access_modes': ('project', 'full'),
                      'boundary': '原生 hook 工具参数保护（与 ZCode 共享 workbuddy-native-guard）；' + _SANDBOX_DISCLAIMER},
        'recovery': {'close': 'workbuddy_worker.cleanup_lease（精确 session_id/runtime_id/channel；真机验证另记）',
                     'evidence': 'workbuddy.cleanup + lease identity',
                     'instance_change': 'lease runtime_id binding; never the GUI'},
        'native_state_field': 'workbuddy',
        'prompt_attempted_field': ('workbuddy', 'prompt_attempted'),
        'outcome_unknown_field': ('workbuddy', 'outcome_unknown'),
    },
    'zcode': {
        'worker': 'zcode_worker.run_task',
        'runtime': 'agent-runtime/run-zcode.mjs（本机 CDP 桥）',
        'session': {'id_fields': ('zcode_session_id',), 'id_prefix': 'modeldesk-zcode-',
                    'native_state_field': 'zcode', 'resume': 'handoff_new_session',
                    'close_field': ('zcode', 'cleanup')},
        'evidence': {'native_job': 'zcode-native-job.json',
                     'native_proof': 'zcode-native-proof.json',
                     'native_events': 'zcode-native-events.jsonl',
                     'denied_flag': 'zcode-native-denied.flag',
                     'session_index': '.model-desk/zcode-native-sessions.json',
                     'legacy_job': 'zcode-tools-job.json',
                     'legacy_proof': 'zcode-tools-proof.json',
                     'cancel_flag': 'cancelled.flag'},
        'isolation': {'access_modes': ('project', 'full'),
                      'boundary': '原生 hook 工具参数保护（workbuddy-native-guard + record-policy）；' + _SANDBOX_DISCLAIMER},
        'recovery': {'close': 'zcode_worker.finalize_session（--finalize-session；实例证据或精确会话桥接关闭）',
                     'evidence': 'zcode.cleanup + zcode_instance + zcode_session_id',
                     'instance_change': 'pinned pid/created_at/command digest; unknown stays retained'},
        'native_state_field': 'zcode',
        'prompt_attempted_field': ('zcode', 'prompt_attempted'),
        'outcome_unknown_field': ('zcode', 'outcome_unknown'),
    },
}


def engines():
    return tuple(DESCRIPTIONS)


def _engine_name(value):
    name = str(value or '').strip().lower()
    if name not in DESCRIPTIONS:
        raise ValueError('未知执行端契约：' + str(value))
    return name


def _descendant(state, path, default=None):
    value = state
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def _cleanup_from_stage(state):
    """Project dsh/pi controller evidence onto the shared session vocabulary.

    The dsh/pi workers have no native cleanup field: the controller observes
    the runtime child process exiting and records the ``session_close`` stage.
    Failed/missing evidence stays unknown; it is never read as closed.
    """
    evidence = state.get('execution_evidence')
    stages = evidence.get('stages') if isinstance(evidence, dict) else {}
    raw = stages.get('session_close') if isinstance(stages, dict) else {}
    value = raw.get('state') if isinstance(raw, dict) else None
    if value == 'completed':
        return CLOSED
    if value == 'not_applicable':
        return NOT_CREATED
    return UNKNOWN


def describe(engine):
    """The frozen contract for one executor, plus its capability declaration."""
    name = _engine_name(engine)
    import executor_adapter
    base = DESCRIPTIONS[name]
    return {
        'schema': SCHEMA,
        'engine': name,
        'worker': base['worker'],
        'runtime': base['runtime'],
        'lifecycle': [dict(step) for step in LIFECYCLE],
        'session': deepcopy(base['session']),
        'evidence': deepcopy(base['evidence']),
        'isolation': deepcopy(base['isolation']),
        'recovery': deepcopy(base['recovery']),
        'capabilities': executor_adapter.engine_capabilities(name),
    }


def native_view(engine, task_state):
    """Canonical read-only projection of one task's recorded executor fields.

    Every field is taken from what the worker already persisted; a missing
    value stays ``None``/``unknown`` instead of being inferred.  For dsh/pi
    ``cleanup`` is projected from the controller's ``session_close`` stage, and
    ``prompt_certificate`` is ``not_recorded`` because their records carry no
    per-send field.
    """
    name = _engine_name(engine)
    state = task_state if isinstance(task_state, dict) else {}
    base = DESCRIPTIONS[name]
    native_state_field = base.get('native_state_field')
    native = state.get(native_state_field) if native_state_field else None
    native = native if isinstance(native, dict) else {}
    prompt_path = base.get('prompt_attempted_field')
    prompt = _descendant(state, prompt_path) if prompt_path else None
    prompt = prompt if type(prompt) is bool else None
    unknown_path = base.get('outcome_unknown_field')
    outcome_unknown = _descendant(state, unknown_path) if unknown_path else None
    outcome_unknown = outcome_unknown if type(outcome_unknown) is bool else None
    if prompt_path is None:
        certificate = CERT_NOT_RECORDED
    elif prompt is None:
        certificate = CERT_ABSENT
    else:
        certificate = CERT_RECORDED
    close_path = base['session'].get('close_field')
    cleanup = _descendant(state, close_path) if close_path else _cleanup_from_stage(state)
    if cleanup not in SESSION_STATES:
        cleanup = UNKNOWN
    session_id = next((state.get(field) for field in base['session']['id_fields']
                       if isinstance(state.get(field), str) and state.get(field)), None)
    return {'schema': SCHEMA, 'engine': name, 'session_id': session_id,
            'cleanup': cleanup, 'prompt_attempted': prompt,
            'outcome_unknown': outcome_unknown, 'prompt_certificate': certificate}


def resend_decision(engine, task_state):
    """Whether re-dispatching this task is contractually allowed.

    ``allowed`` requires the record to prove the prompt was never attempted and
    the session closure state is safe.  Unknown sends are never resent; engines
    without a per-send certificate answer ``unproven`` so the controller's
    atomic claim gate (not this module) stays responsible for them.  Advisory
    for callers: each worker's own recovery guard keeps enforcing refusal.
    """
    name = _engine_name(engine)
    view = native_view(name, task_state)
    if view['prompt_certificate'] == CERT_NOT_RECORDED:
        return {'verdict': RESEND_UNPROVEN, 'view': view,
                'reason': 'DSH/Pi 记录不含逐请求发送凭据；重派由控制器原子认领与显式恢复决定'}
    if view['prompt_certificate'] == CERT_ABSENT:
        return {'verdict': RESEND_UNPROVEN, 'view': view,
                'reason': '记录早于原生会话凭据；是否重派由控制器认领与显式恢复决定'}
    if view['prompt_attempted'] is True:
        return {'verdict': RESEND_DENIED, 'view': view,
                'reason': '原请求可能已送达；不自动重发'}
    # Older records may omit this field. An explicitly malformed field is
    # different: it cannot certify a known outcome (including JSON null).
    missing = object()
    raw_unknown = _descendant(task_state, DESCRIPTIONS[name]['outcome_unknown_field'], missing)
    if raw_unknown is not missing and type(raw_unknown) is not bool:
        return {'verdict': RESEND_DENIED, 'view': view,
                'reason': '执行结果凭据类型无效；不授权重发'}
    if view['outcome_unknown'] is True:
        return {'verdict': RESEND_DENIED, 'view': view,
                'reason': '执行结果未知；保留证据，不换工具或渠道重试'}
    if view['cleanup'] in (PENDING, UNKNOWN):
        return {'verdict': RESEND_DENIED, 'view': view,
                'reason': '原生会话关闭未确认；先显式核对收尾'}
    return {'verdict': RESEND_ALLOWED, 'view': view,
            'reason': '记录证明本轮未发送且会话状态安全'}
