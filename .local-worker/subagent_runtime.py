"""Controlled DSH native subagent runtime: gate, one-level probe, lifecycle.

D2 scope
--------

* A normal task may only mount a child when ``model_capabilities`` grants it for
  the exact identity (executor + model + preset content + endpoint + release) and
  the user policy is ``allow``.  Home/local models stay denied.
* A *probe* is the only caller allowed to mount a child without a stored grant.
  It is explicit, non-local, paid-authorized, route-checked and gated on the
  already independently accepted first real two-stage handover.  It never writes
  a ``supported`` row into ``models.json``; it writes measurement evidence to a
  separate file for the main brain to inspect.
* The child is a single in-process ``spawn`` (fresh empty session, no fork of
  the parent history), pinned through ``agentOptions`` to the parent's
  ``desk-worker`` route.  Every child LLM call therefore hits the same loopback
  gateway, which rejects any request whose model differs from the parent model,
  so there is no silent fallback to a bare API.
* Parent/child identity comes from the DSH lifecycle edges recorded by the
  executor-side bridge (``agent-runtime/subagent-bridge.cjs``), never from
  parsing the answer text.

Honest limits: DSH exposes no per-child token field, so the child's *bill* is
reported as the provable aggregate only; the lifecycle window is reported as a
non-authoritative correlation, never as a fabricated child invoice.  Scope is a
working constraint, not an OS sandbox.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

import executor_adapter
import model_capabilities
import model_registry as registry
import model_router
import workflow_settings

HERE = Path(__file__).resolve().parent
SCHEMA = 1
LEDGER_NAME = 'subagent-ledger.jsonl'
EVIDENCE_PATH = HERE / 'tasks/subagent-probe-evidence.json'
SMOKE_RECORD = HERE / 'tasks/stage-smoke-record.json'
MAX_DEPTH = 1
PROBE_KIND = 'chat'
PROBE_TIMEOUT = 180
LEDGER_SOURCE = 'dsh-subagent-lifecycle'
EVIDENCE_KIND = 'subagent-probe-evidence'
PROBE_PROMPT = (
    '这是受控子代理验证，只读，不要修改任何文件。\n'
    '请只做一件事：调用 subagent 工具一次，把它的 prompt 设为「只回复一个词：ok」。\n'
    '不要多次调用，不要重试，不要后台运行。收到子代理返回后，只回复它返回的那个词。'
)
CANCEL_PROMPT = (
    '这是受控子代理取消验证，只读，不要修改任何文件。\n'
    '请调用 subagent 工具一次，prompt 设为「只回复一个词：ok」。不要重试。'
)

# --------------------------------------------------------------- edit probe
# The chat probe above only ever proved read-only delegation.  The edit probe is
# a *separate, explicit* path that measures the full-access ``edit`` mode on one
# disposable file inside the probe run's own workspace.  It is not reachable from
# an ordinary task: the normal gate still denies ``edit`` unless a stored edit
# measurement exists, and the probe requires its own CLI flag plus an in-process
# probe context, so a task that merely sets ``subagent_probe`` cannot mount a
# full-access child.
EDIT_PROBE_KIND = model_capabilities.MODE_EDIT
EDIT_PROBE_TIMEOUT = 240
EDIT_PROBE_SANDBOX = 'danger-full-access'
EDIT_PROBE_PERMISSION = 'approve-all'
EDIT_PROBE_EVIDENCE_KIND = 'subagent-edit-probe-evidence'
EDIT_PROBE_EVIDENCE_PATH = HERE / 'tasks/edit-subagent-evidence.json'
EDIT_PROBE_LOCK_NAME = 'edit-probe.lock'
# A lock older than this is treated as a crashed run's leftover and reclaimed;
# within the window a second probe is refused instead of silently re-charged.
EDIT_PROBE_LOCK_TTL = 1800
EDIT_PROBE_NONCE_BYTES = 16
_ACTIVE_PROBE = None


def edit_probe_prompt(relative_target, nonce):
    """Parent prompt: exactly one child writes the runtime nonce to one file.

    The random value is generated here and only ever appears in this prompt, so
    the file's content is not derivable by the child from anything else.  The
    parent is told not to touch the file itself; the recorded parent tool calls
    confirm whether it did.
    """
    return (
        '这是受控 edit 子代理写入验证：一次性，禁止重试、禁止后台、禁止并发、禁止嵌套。\n'
        f'本任务的隔离工作目录中唯一允许写入的文件是：{relative_target}\n\n'
        '请严格只做一次：\n'
        '1. 调用 subagent 工具恰好一次。把子代理的 prompt 设为下面这段（原样，不要改动其中内容）：\n'
        f'   「请调用你的写文件工具，把下面这一行内容原样写入文件 {relative_target}'
        '（相对你的工作目录；只写这一个文件，覆盖写入，不要追加任何其它字符）：\n'
        f'{nonce}\n'
        '写完后只回复一个词：ok」\n'
        f'2. 你自己不要创建、修改、删除或写入任何文件，尤其不要直接写入 {relative_target}；'
        '写入必须由子代理通过它自己的工具调用完成。\n'
        '3. 收到子代理回复后，只回复它返回的那个词。\n'
    )


def edit_probe_scope(run, target, runs=None):
    """Bind the probe run and its one writable file to the verified runs tree.

    A scope is a working constraint, not an OS sandbox: the containment check
    below proves only that the run directory is inside the accepted ``runs``
    tree and the target file is inside that run's own ``workspace``.  It does not
    claim the rest of the machine is unreachable under danger-full-access.
    """
    runs = Path(runs) if runs else registry.RUNS
    checks = {'run_inside_runs': False, 'target_inside_run_workspace': False,
              'run_declares_edit_kind': False, 'run_declares_edit_probe': False,
              'scope_evaluated': bool(run is not None and target is not None)}
    reasons = []
    if run is None or target is None:
        return {'checks': checks, 'reasons': reasons}
    run = Path(run)
    try:
        runs_resolved = runs.resolve()
        run_resolved = run.resolve()
        workspace = (run / 'workspace').resolve()
        target_resolved = Path(target).resolve()
    except OSError as error:
        reasons.append(f'探测路径无法解析：{error}')
        return {'checks': checks, 'reasons': reasons}
    checks['run_inside_runs'] = run_resolved != runs_resolved and runs_resolved in run_resolved.parents
    if not checks['run_inside_runs']:
        reasons.append('探测 run 目录不在已验证的 runs 目录下')
    checks['target_inside_run_workspace'] = (target_resolved != workspace
                                             and workspace in target_resolved.parents)
    if not checks['target_inside_run_workspace']:
        reasons.append('目标文件不在该探测 run 的隔离 workspace 内（路径越界）')
    state = registry.read_task(run) if run.is_dir() else {}
    state = state if isinstance(state, dict) else {}
    checks['run_declares_edit_kind'] = state.get('kind') == EDIT_PROBE_KIND
    if not checks['run_declares_edit_kind']:
        reasons.append('探测 run 未声明 kind=edit，拒绝以其它模式冒称 edit')
    checks['run_declares_edit_probe'] = bool(state.get('subagent_edit_probe'))
    if not checks['run_declares_edit_probe']:
        reasons.append('探测 run 未声明专用 edit 探测标记')
    return {'checks': checks, 'reasons': reasons}


def _set_active_probe(context):
    global _ACTIVE_PROBE
    _ACTIVE_PROBE = context if isinstance(context, dict) else None
    return _ACTIVE_PROBE


def _clear_active_probe():
    global _ACTIVE_PROBE
    _ACTIVE_PROBE = None


def probe_context_path(runs=None, pid=None):
    """A process-scoped probe context file.

    The CLI runs this file as ``__main__`` while ``dsh_worker`` imports it as
    ``subagent_runtime``, so a module global would not be visible across the two
    module instances.  The context is therefore also persisted per process id so
    the enforcement point reads exactly the probe that is in flight.
    """
    runs = Path(runs) if runs else registry.RUNS
    return runs / f'.probe-context-{pid or os.getpid()}.json'


def _persist_probe_context(context, runs=None):
    if not isinstance(context, dict):
        return None
    path = probe_context_path(runs)
    path.parent.mkdir(parents=True, exist_ok=True)
    registry.write_json(path, context)
    return path


def _clear_persisted_probe_context(runs=None):
    try:
        probe_context_path(runs).unlink()
    except OSError:
        pass


def load_probe_context(runs=None):
    if isinstance(_ACTIVE_PROBE, dict):
        return _ACTIVE_PROBE
    data = _read_json(probe_context_path(runs))
    return data if isinstance(data, dict) else None


def edit_probe_lock_path(runs=None):
    return (Path(runs) if runs else registry.RUNS) / EDIT_PROBE_LOCK_NAME


def acquire_edit_probe_lock(runs=None, now=None):
    """Refuse a concurrent or repeated edit probe before any paid dispatch.

    ``O_EXCL`` creation is the guard: a second probe started while one is in
    flight raises instead of spending again.  A lock older than the TTL is a
    crashed run's leftover and is reclaimed once, with the reclaim recorded.
    """
    path = edit_probe_lock_path(runs)
    path.parent.mkdir(parents=True, exist_ok=True)
    moment = now if isinstance(now, datetime) else _now()
    reclaimed = False
    try:
        handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        previous = _read_json(path) or {}
        created = _parse_time(previous.get('at')) if isinstance(previous, dict) else None
        stale = bool(created and (moment - created).total_seconds() > EDIT_PROBE_LOCK_TTL)
        if not stale:
            raise ProbeDenied('已有 edit 探测在途（锁文件存在），拒绝并发或重复派发，不重试也不重复计费')
        try:
            path.unlink()
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            reclaimed = True
        except OSError as error:
            raise ProbeDenied(f'edit 探测锁无法回收：{error}')
    os.write(handle, json.dumps({'pid': os.getpid(), 'at': moment.isoformat(),
                                 'by': 'subagent_runtime.edit_probe', 'reclaimed_stale': reclaimed},
                                ensure_ascii=False).encode('utf-8'))
    os.close(handle)
    return {'path': str(path), 'reclaimed_stale': reclaimed}


def release_edit_probe_lock(lock):
    path = lock.get('path') if isinstance(lock, dict) else lock
    if not path:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


def new_edit_probe_run(model_id, runs=None, nonce=None):
    """Create the isolated run/workspace the edit probe measures inside."""
    runs = Path(runs) if runs else registry.RUNS
    nonce = nonce or secrets.token_hex(EDIT_PROBE_NONCE_BYTES)
    task_id = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    run = runs / task_id
    workspace = run / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    relative_target = f'edit-probe-{nonce[:8]}.txt'
    target = workspace / relative_target
    (run / 'prompt.txt').write_text(edit_probe_prompt(relative_target, nonce), encoding='utf-8')
    registry.patch_task(run, id=task_id, status='queued', kind=EDIT_PROBE_KIND, engine='dsh',
                        source='subagent-edit-probe', model_id=model_id,
                        project=str(workspace), files=[relative_target], timeout=EDIT_PROBE_TIMEOUT,
                        test_command='', route={'mode': 'manual', 'reason': '受控 edit 子代理写入探测'},
                        title='受控 edit 子代理写入探测', subagent_probe=True,
                        subagent_edit_probe=True, probe_target=relative_target,
                        probe_nonce=nonce, sandbox_mode=EDIT_PROBE_SANDBOX, created_at=registry.now())
    return {'run': run, 'task_id': task_id, 'workspace': workspace,
            'relative_target': relative_target, 'target': target, 'nonce': nonce}


def authorize_edit_probe(model, settings=None, root=None, runs=None, run=None, target=None):
    """The strict, explicit gate an edit probe must pass before any dispatch."""
    pre = probe_preconditions(model, settings=settings, explicit=True, root=root, runs=runs,
                              mode=EDIT_PROBE_KIND, run=run, target=target)
    if not pre['allowed']:
        raise ProbeDenied('受控 edit 探测未获许可：' + '；'.join(pre['reasons']))
    return {'schema': SCHEMA, 'kind': 'controlled-subagent-edit-probe', 'mode': EDIT_PROBE_KIND,
            'sandbox': EDIT_PROBE_SANDBOX, 'permission_mode': EDIT_PROBE_PERMISSION,
            'model_id': model.get('id'), 'model': model.get('model'), 'endpoint': model.get('api_base'),
            'agent_preset': model.get('agent_preset') or model_capabilities.DEFAULT_PRESET,
            'max_depth': MAX_DEPTH, 'provider': 'spawn', 'fork': False, 'background': False,
            'nested': False, 'child_count_per_round': 1, 'writes_models_json': False,
            'requires_stored_support': False, 'preconditions': pre}


def write_check(run, relative_target, nonce, final):
    """Read the probe file back and record the exact bytes/SHA it contains.

    This is the file-level evidence the main brain can independently reproduce:
    the path inside the run workspace, the raw SHA-256, the stripped content and
    whether it equals the runtime-generated nonce.  Whether the *child* rather
    than the parent did the write is a separate, honestly-labelled attribution.
    """
    run = Path(run)
    workspace = (run / 'workspace').resolve()
    target = (workspace / relative_target).resolve()
    inside = target != workspace and workspace in target.parents
    expected_sha = hashlib.sha256((nonce + '\n').encode('utf-8')).hexdigest()
    block = {'relative_target': relative_target, 'target_on_disk': str(target),
             'inside_run_workspace': inside, 'expected_nonce': nonce,
             'expected_sha256_of_nonce_line': expected_sha,
             'exists': target.is_file(), 'size': None, 'sha256': None,
             'content_stripped': None, 'content_matches_nonce': False}
    if target.is_file():
        raw = target.read_bytes()
        text = raw.decode('utf-8', 'replace')
        block.update({'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                      'content_stripped': text.strip()[:200],
                      'content_matches_nonce': text.strip() == nonce})
    tool_calls = final.get('tool_calls') if isinstance(final.get('tool_calls'), list) else []
    titles = [str(row.get('title')) for row in tool_calls if isinstance(row, dict) and row.get('title')]
    files = [str(row.get('file')) for row in tool_calls if isinstance(row, dict) and row.get('file')]
    subagent_calls = sum(1 for title in titles if title == 'subagent')
    block['attribution'] = {
        'source': 'DSH 生命周期账本 + 父 ACP 工具事件（dsh_worker.tool_calls）+ 文件回读',
        'subagent_tool_calls': subagent_calls,
        'parent_tool_titles': titles[-20:],
        'parent_tool_files': files[-20:],
        'parent_recorded_touch_of_target': relative_target in files,
        'non_authoritative_note': 'DSH 不提供 per-child 文件归属；随机值只出现在委派提示里，'
                                  '且父被要求不得自行写该文件。该归因来自执行器事件与文件回读，'
                                  '属工作约束级证据，不是 OS 硬隔离证明。',
    }
    return model_capabilities.public_view(block)


def edit_probe_limits():
    return [
        'edit 探测在 danger-full-access 下运行：scope 是写给代理的工作约束，不是 OS 文件硬隔离',
        '只测量当前身份（执行器版本/模型/预设内容 revision/endpoint）的 edit 模式；身份漂移即失效',
        'DSH 不暴露 per-child 文件归属与 token 字段，写入归因与费用都只能是可证实聚合',
        '本探测不写 models.json，也不把该轮当成其它模型或 chat 模式的证据',
    ]


def build_edit_evidence(model, plan, run, task_id, final, relative_target, target, nonce, observation,
                        settings, exit_code=None):
    """Assemble the edit round; no secret, key or authorization value is included."""
    run = Path(run)
    rows = read_ledger(run)
    lineage = lifecycle(rows)
    usage = aggregate_usage(run, lineage)
    children = lineage.get('children') or []
    session_id = final.get('dsh_session_id')
    sections = child_sections(children, {**final, 'model': model.get('model')}, session_id,
                              lineage.get('orphan_starts'))
    measured = bool(final.get('status') in {'completed', 'cancelled'} and children)
    preset = final.get('agent_preset') or model.get('agent_preset') or model_capabilities.DEFAULT_PRESET
    revision = final.get('preset_revision') or executor_adapter.preset_content_revision(preset)
    return model_capabilities.public_view({
        'schema': SCHEMA,
        'kind': EDIT_PROBE_EVIDENCE_KIND,
        'generated_at': _now().isoformat(),
        'measured': measured,
        'read_only_probe': False,
        'mode': EDIT_PROBE_KIND,
        'sandbox_mode': final.get('sandbox_mode') or EDIT_PROBE_SANDBOX,
        'permission_mode': final.get('permission_mode') or EDIT_PROBE_PERMISSION,
        'writes_project_files': [],
        'writes_models_json': False,
        'stored_support_required': False,
        'cancel_check': False,
        'probe': {'requested': True, 'executed': bool(children), 'exit_code': exit_code,
                  'status': final.get('status'), 'plan': plan,
                  'deny': (final.get('subagent_capability') or {}).get('blocked')
                  if isinstance(final.get('subagent_capability'), dict) else None,
                  'error': final.get('error')},
        'identity': {
            'executor': 'dsh',
            'executor_version': final.get('executor_version') or executor_adapter.installed_executor_version(),
            'model_id': final.get('model_id') or model.get('id'),
            'model': model.get('model'),
            'endpoint': model.get('api_base'),
            'agent_preset': preset,
            'preset_revision': revision,
            'capability_identity': executor_adapter.model_identity(model, preset={'revision': revision}),
        },
        'task': {'task_id': task_id, 'run': str(run), 'parent_session_id': session_id,
                 'acpx_record_id': final.get('acpx_record_id'), 'kind': final.get('kind'),
                 'status': final.get('status'), 'project': final.get('project'),
                 'file_scope': [relative_target], 'changed_files': final.get('changed_files') or []},
        'file_scope': {
            'workspace': str((run / 'workspace').resolve()),
            'relative_target': relative_target,
            'target_on_disk': str(target),
            'inside_run_workspace': observation.get('inside_run_workspace'),
            'run_inside_verified_runs': True,
            'user_project_touched': False,
        },
        'write_check': observation,
        'ledger': {'path': str(ledger_path(run)), 'source': LEDGER_SOURCE,
                   'raw_events': subagent_events(rows), 'lineage': lineage},
        'child_identity': {
            'count': len(children),
            'child_ids': sections['child_ids'],
            'distinct_from_parent_session': sections['distinct_from_parent_session'],
            'providers': sections['providers'],
            'child_models': sections['child_models'],
            'child_model_matches_parent': sections['child_model_matches_parent'],
            'paired_lifecycle': sections['paired_lifecycle'],
        },
        'child_output': {
            'from_executor_tool_result': sections['from_executor_tool_result'],
            'from_lifecycle_last_message': sections['from_lifecycle_last_message'],
            'parent_answer_tail': sections['parent_answer_tail'],
            'note': 'ACP 工具完成事件是子代理输出的执行器证据；生命周期边的 lastAssistantMessage 可能为空',
        },
        'preset_proof': preset_proof_summary(run, final),
        'gateway': usage,
        'models_json_untouched': True,
        'policy_note': 'edit 探测只写独立的 edit 证据文件；是否把该模型的 policy 扩展为覆盖 edit '
                       '由主脑独立检查后决定，worker 不写 models.json',
        'limits': edit_probe_limits(),
        'reproduce_commands': reproduce_commands(model.get('id'), run, edit=True),
    })


def run_edit_probe(model_id, evidence_path=None, root=None, runs=None):
    """Execute exactly one full-access edit round inside an isolated run.

    Every precondition (registered non-local paid model, paid authorization,
    accepted first tranche, current version, scope binding, single-flight lock) is
    enforced before the single dispatch.  A failure is recorded once and never
    retried; no ``supported`` row is written and no user project file is touched.
    """
    runs = Path(runs) if runs else registry.RUNS
    model = registry.get_model(model_id, require_enabled=True)
    lock = acquire_edit_probe_lock(runs)
    try:
        created = new_edit_probe_run(model_id, runs=runs)
        run = created['run']
        if created['target'].exists():
            raise ProbeDenied('edit 探测目标文件已存在，拒绝覆盖')
        plan = authorize_edit_probe(model, root=root, runs=runs, run=run, target=created['target'])
        settings = workflow_settings.snapshot()
        import dsh_worker  # lazy: dsh_worker imports this module
        _set_active_probe({'mode': EDIT_PROBE_KIND, 'run': str(run),
                           'target': str(created['target']), 'nonce': created['nonce'],
                           'kind': 'edit-probe'})
        _persist_probe_context(_ACTIVE_PROBE, runs)
        state = registry.read_task(run)
        try:
            exit_code = dsh_worker.run_task(run, state, model)
        except Exception as error:  # record once; a paid dispatch is never retried
            exit_code = 1
            registry.patch_task(run, error=str(error)[:500])
        finally:
            _clear_active_probe()
        final = registry.read_task(run)
        observation = write_check(run, created['relative_target'], created['nonce'], final)
        evidence = build_edit_evidence(model, plan, run, created['task_id'], final,
                                       created['relative_target'], created['target'], created['nonce'],
                                       observation, settings, exit_code=exit_code)
        # dsh_worker's generic probe branch labels proven_modes from READ_ONLY_MODES;
        # record the authoritative measured mode beside it instead of rewriting it.
        registry.patch_task(run, probe_mode_provenance={
            'mode_measured': EDIT_PROBE_KIND, 'sandbox_mode': evidence.get('sandbox_mode'),
            'recorded_by': 'subagent_runtime.run_edit_probe',
            'note': '通用探测分支把 proven_modes 固定标为 chat；本轮实际 kind=edit，'
                    '权威模式以本字段和 edit 证据文件为准'})
        if evidence_path:
            write_evidence(evidence_path, evidence)
            registry.patch_task(run, subagent_edit_probe_evidence=str(evidence_path))
        return evidence
    finally:
        _clear_active_probe()
        _clear_persisted_probe_context(runs)
        release_edit_probe_lock(lock)


# ------------------------------------------------------------------ task value
# A grant only says a child *may* be mounted.  It must not turn every ordinary
# request into delegation: the benefit gate below requires one real expected
# value signal, and an explicit user instruction never to create a child always
# wins.  Numbers are deliberately conservative policy thresholds, not capability
# claims.
BENEFIT_MIN_FILES = 4
BENEFIT_MIN_PROMPT_CHARS = 4000
BENEFIT_MIN_EXPECTED_CALLS = 4
BENEFIT_COMPLEXITY = {'high', 'complex', 'very-high', 'very high'}
BENEFIT_CATEGORIES = {'research', 'audit', 'review', 'refactor', 'investigation', 'migration'}
_FORBID_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r'不要.{0,10}(创建|派发|启用|使用).{0,6}子代理',
    r'禁止.{0,10}子代理',
    r'不得.{0,10}(创建|使用).{0,6}子代理',
    r'no\s+sub-?agents?',
    r'without\s+(a\s+|any\s+)?sub-?agents?',
    r'do\s+not\s+(create|spawn|use)\s+(a\s+|any\s+)?sub-?agents?',
))
_REQUEST_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r'(创建|派发|启用|使用|调用).{0,6}子代理',
    r'子代理工具',
    r'\bsub-?agent\b',
    r'\bdelegat(e|ion)\b',
    r'委派',
))


def _matches(patterns, text):
    return [pattern.pattern for pattern in patterns if pattern.search(text)]


def task_benefit(task, prompt_text=None):
    """Whether this task has a real expected value for one child.

    Capability/policy decides *whether a child may be mounted at all*; this
    decides whether *this task* should use one.  A simple request with no
    parallel/independent work and no multi-file or multi-call scope is denied, so
    a default ``allow`` never turns every ordinary chat into delegation.
    """
    task = task if isinstance(task, dict) else {}
    if prompt_text is None:
        prompt_text = str(task.get('prompt') or '')
    prompt_text = str(prompt_text or '')
    files = task.get('files') if isinstance(task.get('files'), list) else []
    route = task.get('route') if isinstance(task.get('route'), dict) else {}
    requirements = route.get('requirements') if isinstance(route.get('requirements'), dict) else {}
    try:
        expected_calls = int(requirements.get('expected_calls') or 0)
    except (TypeError, ValueError):
        expected_calls = 0
    complexity = str(requirements.get('complexity') or '').strip().lower()
    category = str(requirements.get('category') or '').strip().lower()

    forbidden = _matches(_FORBID_PATTERNS, prompt_text)
    requested = _matches(_REQUEST_PATTERNS, prompt_text)
    signals = []
    if requested:
        signals.append('任务文本明确要求子代理/委派')
    if len(files) >= BENEFIT_MIN_FILES:
        signals.append(f'多文件范围（{len(files)} ≥ {BENEFIT_MIN_FILES}）')
    if len(prompt_text) >= BENEFIT_MIN_PROMPT_CHARS:
        signals.append(f'长任务文本（{len(prompt_text)} ≥ {BENEFIT_MIN_PROMPT_CHARS} 字符）')
    if expected_calls >= BENEFIT_MIN_EXPECTED_CALLS:
        signals.append(f'多轮预期调用（{expected_calls} ≥ {BENEFIT_MIN_EXPECTED_CALLS}）')
    if complexity in BENEFIT_COMPLEXITY:
        signals.append('高复杂度路由')
    if category in BENEFIT_CATEGORIES:
        signals.append('检索/审计类任务')

    reasons = []
    if forbidden:
        reasons.append('任务文本明确禁止创建子代理，用户指令优先')
    elif not signals:
        reasons.append('任务没有可核实的预期委派收益（简单/短任务默认不开子代理）')
    return {'eligible': bool(signals) and not forbidden, 'signals': signals,
            'forbidden': bool(forbidden), 'requested': bool(requested),
            'thresholds': {'files': BENEFIT_MIN_FILES, 'prompt_chars': BENEFIT_MIN_PROMPT_CHARS,
                           'expected_calls': BENEFIT_MIN_EXPECTED_CALLS},
            'reasons': reasons}



class ProbeDenied(PermissionError):
    """Raised when the controlled probe's preconditions are not all satisfied."""


def _now():
    return datetime.now(timezone.utc)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------ preconditions

def first_tranche_verified(root=None, runs=None):
    """Verify the first real two-stage handover the probe depends on.

    A record is only trusted when the referenced task directory exists, its
    independent review is ``accepted`` for the same task/stage and the review's
    real DSH session matches the recorded one.  A hand-written flag alone is not
    evidence.
    """
    root = Path(root) if root else HERE
    runs = Path(runs) if runs else registry.RUNS
    result = {'verified': False, 'record': str(root / 'tasks/stage-smoke-record.json'),
              'checks': [], 'reasons': []}
    data = _read_json(root / 'tasks/stage-smoke-record.json')
    if not isinstance(data, dict):
        result['reasons'].append('首批本地两阶段交接记录无法读取')
        return result
    pairs = (('first', 'first_verified', 'first_task', 'first_session', 'first_stage'),
             ('second', 'second_verified', 'second_task', 'second_session', 'second_stage'))
    for label, verified_key, task_key, session_key, stage_key in pairs:
        if data.get(verified_key) is not True:
            result['reasons'].append(f'{label}: 记录未标记独立验收通过')
            continue
        task_id = data.get(task_key)
        session_id = data.get(session_key)
        stage_id = data.get(stage_key)
        run = runs / str(task_id or '')
        review = _read_json(run / 'review.json')
        if not isinstance(review, dict):
            result['reasons'].append(f'{label}: 找不到任务 {task_id} 的真实验收记录')
            continue
        if review.get('state') != 'accepted':
            result['reasons'].append(f'{label}: 任务 {task_id} 未通过独立验收')
            continue
        executor = review.get('executor') if isinstance(review.get('executor'), dict) else {}
        if executor.get('dsh_session_id') != session_id:
            result['reasons'].append(f'{label}: 会话身份与验收记录不一致')
            continue
        if stage_id and review.get('stage_id') != stage_id:
            result['reasons'].append(f'{label}: 阶段身份与验收记录不一致')
            continue
        if review.get('task_id') != task_id:
            result['reasons'].append(f'{label}: 验收记录不属于该任务')
            continue
        result['checks'].append({'stage': label, 'task_id': task_id, 'session_id': session_id,
                                 'stage_id': stage_id, 'review_state': review.get('state')})
    result['verified'] = bool(result['checks']) and not result['reasons'] and len(result['checks']) == 2
    return result


def probe_preconditions(model, settings=None, explicit=False, root=None, runs=None,
                        mode=None, run=None, target=None):
    """Every precondition the controlled probe must satisfy, with reasons.

    ``mode`` is the task mode the probe will actually measure.  For the full-
    access ``edit`` mode the run/workspace/target binding is verified here too,
    so an out-of-scope file can never be measured.  A read-only probe request
    (``mode`` omitted or ``chat``) keeps the original behaviour.
    """
    root = Path(root) if root else HERE
    settings = settings if isinstance(settings, dict) else workflow_settings.snapshot()
    model = model if isinstance(model, dict) else {}
    checks = {}
    reasons = []

    checks['explicit'] = bool(explicit)
    if not explicit:
        reasons.append('必须以显式 --probe 调用，普通执行不会进入探测路径')

    checks['mode'] = mode
    if mode is not None and mode not in model_capabilities.MODES:
        reasons.append(f'未知任务模式 {mode}，未验证模式默认禁用')
    if run is not None and mode is not None:
        # The probe context names both a mode and the concrete run it measures;
        # the run's own record must agree, so a chat context can never be pointed
        # at an edit run (or vice versa).
        declared = (registry.read_task(run) or {}).get('kind') if Path(run).is_dir() else None
        checks['run_mode_matches'] = declared == mode
        if declared != mode:
            reasons.append(f'探测 run 声明的 kind={declared} 与请求模式 {mode} 不一致')
    if mode == EDIT_PROBE_KIND:
        scope = edit_probe_scope(run, target, runs=runs)
        checks['scope'] = scope['checks']
        reasons.extend(scope['reasons'])

    local = model.get('provider') == 'local' or model.get('id') == 'home'
    checks['non_local'] = not local
    if local:
        reasons.append('禁止使用 home/本地模型做父代理')
    checks['model_registered'] = bool(model.get('id') and model.get('model'))
    if not checks['model_registered']:
        reasons.append('模型未在注册表中')
    checks['model_enabled'] = bool(model.get('enabled', True))
    if not checks['model_enabled']:
        reasons.append('模型已停用')
    checks['route_endpoint'] = bool(str(model.get('api_base') or '').strip())
    if not checks['route_endpoint']:
        reasons.append('模型缺少可核对的上游 endpoint')

    checks['paid_allowed'] = bool(settings.get('paid_allowed'))
    if not checks['paid_allowed']:
        reasons.append('工作台未开启付费授权')

    paid = bool(model_router.is_paid(model))
    checks['billable_rates'] = (not paid) or bool(model_router.rates_for(model))
    if paid and not checks['billable_rates']:
        reasons.append('付费模型缺少可核对单价')

    tranche = first_tranche_verified(root=root, runs=runs)
    checks['first_tranche_verified'] = tranche['verified']
    if not tranche['verified']:
        reasons.append('首批真实本地两阶段交接尚未由主脑独立验收：' + '；'.join(tranche['reasons'] or ['未核对']))

    # The probe must never need (or create) a stored 'supported' claim to start.
    block = model.get('subagent') if isinstance(model.get('subagent'), dict) else {}
    rows = block.get('evidence') if isinstance(block.get('evidence'), list) else []
    checks['requires_stored_support'] = False
    checks['stored_policy'] = block.get('policy') or 'unset'
    checks['stored_supported_rows'] = sum(1 for row in rows
                                          if isinstance(row, dict) and row.get('state') == 'supported'
                                          and row.get('measured') is True)

    return {'schema': SCHEMA, 'allowed': not reasons, 'checks': checks, 'reasons': reasons,
            'first_tranche': tranche, 'model_id': model.get('id'), 'paid': paid}


def authorize_probe(model, explicit=False, settings=None, root=None, runs=None, mode=None):
    """Return the probe plan or raise :class:`ProbeDenied`.

    The probe entry points set an in-process context naming the mode and (for the
    edit probe) the concrete run/target being measured.  Without that context an
    explicit probe call is refused, so a task that merely carries the
    ``subagent_probe`` marker cannot mount a full-access child behind this gate.
    """
    context = load_probe_context(runs)
    if mode is None and context:
        mode = context.get('mode')
    if explicit and context is None and mode is None:
        raise ProbeDenied('受控探测必须以 --probe/--edit-probe 入口在显式探测上下文中调用')
    pre = probe_preconditions(model, settings=settings, explicit=explicit, root=root, runs=runs,
                              mode=mode, run=(context or {}).get('run'),
                              target=(context or {}).get('target'))
    if not pre['allowed']:
        raise ProbeDenied('受控探测未获许可：' + '；'.join(pre['reasons']))
    return {'schema': SCHEMA, 'kind': 'controlled-subagent-probe',
            'mode': mode or model_capabilities.MODE_CHAT,
            'model_id': model.get('id'), 'model': model.get('model'), 'endpoint': model.get('api_base'),
            'agent_preset': model.get('agent_preset') or model_capabilities.DEFAULT_PRESET,
            'max_depth': MAX_DEPTH, 'provider': 'spawn', 'preconditions': pre,
            'writes_models_json': False, 'child_count_per_round': 1}


# -------------------------------------------------------------- child config

def child_config(model):
    """The controlled one-level child tool configuration.

    ``agentOptions`` pins the child to the parent's ``desk-worker`` route and
    model.  Background execution is disabled so a parent cannot leave an
    unobserved child behind, and the depth cap is absolute.
    """
    model = model if isinstance(model, dict) else {}
    return {
        'provider': 'spawn',
        'toolName': 'subagent',
        'modelSelectionSettings': False,
        'enableRunInBackground': False,
        'backgroundMode': 'one-shot',
        'agentOptions': {'provider': 'desk-worker', 'model': model.get('model')},
        'maxDepth': MAX_DEPTH,
    }


# ------------------------------------------------------------------ ledger

def ledger_path(run):
    return Path(run) / LEDGER_NAME


def read_ledger(run):
    path = ledger_path(run)
    rows = []
    if path.is_file():
        try:
            for line in path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        except OSError:
            rows = []
    return rows


def lifecycle(rows):
    """Pair lifecycle edges into child lineage; expose orphans and mismatches."""
    starts = {}
    children = []
    unmatched = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        kind = row.get('type')
        run_id = row.get('run_id')
        if kind == 'start':
            starts[run_id] = row
        elif kind == 'end':
            start = starts.pop(run_id, None)
            if start is None:
                unmatched.append(row.get('run_id'))
                continue
            children.append({
                'run_id': run_id,
                'provider': start.get('provider') or row.get('provider'),
                'child_id': start.get('child_id') or row.get('child_id'),
                'child_model': start.get('child_model'),
                'child_session': start.get('child_session'),
                'local': bool(start.get('local')),
                'started_at': start.get('at'),
                'ended_at': row.get('at'),
                'stop_reason': row.get('stop_reason'),
                'output_chars': row.get('output_chars'),
                'output_preview': row.get('output_preview'),
            })
    return {'schema': SCHEMA, 'source': LEDGER_SOURCE, 'children': children,
            'orphan_starts': [row.get('run_id') for row in starts.values()],
            'unmatched_ends': unmatched}


def subagent_events(rows, limit=200):
    """Public, redacted view of the ledger rows for the E2 event interface."""
    out = []
    for row in (rows or [])[-limit:]:
        if not isinstance(row, dict):
            continue
        out.append(model_capabilities.public_view({
            'schema': row.get('schema'), 'source': row.get('source'), 'type': row.get('type'),
            'at': row.get('at'), 'provider': row.get('provider'), 'child_id': row.get('child_id'),
            'run_id': row.get('run_id'), 'local': row.get('local'),
            'child_model': row.get('child_model'), 'stop_reason': row.get('stop_reason'),
            'output_chars': row.get('output_chars'), 'output_preview': row.get('output_preview'),
        }))
    return out


# ------------------------------------------------------------------ billing

def aggregate_usage(run, lineage=None):
    """Provable aggregate gateway usage plus an explicit non-authoritative window.

    The relay settles by real upstream usage at the request-time rate and its
    ``settle`` is idempotent per ``request_id``, so summing distinct records
    cannot double-count.  DSH does not expose a per-child token field, so no
    child invoice is fabricated: requests whose relay timestamp falls inside a
    child lifecycle window are reported separately and labelled non-authoritative.
    """
    rows = executor_adapter.task_cost_rows(run) or []
    totals = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
              'settled_cny': None, 'settled_calls': 0, 'unknown_calls': 0}
    settled = 0.0
    has_settled = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        totals['requests'] += 1
        usage = row.get('usage') if isinstance(row.get('usage'), dict) else {}
        for source, target in (('prompt_tokens', 'prompt_tokens'), ('completion_tokens', 'completion_tokens'),
                               ('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
                               ('total_tokens', 'total_tokens')):
            value = usage.get(source)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                totals[target] += int(value)
        if row.get('state') == 'settled' and row.get('cost_cny') is not None:
            try:
                settled += float(row['cost_cny'])
                totals['settled_calls'] += 1
                has_settled = True
            except (TypeError, ValueError):
                totals['unknown_calls'] += 1
        elif row.get('billable', True):
            totals['unknown_calls'] += 1
    if totals['requests'] and not totals['total_tokens']:
        totals['total_tokens'] = totals['prompt_tokens'] + totals['completion_tokens']
    if has_settled:
        totals['settled_cny'] = round(settled, 8)

    windows = []
    children = (lineage or {}).get('children') or []
    for child in children:
        start = _parse_time(child.get('started_at'))
        end = _parse_time(child.get('ended_at'))
        if start and end and end >= start:
            windows.append((start, end))
    bracketed = 0
    for row in rows:
        moment = _parse_time(row.get('at')) if isinstance(row, dict) else None
        if moment and any(start <= moment <= end for start, end in windows):
            bracketed += 1
    return model_capabilities.public_view({
        'schema': SCHEMA,
        'source': 'budget-relay/task.json cost_requests (upstream usage, request-time rates)',
        'authoritative': 'aggregate',
        'aggregate': totals,
        'recorded_models': sorted({row.get('model') for row in rows
                                   if isinstance(row, dict) and row.get('model')}),
        'per_child': 'unavailable',
        'reason': 'DSH exposes no per-child token field and the gateway record carries no child id, '
                  'so only the aggregate is provable; no child invoice is fabricated',
        'child_lifecycle_window_requests': bracketed,
        'attribution': 'lifecycle-window bracket (non-authoritative correlation, not a bill)',
        'no_double_count': 'distinct request_id records; relay settle() is idempotent per request_id',
        'acp_used_size_not_used': True,
    })


# -------------------------------------------------------------------- probe

def new_probe_run(kind=PROBE_KIND, prompt=PROBE_PROMPT, model_id=None):
    task_id = time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    run = registry.RUNS / task_id
    run.mkdir(parents=True, exist_ok=True)
    (run / 'prompt.txt').write_text(prompt, encoding='utf-8')
    registry.patch_task(run, id=task_id, status='queued', kind=kind, engine='dsh', source='subagent-probe',
                        model_id=model_id, project='', files=[], timeout=PROBE_TIMEOUT, test_command='',
                        route={'mode': 'manual', 'reason': '受控子代理探测'}, title='受控子代理探测',
                        subagent_probe=True, created_at=registry.now())
    return run, task_id


def _existing_rounds(path):
    data = _read_json(path)
    if not isinstance(data, dict):
        return []
    if isinstance(data.get('rounds'), list):
        return [row for row in data['rounds'] if isinstance(row, dict)]
    return [data] if data.get('measured') is not None else []


def _round_key(evidence):
    task = evidence.get('task') if isinstance(evidence.get('task'), dict) else {}
    return str(task.get('task_id') or evidence.get('task_id') or '')


def evidence_totals(rounds):
    """Sum the provable settled spend and keep every unknown call visible.

    ``settled_complete`` is only true when no recorded request is unknown or
    pending; an unknown call is never counted as settled, so the totals cannot be
    presented as a fully closed bill.
    """
    settled = 0.0
    has_settled = False
    unknown = 0
    requests = 0
    for row in rounds if isinstance(rounds, list) else []:
        aggregate = ((row.get('gateway') or {}).get('aggregate') or {}) if isinstance(row, dict) else {}
        value = aggregate.get('settled_cny')
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            settled += float(value)
            has_settled = True
        try:
            unknown += int(aggregate.get('unknown_calls') or 0)
            requests += int(aggregate.get('requests') or 0)
        except (TypeError, ValueError):
            pass
    return {
        'rounds': len(rounds or []),
        'requests': requests,
        'settled_cny': round(settled, 8) if has_settled else None,
        'unknown_calls': unknown,
        'settled_complete': unknown == 0,
        'note': ('全部记录均已 settled' if unknown == 0 else
                 '仍有 unknown/pending 调用未结清，不能声称总账单已完全结清'),
    }


def merge_evidence(path, rounds):
    """Insert or refresh rounds keyed by real task id; never drop an old one.

    A later run with a new task id is appended; re-generating the same task's
    round refreshes it in place.  ``round_totals`` keeps the aggregate spend and
    the unknown-call count across every preserved round.
    """
    path = Path(path)
    merged = {}
    order = []
    for row in list(_existing_rounds(path)) + list(rounds or []):
        if not isinstance(row, dict):
            continue
        key = _round_key(row)
        if not key:
            continue
        if key not in merged:
            order.append(key)
        merged[key] = row
    order.sort()
    ordered = [merged[key] for key in order]
    latest = ordered[-1] if ordered else {}
    payload = {**latest, 'rounds': ordered, 'round_count': len(ordered),
               'round_totals': evidence_totals(ordered)}
    registry.write_json(path, payload)
    return payload


def write_evidence(path, evidence):
    """Append one measured round; earlier rounds are preserved, not rewritten."""
    return merge_evidence(path, [evidence])


def reconstruct_round(run, model=None, model_id=None):
    """Rebuild one measured round from a run's recorded artifacts.

    Used only to restore a round that a real run produced before an append path
    existed.  It dispatches nothing and reads only files the run already wrote;
    the round is explicitly marked as a reconstruction and keeps its recorded
    preset revision, cost/unknown counts and provenance instead of being
    re-measured against the current build.
    """
    run = Path(run)
    state = registry.read_task(run) or {}
    run_id = state.get('id') or run.name
    if not isinstance(model, dict):
        try:
            model = registry.get_model(model_id or state.get('model_id'), require_enabled=False) or {}
        except Exception:
            model = {}
    rows = read_ledger(run)
    lineage = lifecycle(rows)
    usage = aggregate_usage(run, lineage)
    proof = _read_json(run / 'preset-proof.json') or {}
    children = lineage.get('children') or []
    session_id = state.get('dsh_session_id')
    preset = state.get('agent_preset') or model.get('agent_preset') or model_capabilities.DEFAULT_PRESET
    revision = state.get('preset_revision') or proof.get('revision')
    sections = child_sections(children, state, session_id, lineage.get('orphan_starts'))
    return model_capabilities.public_view({
        'schema': SCHEMA,
        'kind': EVIDENCE_KIND,
        'generated_at': _now().isoformat(),
        'measured': bool(state.get('status') in {'completed', 'cancelled'} and children),
        'read_only_probe': True,
        'writes_project_files': list(state.get('changed_files') or []),
        'writes_models_json': False,
        'cancel_check': bool(state.get('subagent_cancel_check')),
        'probe': {'requested': True, 'executed': bool(children),
                  'exit_code': 0 if state.get('status') == 'completed' else 1,
                  'status': state.get('status'), 'plan': None},
        'identity': {
            'executor': 'dsh',
            'executor_version': state.get('executor_version') or model.get('executor_version')
            or executor_adapter.installed_executor_version(),
            'model_id': state.get('model_id') or model.get('id'),
            'model': model.get('model'),
            'endpoint': model.get('api_base'),
            'agent_preset': preset,
            'preset_revision': revision,
            'capability_identity': model_capabilities.evidence_identity(
                executor='dsh', model=model.get('model') or '', preset=preset, preset_revision=revision,
                endpoint=model.get('api_base'), version=state.get('executor_version')),
        },
        'task': {'task_id': run_id, 'run': str(run), 'parent_session_id': session_id,
                 'acpx_record_id': state.get('acpx_record_id'), 'kind': state.get('kind'),
                 'status': state.get('status'), 'changed_files': state.get('changed_files') or []},
        'ledger': {'path': str(ledger_path(run)), 'source': LEDGER_SOURCE,
                   'raw_events': subagent_events(rows), 'lineage': lineage},
        'child_identity': {
            'count': len(children),
            'child_ids': sections['child_ids'],
            'distinct_from_parent_session': sections['distinct_from_parent_session'],
            'providers': sections['providers'],
            'child_models': sections['child_models'],
            'child_model_matches_parent': sections['child_model_matches_parent'],
            'paired_lifecycle': sections['paired_lifecycle'],
        },
        'child_output': {
            'from_executor_tool_result': sections['from_executor_tool_result'],
            'from_lifecycle_last_message': sections['from_lifecycle_last_message'],
            'parent_answer_tail': sections['parent_answer_tail'],
            'note': 'ACP 工具完成事件是子代理输出的执行器证据；生命周期边的 lastAssistantMessage 可能为空',
        },
        'read_only': read_only_block(),
        'gateway': usage,
        'preset_proof': preset_proof_summary(run, state),
        'models_json_untouched': True,
        'policy_note': '探测不写 models.json；是否把该模型策略设为 allow 由主脑独立检查后决定',
        'reconstruction': {
            'reconstructed': True,
            'dispatched': False,
            'reason': '该轮早于证据追加路径，按运行目录中已存在的真实记录重建，不重新派发、不重测',
            'artifacts': [str(run / 'task.json'), str(run / 'preset-proof.json'), str(ledger_path(run))],
        },
        'limits': evidence_limits(),
        'reproduce_commands': reproduce_commands(model.get('id'), run),
    })


def reconstruct_edit_round(run, model=None, model_id=None):
    """Rebuild a *failed* edit probe attempt from its raw artifacts.

    Used to preserve the deny and real spend of an earlier attempt next to the
    successful measurement.  It dispatches nothing, measures nothing new and
    marks itself ``measured: false`` with ``dispatched: false``.
    """
    run = Path(run)
    state = registry.read_task(run) or {}
    if not isinstance(model, dict):
        try:
            model = registry.get_model(model_id or state.get('model_id'), require_enabled=False) or {}
        except Exception:
            model = {}
    relative_target = state.get('probe_target') or next(iter(state.get('files') or []), None)
    nonce = state.get('probe_nonce')
    observation = (write_check(run, relative_target, nonce, state)
                   if relative_target and nonce else {'exists': False})
    rows = read_ledger(run)
    lineage = lifecycle(rows)
    capability = state.get('subagent_capability') if isinstance(state.get('subagent_capability'), dict) else {}
    return model_capabilities.public_view({
        'schema': SCHEMA,
        'kind': EDIT_PROBE_EVIDENCE_KIND,
        'generated_at': _now().isoformat(),
        'measured': False,
        'read_only_probe': False,
        'mode': EDIT_PROBE_KIND,
        'sandbox_mode': state.get('sandbox_mode') or EDIT_PROBE_SANDBOX,
        'writes_project_files': [],
        'writes_models_json': False,
        'stored_support_required': False,
        'probe': {'requested': True, 'executed': bool(lineage.get('children')),
                  'exit_code': state.get('exit_code'), 'status': state.get('status'), 'plan': None,
                  'deny': capability.get('blocked') or state.get('error'),
                  'unmounted_reason': '本轮 parent 会话未挂载 subagent 工具，未产生真实 child'},
        'identity': {
            'executor': 'dsh', 'executor_version': state.get('executor_version'),
            'model_id': state.get('model_id') or model.get('id'), 'model': model.get('model'),
            'endpoint': model.get('api_base'),
            'agent_preset': state.get('agent_preset') or model.get('agent_preset'),
            'preset_revision': state.get('preset_revision'),
        },
        'task': {'task_id': state.get('id') or run.name, 'run': str(run),
                 'parent_session_id': state.get('dsh_session_id'), 'kind': state.get('kind'),
                 'status': state.get('status'), 'project': state.get('project'),
                 'file_scope': [relative_target] if relative_target else [],
                 'changed_files': state.get('changed_files') or []},
        'write_check': observation,
        'child_identity': {'count': 0, 'child_ids': [], 'distinct_from_parent_session': False,
                           'paired_lifecycle': False},
        'gateway': aggregate_usage(run, lineage),
        'preset_proof': preset_proof_summary(run, state),
        'models_json_untouched': True,
        'policy_note': '失败尝试只保留原始记录，不构成任何模型的 supported 证据',
        'limits': edit_probe_limits(),
        'reconstruction': {
            'reconstructed': True, 'dispatched': False,
            'reason': '该轮为 edit 探测的失败/未挂载尝试，按运行目录原始记录重建，不重新派发、不重测',
            'artifacts': [str(run / 'task.json'), str(ledger_path(run))],
        },
        'reproduce_commands': reproduce_commands(model.get('id'), run, edit=True),
    })


def run_probe(model_id, evidence_path=None, cancel_check=False, root=None, runs=None):
    """Execute exactly one minimal read-only parent -> child round.

    The probe is refused (``ProbeDenied``) unless every precondition passes; it
    never sets a fake ``supported`` row and never writes ``models.json``.
    """
    model = registry.get_model(model_id, require_enabled=True)
    _set_active_probe({'mode': model_capabilities.MODE_CHAT, 'run': None, 'target': None,
                       'kind': 'cancel-check' if cancel_check else 'chat-probe'})
    _persist_probe_context(_ACTIVE_PROBE, runs)
    try:
        plan = authorize_probe(model, explicit=True, root=root, runs=runs)
        settings = workflow_settings.snapshot()
        import dsh_worker  # lazy: dsh_worker imports this module

        prompt = CANCEL_PROMPT if cancel_check else PROBE_PROMPT
        run, task_id = new_probe_run(prompt=prompt, model_id=model_id)
        _set_active_probe({'mode': model_capabilities.MODE_CHAT, 'run': str(run), 'target': None,
                           'kind': 'cancel-check' if cancel_check else 'chat-probe'})
        _persist_probe_context(_ACTIVE_PROBE, runs)
        state = registry.read_task(run)
        if cancel_check:
            state = registry.patch_task(run, subagent_cancel_check=True)
            state = registry.read_task(run)

        if cancel_check:
            _arm_cancel_on_child_start(run)
        try:
            exit_code = dsh_worker.run_task(run, state, model)
        except Exception as error:  # record the failure, never retry a paid dispatch
            exit_code = 1
            registry.patch_task(run, error=str(error)[:500])

        rows = read_ledger(run)
        lineage = lifecycle(rows)
        usage = aggregate_usage(run, lineage)
        final = registry.read_task(run)
        evidence = build_evidence(model, plan, run, task_id, final, rows, lineage, usage, settings,
                                  cancel_check=cancel_check, exit_code=exit_code)
        if evidence_path:
            write_evidence(evidence_path, evidence)
            registry.patch_task(run, subagent_probe_evidence=str(evidence_path))
        return evidence
    finally:
        _clear_active_probe()
        _clear_persisted_probe_context(runs)


def _arm_cancel_on_child_start(run, timeout=90, poll=.25):
    """Cancel the parent run as soon as a child start edge appears.

    The cancel uses the same ``cancelled.flag`` the runtime already polls; it is
    a real cancellation, not a second dispatch.
    """
    import threading

    def watch():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(row.get('type') == 'start' for row in read_ledger(run)):
                (run / 'cancelled.flag').write_text(
                    json.dumps({'reason': '受控探测：子代理启动后取消父任务', 'by': 'subagent_runtime',
                                'redispatched': False}, ensure_ascii=False), encoding='utf-8')
                return
            if (run / 'result.json').is_file():
                return
            time.sleep(poll)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread


def child_sections(children, final, parent_session=None, orphan_starts=()):
    """The shared child-identity/output block for live and rebuilt evidence."""
    children = children if isinstance(children, list) else []
    child_ids = [child.get('child_id') for child in children if child.get('child_id')]
    child_models = [child.get('child_model') for child in children if child.get('child_model')]
    return model_capabilities.public_view({
        'child_ids': child_ids,
        'distinct_from_parent_session': bool(child_ids) and all(
            child_id != parent_session for child_id in child_ids),
        'providers': sorted({child.get('provider') for child in children if child.get('provider')}),
        'child_models': child_models,
        'child_model_matches_parent': (all(name == final.get('model') for name in child_models)
                                       if child_models and final.get('model') else None),
        'paired_lifecycle': bool(children) and not list(orphan_starts or ()),
        'from_executor_tool_result': final.get('subagent_tool_outputs') or [],
        'from_lifecycle_last_message': [child.get('output_preview') for child in children
                                        if child.get('output_preview') is not None],
        'parent_answer_tail': str(final.get('answer') or '')[-200:],
    })


def read_only_block():
    """Honest mode/permission source for the accepted read-only probe."""
    return {
        'probe': True,
        'supported_modes': list(model_capabilities.READ_ONLY_MODES),
        'source': '受控探测的父任务是 read-only/chat（sandbox=read-only），因此只实测了 chat 模式',
        'unverified_modes': [mode for mode in model_capabilities.MODES
                             if mode not in model_capabilities.READ_ONLY_MODES],
        'edit_note': '完全权限 edit 模式下的子代理写入边界未实测；不得据此宣称文件硬隔离',
    }


def preset_proof_summary(run, final):
    """Compare the recorded preset proof session with the run's parent session."""
    proof = _read_json(Path(run) / 'preset-proof.json') or {}
    parent = (final or {}).get('dsh_session_id')
    recorded = proof.get('session_id')
    if not recorded:
        matched = 'missing'
    elif recorded == parent:
        matched = 'parent'
    else:
        matched = 'child-overwrite-observed'
    return {'recorded_session_id': recorded, 'expected_parent_session': parent,
            'recorded_revision': proof.get('revision'), 'matched': matched,
            'note': 'write-once bridge：只有首个 awaited 父 agent 写入的 proof 才算数'
                    if matched == 'parent' else
                    '记录的 proof 不是父会话写入；旧 bridge 曾让 child 覆写，按当时记录原样保留'
                    if matched == 'child-overwrite-observed' else '没有可核对的 proof 会话身份'}


def evidence_limits():
    return [
        'DSH 生命周期边是观察接口，不含子代理 token 字段，故只能给出可证实的聚合账单',
        '生命周期窗口内的网关请求只是非权威相关，不当作子账单',
        'scope 是工作约束而非 OS 沙箱；完全权限时子代理可能继承写入能力',
        '只读探测只实测 chat 模式；edit 完全权限模式默认禁用，未验证边界不得当作已实测',
    ]


def build_evidence(model, plan, run, task_id, final, rows, lineage, usage, settings,
                   cancel_check=False, exit_code=None):
    """Assemble the measurement record.  No secret, key or token is included."""
    children = lineage.get('children') or []
    session_id = final.get('dsh_session_id')
    measured = bool(final.get('status') in {'completed', 'cancelled'} and children)
    sections = child_sections(children, {**final, 'model': model.get('model')}, session_id,
                              lineage.get('orphan_starts'))
    child_ids = sections['child_ids']
    return model_capabilities.public_view({
        'schema': SCHEMA,
        'kind': EVIDENCE_KIND,
        'generated_at': _now().isoformat(),
        'measured': measured,
        'read_only_probe': True,
        'writes_project_files': list(final.get('changed_files') or []),
        'writes_models_json': False,
        'cancel_check': bool(cancel_check),
        'probe': {'requested': True, 'executed': bool(children), 'exit_code': exit_code,
                  'status': final.get('status'), 'plan': plan},
        'identity': {
            'executor': 'dsh',
            'executor_version': executor_adapter.installed_executor_version(),
            'model_id': model.get('id'),
            'model': model.get('model'),
            'endpoint': model.get('api_base'),
            'agent_preset': model.get('agent_preset') or model_capabilities.DEFAULT_PRESET,
            'preset_revision': executor_adapter.preset_content_revision(
                model.get('agent_preset') or model_capabilities.DEFAULT_PRESET),
            'capability_identity': executor_adapter.model_identity(model, preset={'revision': None}),
        },
        'task': {'task_id': task_id, 'run': str(run), 'parent_session_id': session_id,
                 'acpx_record_id': final.get('acpx_record_id'), 'kind': final.get('kind'),
                 'status': final.get('status'), 'changed_files': final.get('changed_files') or []},
        'ledger': {'path': str(ledger_path(run)), 'source': LEDGER_SOURCE,
                   'raw_events': subagent_events(rows), 'lineage': lineage},
        'child_identity': {
            'count': len(children),
            'child_ids': child_ids,
            'distinct_from_parent_session': sections['distinct_from_parent_session'],
            'providers': sections['providers'],
            'child_models': sections['child_models'],
            'child_model_matches_parent': sections['child_model_matches_parent'],
            'paired_lifecycle': sections['paired_lifecycle'],
        },
        'child_output': {
            'from_executor_tool_result': sections['from_executor_tool_result'],
            'from_lifecycle_last_message': sections['from_lifecycle_last_message'],
            'parent_answer_tail': sections['parent_answer_tail'],
            'note': 'ACP 工具完成事件是子代理输出的执行器证据；生命周期边的 lastAssistantMessage 可能为空',
        },
        'read_only': read_only_block(),
        'preset_proof': preset_proof_summary(run, final),
        'gateway': usage,
        'models_json_untouched': True,
        'policy_note': '探测不写 models.json；是否把该模型策略设为 allow 由主脑独立检查后决定',
        'limits': evidence_limits(),
        'reproduce_commands': reproduce_commands(model.get('id'), run),
    })


def reproduce_commands(model_id, run=None, edit=False):
    python = sys.executable
    commands = [
        f'"{python}" -W ignore::ResourceWarning -m unittest discover -s tests -t tests -p "test_subagent_runtime.py"',
        f'"{python}" -W ignore::ResourceWarning -m unittest discover -s tests -t tests -p "test_*.py"',
        f'"{python}" subagent_runtime.py --check --model-id {model_id}',
        f'"{python}" subagent_runtime.py --probe --model-id {model_id} --evidence tasks/subagent-probe-evidence.json',
    ]
    if edit:
        commands.append(f'"{python}" subagent_runtime.py --edit-check --model-id {model_id}')
        commands.append(f'"{python}" subagent_runtime.py --edit-probe --model-id {model_id} '
                        '--evidence tasks/edit-subagent-evidence.json')
    if run is not None:
        commands.append(f'read {Path(run) / LEDGER_NAME} and {Path(run) / "task.json"} (cost_requests) for the raw record')
    return commands


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='受控 DSH 原生子代理运行时（闸门 / 探测 / 生命周期）')
    parser.add_argument('--model-id')
    parser.add_argument('--check', action='store_true', help='只检查前置条件，不调用模型')
    parser.add_argument('--probe', action='store_true', help='执行一次最小非本地只读父子探测')
    parser.add_argument('--cancel-check', action='store_true', help='执行一次子代理启动后取消的探测')
    parser.add_argument('--edit-check', action='store_true', help='只检查 edit 探测前置条件，不调用模型')
    parser.add_argument('--edit-probe', action='store_true',
                        help='执行一次显式受控 edit（danger-full-access）单文件写入探测')
    parser.add_argument('--evidence', type=Path, help='把实测证据写到该 UTF-8 JSON 路径')
    parser.add_argument('--ledger', help='只读取并打印某次运行的生命周期账本')
    parser.add_argument('--reconstruct-run', action='append', default=[],
                        help='从已存在的真实运行目录重建证据轮次（只读，不派发，不新增收费）')
    parser.add_argument('--reconstruct-edit-run', action='append', default=[],
                        help='从已存在的失败 edit 探测运行目录重建原始失败轮次（只读，不派发）')
    args = parser.parse_args()
    if args.ledger:
        rows = read_ledger(Path(args.ledger).parent if Path(args.ledger).is_file() else Path(args.ledger))
        print(json.dumps({'events': subagent_events(rows), 'lineage': lifecycle(rows)}, ensure_ascii=False, indent=2))
        return 0
    if args.reconstruct_run:
        rounds = [reconstruct_round(Path(run), model_id=args.model_id) for run in args.reconstruct_run]
        target = args.evidence or EVIDENCE_PATH
        saved = merge_evidence(target, rounds)
        print(json.dumps({'reconstructed': [row.get('task', {}).get('task_id') for row in rounds],
                          'round_count': saved.get('round_count'),
                          'round_totals': saved.get('round_totals')}, ensure_ascii=False, indent=2))
        return 0
    if args.reconstruct_edit_run:
        rounds = [reconstruct_edit_round(Path(run), model_id=args.model_id)
                  for run in args.reconstruct_edit_run]
        target = args.evidence or EDIT_PROBE_EVIDENCE_PATH
        saved = merge_evidence(target, rounds)
        print(json.dumps({'reconstructed': [row.get('task', {}).get('task_id') for row in rounds],
                          'round_count': saved.get('round_count'),
                          'round_totals': saved.get('round_totals')}, ensure_ascii=False, indent=2))
        return 0
    model = registry.get_model(args.model_id, require_enabled=False)
    edit_mode = bool(args.edit_probe or args.edit_check)
    # ``dispatch`` decides whether a real run is requested; ``intent`` also
    # covers the dry checks, which are explicit probe calls and must be judged.
    dispatch = bool(args.probe or args.cancel_check or args.edit_probe)
    intent = bool(dispatch or args.edit_check)
    if args.check or args.edit_check or not dispatch:
        pre = probe_preconditions(model, explicit=intent,
                                  mode=EDIT_PROBE_KIND if edit_mode else None)
        print(json.dumps(pre, ensure_ascii=False, indent=2))
        return 0 if pre['allowed'] or not intent else 1
    try:
        if args.edit_probe:
            evidence = run_edit_probe(args.model_id, evidence_path=args.evidence)
        else:
            evidence = run_probe(args.model_id, evidence_path=args.evidence, cancel_check=args.cancel_check)
    except ProbeDenied as error:
        print(json.dumps({'blocked': True, 'reason': str(error)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({'executed': evidence.get('probe', {}).get('executed'),
                      'mode': evidence.get('mode'), 'sandbox_mode': evidence.get('sandbox_mode'),
                      'task': evidence.get('task'), 'child_identity': evidence.get('child_identity'),
                      'write_check': {key: evidence.get('write_check', {}).get(key) for key in
                                      ('exists', 'sha256', 'content_matches_nonce', 'inside_run_workspace')},
                      'gateway': evidence.get('gateway', {}).get('aggregate')}, ensure_ascii=False, indent=2))
    return 0 if evidence.get('measured') else 1


if __name__ == '__main__':
    raise SystemExit(main())
