"""Execute panel jobs through the installed DSH and the pinned acpx runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import yaml
import model_registry as registry
import workflow_settings
import budget_relay
import context_manager
import model_router
import model_scope
import deepseek_billing
import preset_runtime
import thinking_config
import delegation_policy
import executor_adapter
import model_capabilities
import recovery_retry
import record_protection
import runtime_paths
import subagent_runtime
import task_timing

HERE = Path(__file__).resolve().parent
RUNTIME = HERE / 'agent-runtime'
# The DSH install and Node belong to the user.  They are read from explicit
# environment variables or a verifiable PATH lookup (see runtime_paths.py and
# docs/INSTALL.md); nothing here points at any author's drive, user profile or
# editor cache.  An unconfigured runtime stays ``UNSET`` - a path that can never
# resolve to a file - so availability reports "not ready" instead of guessing.
EXECUTABLE = runtime_paths.dsh_executable() or runtime_paths.UNSET
NODE = runtime_paths.node_executable() or runtime_paths.UNSET
# Post-stream process-exit grace.  The ACP child has already closed both streams
# and returned (or refused) its outcome; a slow teardown must never discard a real
# validated result.  This is not a task deadline: ``task_timing`` owns that, and
# timeout=0 stays unlimited.
EXIT_GRACE_SECONDS = 30


def availability():
    """Read-only DSH install check; a missing piece is reported, never replaced.

    The same environment variables the adapter uses for identity decide what is
    "installed", so the panel, the capability evidence and the worker can never
    look at two different DSH installs.
    """
    cli = runtime_paths.dsh_cli(EXECUTABLE)
    blocked = []
    if not EXECUTABLE.is_file():
        blocked.append(f'DSH 可执行文件未找到（请设置 {runtime_paths.DSH_EXE_ENV} 指向本机 '
                       f'“{runtime_paths.DSH_EXECUTABLE_NAME}”，或把它的目录加入 PATH）')
    if not NODE.is_file():
        blocked.append(f'Node 未找到（请设置 {runtime_paths.NODE_EXE_ENV}，或把 Node 目录加入 PATH）')
    if not runtime_paths.acpx_runtime(RUNTIME).is_file():
        blocked.append(f'本仓库的 acpx {runtime_paths.ACPX_VERSION} 依赖未安装'
                       f'（在 .local-worker{os.sep}agent-runtime 目录执行 pnpm install）')
    return {'ready': not blocked,
            'name': 'DeepSeek Harness', 'transport': 'ACP', 'executable': str(EXECUTABLE),
            'acpx_version': runtime_paths.ACPX_VERSION, 'cli': str(cli), 'node': str(NODE),
            'message': '' if not blocked else '；'.join(blocked) + '；不会改用其他执行器',
            'edit_sandbox_mode': sandbox_mode('edit'), 'chat_sandbox_mode': sandbox_mode('chat')}


def sandbox_mode(kind):
    # Panel edit jobs use the official DSH full-access mode; chat jobs stay read-only.
    return 'danger-full-access' if kind == 'edit' else 'read-only'


def fingerprint(model, kind, project, preset=None):
    data = [model['id'], model['api_base'], model['model'], model.get('max_input_tokens'), model.get('max_output_tokens'), kind, str(project)]
    if kind == 'edit':
        # Older workspace-write edit sessions keep their fingerprint, so they must be recreated rather than silently upgraded.
        data += ['sandbox', sandbox_mode(kind)]
        # A writable session created before the record-write guard existed must
        # not be silently resumed: the guard's content identity is part of the
        # session identity, so an old edit session is refused with "create a new
        # task" instead of running without write protection.
        data += ['record-protection', record_protection.revision()]
    if model.get('thinking_level', 'default') != 'default':
        data += ['thinking', model['thinking_level']]
    if preset:
        data += [preset['id'], preset['revision']]
        if preset.get('subagent_mount'):
            # Only an actually mounted child changes the session identity; a
            # default (unmounted) preset keeps the legacy fingerprint.
            data += ['subagent-mount', True]
    # Delegation permission is part of the session identity: a granted session
    # must never be reused by a model whose grant was revoked (or vice versa).
    # The stored capability block is hashed *by content* (so replacing evidence
    # or editing a grant is visible even when the record count is unchanged) and
    # the installed executor version is included (so an executor upgrade cannot
    # quietly inherit an old granted session).  A default block keeps the legacy
    # fingerprint, so sessions created before this field existed are not
    # silently recreated.
    block = model.get('subagent') if isinstance(model.get('subagent'), dict) else {}
    policy = block.get('policy', 'unset') or 'unset'
    if policy != 'unset' or block.get('evidence'):
        executor_version = (model.get('executor_version')
                            or executor_adapter.installed_executor_version()
                            or model_capabilities.UNVERSIONED)
        data += ['subagent', policy, model_capabilities.capability_digest(block), executor_version]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def build_patch(model, kind, preset=None, subagent=False, child_config=None, record_root=None):
    provider = 'desk-worker'
    rows = [
        {'id': 'llm-pi-ai', 'config': {'providers': {provider: {
            'displayName': model['name'], 'apiKeyEnv': 'DSH_WORKER_API_KEY',
            'api': 'openai-completions', 'baseURL': model['api_base'],
            'defaultContextWindow': model.get('max_input_tokens', 32768),
            'defaultMaxTokens': model.get('max_output_tokens', 4096),
            'retryPolicy': {'mode': 'normal', 'maxRetries': 0},
            'compat': {'maxTokensField': 'max_tokens', 'supportsDeveloperRole': False, 'thinkingFormat': 'deepseek'},
            'models': [{'id': model['model'], 'name': model['model'], 'input': ['text'],
                        'contextWindow': model.get('max_input_tokens', 32768),
                        'maxTokens': model.get('max_output_tokens', 4096), 'reasoningEfforts': False}]}}}},
        {'id': 'acp', 'config': {'provider': provider, 'model': model['model']}},
        {'id': 'agent-default-model', 'config': {'provider': provider, 'model': model['model']}},
        {'id': 'sandbox-policy', 'config': {'mode': sandbox_mode(kind)}},
    ]
    for plugin in ('session-log-deepseek', 'session-telemetry-otel', 'llm-deepseek',
                   'llm-deepseek-account', 'tool-web', 'web-search-deepseek',
                   'tool-working-directory', 'tool-goal', 'tool-ralph'):
        rows.append({'id': plugin, 'disabled': True})
    # Nested delegation stays disabled unless a measured per-model grant (or the
    # explicit controlled probe) exists.  The gate lives in
    # executor_adapter/model_capabilities; this function only renders the
    # decision, so the default (subagent=False) is the fail-closed behaviour
    # every existing caller keeps.  When a child is mounted it is exactly one
    # in-process spawn tool; the fork backend is never enabled because it would
    # seed the child with the parent's finished history.
    config = child_config if isinstance(child_config, dict) else subagent_runtime.child_config(model)
    subagent_rows = [
        {'id': 'tool-subagent', 'disabled': not subagent},
        {'id': 'tool-subagent-fork', 'disabled': True},
        {'id': 'tool-subagent-control', 'disabled': True},
        {'id': 'tool-subagent-list-agents', 'disabled': True},
    ]
    if subagent:
        subagent_rows[0]['config'] = config
    rows += subagent_rows
    rows.append({'id': 'subagent-model-selection-settings',
                 'config': {'enabled': False, 'allowedModels': []}})
    # Add workspaceRoot in run_task after resolving the actual project directory.
    if preset:
        rows += preset_runtime.patch_rows(preset, record_root=record_root)
    return rows


def subagent_decision(model, run=None, preset=None, probe=False, task=None, prompt_text=None):
    """Resolve and record the per-model subagent decision for one task.

    Called before the DSH patch is written, so a model without measured support
    and an explicit allow policy can never receive the subagent plugins.  Three
    separate gates must all pass: capability + user policy for this identity and
    *task mode* (``executor_adapter.subagent_gate``), and a real expected task
    value (``subagent_runtime.task_benefit``), so an ``allow`` grant never turns
    an ordinary simple request into delegation.  The ``probe`` branch is the
    separate, explicit, precondition-checked measurement path; it never writes a
    support claim.
    """
    if task is None and run is not None:
        try:
            task = registry.read_task(run)
        except Exception:
            task = None
    task = task if isinstance(task, dict) else {}
    if prompt_text is None and run is not None:
        try:
            prompt_text = (Path(run) / 'prompt.txt').read_text(encoding='utf-8')
        except OSError:
            prompt_text = ''
    mode = task.get('kind')
    if probe:
        try:
            plan = subagent_runtime.authorize_probe(model, explicit=True)
            # A controlled probe *measures* one target mode; it does not prove it.
            # Only the main brain's independent acceptance turns a round into a
            # support claim, so the provisional decision must not pre-announce
            # ``chat`` (the old hard-coded label) as an already-proven mode.  The
            # real measured mode comes from the probe context, and the formal
            # capability cap is still read from main-accepted evidence.
            target_mode = plan.get('mode') or mode or model_capabilities.MODE_CHAT
            decision = {'decision': 'allow', 'state': 'probe', 'policy': 'probe', 'executor': 'dsh',
                        'identity': executor_adapter.model_identity(model, preset=preset),
                        'stale': False, 'expired': False, 'local': False, 'probe': True,
                        'pending_acceptance': True,
                        'mode': mode, 'proven_modes': [],
                        'probe_target_mode': target_mode,
                        'reasons': ['受控探测：前置条件通过',
                                    '本轮测量目标 ' + str(target_mode)
                                    + '；本轮结果未经主脑独立验收前不算已证实模式'],
                        'plan': plan}
        except subagent_runtime.ProbeDenied as exc:
            decision = model_capabilities.evaluate(model, executor='dsh', mode=mode)
            decision = {**decision, 'decision': 'deny', 'blocked': str(exc)[:400], 'probe': True}
    else:
        benefit = subagent_runtime.task_benefit(task, prompt_text)
        if isinstance(mode, str) and mode not in model_capabilities.MODES:
            decision = model_capabilities.evaluate(model, executor='dsh', mode=mode)
            decision = {**decision, 'decision': 'deny',
                        'blocked': f'未知任务模式 {mode}，未验证模式默认禁用'}
        else:
            try:
                decision = executor_adapter.subagent_gate(model, engine='dsh', preset=preset, mode=mode)
            except executor_adapter.SubagentDenied as exc:
                decision = model_capabilities.evaluate(model, executor='dsh', mode=mode)
                decision = {**decision, 'decision': 'deny', 'blocked': str(exc)[:400]}
        decision = {**decision, 'benefit': benefit}
        if decision.get('decision') == 'allow' and not benefit['eligible']:
            # Capability and policy allow a child, but this task has no proven
            # expected value: keep the plugins disabled instead of spending on it.
            decision = {**decision, 'decision': 'deny',
                        'blocked': '任务无预期委派收益：' + '；'.join(benefit['reasons'])}
    if run is not None:
        try:
            registry.patch_task(run, subagent_capability={k: decision.get(k) for k in
                                ('decision', 'state', 'policy', 'executor', 'identity', 'stale', 'expired',
                                 'local', 'reasons', 'mode', 'proven_modes', 'benefit', 'blocked',
                                 'probe', 'pending_acceptance', 'probe_target_mode')})
        except Exception:  # bookkeeping must never block or change execution
            pass
    return decision


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def tool_update(previous, event, project):
    tool = dict(previous)
    for field in ('status', 'kind'):
        if field in event:
            tool[field] = event[field]
    title = event.get('title')
    # ACP completion updates sometimes contain only the placeholder "tool call".
    if title and (title != 'tool call' or not tool.get('title')):
        tool['title'] = title
    raw_input = event.get('rawInput')
    if isinstance(raw_input, dict) and isinstance(raw_input.get('file_path', raw_input.get('path')), str):
        target = Path(raw_input.get('file_path', raw_input.get('path')))
        if not target.is_absolute():
            target = project / target
        try:
            tool['file'] = str(target.relative_to(project))
        except ValueError:
            tool['file'] = target.name
    return tool


def tool_result_text(event):
    """Extract the tool result text from an ACP tool completion update.

    The ACP profile delivers the subagent result as content blocks; the text is
    the executor-side proof of the child's output, not a guess from the answer.
    """
    content = event.get('content')
    parts = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            inner = block.get('content') if isinstance(block.get('content'), dict) else block
            if isinstance(inner, dict) and isinstance(inner.get('text'), str):
                parts.append(inner['text'])
    if not parts and isinstance(event.get('text'), str):
        text = event['text']
        marker = '(completed):'
        parts.append(text.split(marker, 1)[1].strip() if marker in text else text)
    return '\n'.join(part for part in parts if part)


def record_review(run, completed, test, changed):
    """Keep executor completion separate from acceptance; never accept here.

    Only tasks carrying the outcome-delegation marker get a review record, so
    old or manual history is never retroactively approved.
    """
    try:
        delegation_policy.record_executor(run, {'completed': completed, 'test': test, 'changed_files': changed})
    except Exception as exc:  # review bookkeeping must never break an execution record
        registry.patch_task(run, review_error=str(exc)[:500])


def build_agent_prompt(run, state, index=None, index_path=None, delegate=False):
    """Assemble the agent prompt: controller task text, fixed constraints, index.

    The controller task text is preserved verbatim; ``verify_prompt`` fails
    loudly if assembly dropped or rewrote it, so an acceptance criterion or user
    constraint is never silently truncated. The project index points the agent at
    the original files instead of pasting them into the prompt.

    ``delegate`` must be the *realtime* subagent decision: when a child was
    actually allowed, the blanket "不要创建子代理" clause is replaced so the
    prompt never contradicts the mounted tool configuration.
    """
    run = Path(run)
    base = (run / 'prompt.txt').read_text(encoding='utf-8')
    sections = ['\n\n' + context_manager.execution_constraints(delegate=delegate)]
    sections.append(context_manager.RECORD_PROTECTION_NOTE)
    if state.get('kind') == 'edit':
        sections.append(context_manager.FILE_SCOPE_PREFIX
                        + json.dumps(state.get('files') or [], ensure_ascii=False))
        sections.append(context_manager.FILE_SCOPE_NOTE)
        if state.get('test_command'):
            sections.append(context_manager.TEST_BY_CONTROLLER_NOTE)
        else:
            sections.append(context_manager.SELF_TEST_NOTE)
    else:
        sections.append(context_manager.READONLY_NOTE)
    if isinstance(index, dict):
        sections.extend(context_manager.context_map_sections(index, index_path, run))
    return context_manager.verify_prompt(context_manager.compose_prompt(base, sections), base), base


def record_subagent_ledger(run):
    """Persist paired child lineage and the provable aggregate gateway usage."""
    rows = subagent_runtime.read_ledger(run)
    lineage = subagent_runtime.lifecycle(rows)
    registry.patch_task(run, subagent_events=subagent_runtime.subagent_events(rows),
                        subagent_lineage=lineage,
                        subagent_usage=subagent_runtime.aggregate_usage(run, lineage))
    return lineage


def _prepare_dsh(run, state, model, relay):
    info = availability()
    if not info['ready']:
        raise ValueError(info['message'] or 'DSH 或 ACP 客户端未就绪，请检查执行器安装路径')
    key = model.get('api_key', '')
    redact = lambda text: (str(text).replace(key, '[已隐藏]') if key else str(text)).replace(relay.token, '[已隐藏]')
    project = Path(state['project']) if state['project'] else run / 'workspace'
    project.mkdir(parents=True, exist_ok=True)
    if state['kind'] == 'edit':
        # The workbench record directory is never a writable dispatch target: a
        # dispatch that names it is refused here, before any patch or session.
        record_protection.validate_files(state.get('files') or [], project)
    files = [(project / name).resolve() for name in state.get('files', [])]
    before = {str(p.relative_to(project)): digest(p) for p in files}
    session_key = state.get('dsh_session_key') or 'desk-' + state['id']
    base_preset = preset_runtime.load(model, EXECUTABLE)
    current_fingerprint = fingerprint(model, state['kind'], project, base_preset)
    if state.get('connection_fingerprint') and state['connection_fingerprint'] != current_fingerprint:
        raise ValueError('原会话的模型接口、Agent 预设、权限或项目已变更，请创建新任务')
    decision = subagent_decision(model, run, preset=base_preset,
                                 probe=bool(state.get('subagent_probe')), task=state)
    allowed_subagent = decision.get('decision') == 'allow'
    if allowed_subagent:
        # Mount exactly one controlled in-process spawn tool.  The revision stays
        # the base content identity so the panel still agrees with the worker.
        preset = preset_runtime.load(model, EXECUTABLE, subagent=True,
                                     child_config=subagent_runtime.child_config(model))
    else:
        preset = base_preset
    patch = build_patch({**model, 'api_base': relay.url}, state['kind'], preset, subagent=allowed_subagent,
                        record_root=project)
    patch[3]['config']['workspaceRoot'] = str(project)
    patch_path = HERE / 'dsh-home/worker-patches' / (current_fingerprint + '.yml')
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(yaml.dump(patch, Dumper=preset_runtime.Dumper, allow_unicode=True, sort_keys=False), encoding='utf-8')
    index_path = context_manager.default_index_path(run)
    index = None
    try:
        index, index_reused = context_manager.ensure_index(project, state.get('files') or [], index_path)
        registry.patch_task(run, context_index=context_manager.index_summary(
            index, reused=index_reused, path=index_path))
    except Exception as exc:  # the index is an aid; it must never stop execution
        registry.patch_task(run, context_index_error=str(exc)[:300])
    prompt, base_prompt = build_agent_prompt(run, state, index=index, index_path=index_path,
                                             delegate=allowed_subagent)
    registry.patch_task(run, **context_manager.prompt_fields(base_prompt, prompt))
    agent_prompt = run / 'dsh-prompt.txt'
    agent_prompt.write_text(prompt, encoding='utf-8')
    ledger = subagent_runtime.ledger_path(run) if allowed_subagent else None
    job = {**availability(), 'task_id': state['id'], 'kind': state['kind'], 'model': model['model'],
           'cwd': str(project), 'patch': str(patch_path), 'state_dir': str(RUNTIME / 'state'),
           'session_key': session_key, 'prompt_file': str(agent_prompt),
           'cancel_file': str(run / 'cancelled.flag'), 'timeout': state['timeout'],
           'record_proof': str(run / record_protection.PROOF_NAME),
           'record_revision': record_protection.revision(),
           'record_root': str(project),
           'subagent_ledger': str(ledger) if ledger else None}
    registry.write_json(run / 'dsh-job.json', job)
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'TOKEN', 'SECRET'))}
    env.update({'DSH_HOME': str(HERE / 'dsh-home'), 'DSH_WORKER_API_KEY': relay.token,
                'MODEL_DESK_PRESET_PROOF': str(run / 'preset-proof.json'),
                'MODEL_DESK_RECORD_PROOF': str(run / record_protection.PROOF_NAME),
                'DSH_TELEMETRY_MODE': 'DISABLED', 'PYTHONIOENCODING': 'utf-8'})
    if ledger is not None:
        env['MODEL_DESK_SUBAGENT_LEDGER'] = str(ledger)
    env['NO_PROXY'] = env['no_proxy'] = ','.join(filter(None, [env.get('NO_PROXY', ''), '127.0.0.1', 'localhost']))
    if model.get('bypass_proxy'):
        bypass = ','.join(filter(None, [env.get('NO_PROXY', ''), urlparse(model['api_base']).hostname]))
        env['NO_PROXY'] = env['no_proxy'] = bypass
    registry.patch_task(run, status='running', phase='连接 DeepSeek Harness', engine='dsh',
                        started_at=registry.now(), worker_pid=os.getpid(), project=str(project),
                        dsh_session_key=session_key, connection_fingerprint=current_fingerprint,
                        sandbox_mode=sandbox_mode(state['kind']),
                        permission_mode='approve-all' if state['kind'] == 'edit' else 'approve-reads')
    registry.patch_task(run, agent_preset=preset['id'], preset_revision=preset['revision'], preset_source=preset['source'],
                        preset_policy_overrides=preset['policy_overrides'], thinking_policy='引擎默认' if model.get('provider') == 'local' else '服务商默认',
                        thinking_runtime=thinking_config.profile(model), thinking_selected_level=model.get('thinking_level', 'default'))
    process = subprocess.Popen([str(NODE), str(RUNTIME / 'run-dsh.mjs'), str(run / 'dsh-job.json')],
                               cwd=project, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    registry.patch_task(run, process_pid=process.pid)
    return {'project': project, 'before': before, 'preset': preset, 'allowed_subagent': allowed_subagent, 'process': process}


def _run_task(run, state, model, relay):
    engine = 'pi' if state.get('engine') == 'pi' else 'dsh'
    label = 'Pi' if engine == 'pi' else 'DSH'
    if engine == 'pi':
        import pi_worker
        prepared = pi_worker.prepare(run, state, model, relay)
    else:
        prepared = _prepare_dsh(run, state, model, relay)
    project, before = prepared['project'], prepared['before']
    preset, allowed_subagent, process = prepared['preset'], prepared['allowed_subagent'], prepared['process']
    key = model.get('api_key', '')
    redact = lambda text: (str(text).replace(key, '[已隐藏]') if key else str(text)).replace(relay.token, '[已隐藏]')
    events = queue.Queue()
    def reader(stream, channel):
        for line in stream:
            events.put((channel, redact(line)))
        events.put((channel, None))
    for stream, channel in ((process.stdout, 'out'), (process.stderr, 'err')):
        threading.Thread(target=reader, args=(stream, channel), daemon=True).start()
    answer = ''
    tools = {}
    subagent_outputs = []
    result = None
    error = ''
    streams = 2
    last_write = 0
    deadline = task_timing.deadline(state.get('timeout',0),grace=30)
    # Distinguishes a real executor failure from a local record/IO failure that
    # happens *after* the executor already returned its outcome.  The latter must
    # never be relabelled as an executor failure.
    outcome = {'executor_finished': False, 'intended_status': None, 'status_persisted': False}
    try:
        with (run / ('pi.log' if engine == 'pi' else 'dsh.log')).open('w', encoding='utf-8') as log:
            while streams:
                if task_timing.expired(deadline):
                    raise TimeoutError(label + ' 任务超过设定时间')
                try:
                    channel, line = events.get(timeout=.3)
                except queue.Empty:
                    continue
                if line is None:
                    streams -= 1
                    continue
                if channel == 'err':
                    log.write(line); log.flush()
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    log.write(line); log.flush()
                    continue
                event_type = event.get('type')
                if event_type == 'text_delta':
                    if event.get('stream') != 'thought':
                        answer += event.get('text', '')
                        log.write(event.get('text', '')); log.flush()
                else:
                    log.write('\n' + json.dumps(event, ensure_ascii=False) + '\n'); log.flush()
                if event_type == 'session':
                    if engine == 'pi':
                        registry.patch_task(run, pi_session_id=event.get('session_id'), pi_session_file=event.get('session_file'))
                    else:
                        registry.patch_task(run, dsh_session_id=event.get('session_id'), acpx_record_id=event.get('acpx_record_id'))
                elif event_type == 'tool_call':
                    tool_id = event.get('toolCallId') or str(len(tools))
                    previous = tools.get(tool_id, {})
                    tools[tool_id] = tool_update(previous, event, project)
                    if str(previous.get('title') or event.get('title') or '') == 'subagent' and event.get('status') == 'completed':
                        output_text = tool_result_text(event)
                        if output_text and len(subagent_outputs) < 8:
                            subagent_outputs.append({'tool_call_id': tool_id, 'text': output_text[:200],
                                                     'chars': len(output_text)})
                            registry.patch_task(run, subagent_tool_outputs=subagent_outputs)
                    registry.patch_task(run, phase='工具：' + str(tools[tool_id].get('title', '执行中'))[:100])
                elif event_type == 'phase':
                    registry.patch_task(run, phase=event.get('text'))
                elif event_type == 'subagent':
                    child = event.get('event') if isinstance(event.get('event'), dict) else {}
                    if child.get('type') == 'start':
                        registry.patch_task(run, phase='子代理已启动：' + str(child.get('provider') or 'spawn')[:60],
                                            subagent_live={'provider': child.get('provider'),
                                                           'child_id': child.get('child_id'),
                                                           'run_id': child.get('run_id'),
                                                           'child_model': child.get('child_model'),
                                                           'at': child.get('at')})
                    elif child.get('type') == 'end':
                        registry.patch_task(run, phase='子代理已结束：' + str(child.get('stop_reason') or 'completed')[:60],
                                            subagent_live_end={'child_id': child.get('child_id'),
                                                               'stop_reason': child.get('stop_reason'),
                                                               'output_chars': child.get('output_chars'),
                                                               'at': child.get('at')})
                elif event_type == 'result':
                    result = event['result']
                elif event_type == 'error':
                    error = event.get('text', label + ' 执行失败')
                if time.monotonic() - last_write > .4:
                    registry.patch_task(run, answer=answer, tool_calls=list(tools.values())[-100:])
                    last_write = time.monotonic()
        # Post-stream exit grace: both streams are closed, so the executor has
        # already produced (or refused) its outcome.  Wait up to the bounded grace
        # instead of raising on a slow teardown -- the old 10-second
        # ``process.wait`` discarded a validated result and relabelled finished
        # work as an executor failure.
        grace_end = time.monotonic() + EXIT_GRACE_SECONDS
        while process.poll() is None and time.monotonic() < grace_end:
            time.sleep(.2)
        exit_grace_exhausted = process.poll() is None
        if allowed_subagent:
            try:
                record_subagent_ledger(run)
            except Exception:  # lineage bookkeeping must never change execution
                pass
        final_state = registry.read_task(run)
        active_fault = recovery_retry.unresolved_fault(final_state)
        if active_fault:
            # Preserve the actual upstream failure; missing runtime proof must
            # not replace its cause. No protection/completion is claimed here.
            registry.patch_task(run, fault_status='unresolved',
                                fault_evidence=recovery_retry.recovery_evidence(final_state))
            raise RuntimeError(active_fault['message'])
        final_state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
        if final_state.get('fault_events'):
            registry.patch_task(run, fault_status='recovered',
                                fault_recovered_at=final_state.get('fault_recovered_at'),
                                fault_evidence=recovery_retry.recovery_evidence(final_state))
        cancelled = (run / 'cancelled.flag').exists() or (result or {}).get('status') == 'cancelled'
        if not cancelled and (process.returncode or not result or result.get('status') != 'completed'):
            raise RuntimeError(error or (result or {}).get('error', {}).get('message') or label + ' 未返回完成结果，请查看执行日志')
        if not cancelled and result.get('stopReason') not in {'end_turn', 'stop', None}:
            raise RuntimeError(label + ' 提前结束：' + str(result.get('stopReason')))
        if not cancelled:
            recorded = registry.read_task(run)
            # Verify successful delivery before accepting it; preserve the real
            # error above for failed/cancelled runs without claiming protection.
            if engine == 'pi':
                import pi_worker
                record_proof = pi_worker.verify(run, project, recorded.get('pi_session_id'))
            else:
                proof = preset_runtime.verify(run, preset, recorded.get('dsh_session_id'))
                registry.patch_task(run, preset_verified=True, preset_proof=proof)
                record_proof = record_protection.verify_proof(run, project, recorded.get('dsh_session_id'))
            registry.patch_task(run, record_protection=True, record_proof=record_proof)
        changed = [name for name, previous in before.items() if digest(project / name) != previous]
        file_hashes = {name: digest(project / name) for name in changed}
        test = None
        if not cancelled and state.get('test_command'):
            registry.patch_task(run, phase='运行验收测试')
            remaining = task_timing.remaining(deadline)
            tested = subprocess.run(['powershell.exe', '-NoProfile', '-Command', state['test_command']], cwd=project,
                                    capture_output=True, encoding='utf-8', errors='replace', timeout=remaining,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            test = {'exit_code': tested.returncode, 'output': redact(tested.stdout + tested.stderr)[-6000:]}
            (run / 'test.log').write_text(test['output'], encoding='utf-8')
        cancelled = cancelled or (run / 'cancelled.flag').exists()
        status = 'cancelled' if cancelled else 'failed' if test and test['exit_code'] else 'completed'
        # The ACP run has returned a validated outcome from here on.  Everything
        # below is local persistence, so a transient IO failure in it must not be
        # reported as an executor failure.
        outcome['executor_finished'] = True
        outcome['intended_status'] = status
        summary = answer[-3500:] or label + ' 未返回正文，请检查工具记录和文件改动。'
        if test:
            summary += '\n\n验收测试：' + ('通过' if test['exit_code'] == 0 else '失败') + '\n' + test['output'][-2000:]
        (run / 'answer.txt').write_text(answer, encoding='utf-8')
        output = {'task_id': state['id'], 'status': status, 'model': model['model'], 'engine': engine,
                  'changed_files': changed, 'file_hashes': file_hashes, 'test': test, 'summary': summary}
        if exit_grace_exhausted:
            # Honest evidence: the outcome is real, the process just outlived the
            # bounded grace and is killed by the cleanup below.
            output['exit_grace_exhausted'] = True
        registry.write_json(run / 'result.json', output)
        registry.patch_task(run, status=status, phase='已停止执行' if cancelled else '执行完成' if status == 'completed' else '验收测试失败',
                            ended_at=registry.now(), answer=answer, summary=summary, changed_files=changed,
                            file_hashes=file_hashes,
                            tool_calls=list(tools.values())[-100:], test_result=test, exit_code=0 if status == 'completed' else 1,
                            exit_grace_exhausted=bool(exit_grace_exhausted),
                            exit_grace_seconds=EXIT_GRACE_SECONDS)
        outcome['status_persisted'] = True
        if not cancelled:
            record_review(run, bool(result) and result.get('status') == 'completed', test, changed)
        print(json.dumps(output, ensure_ascii=False))
        return 0 if status == 'completed' else 1
    except Exception as exc:
        changed = [name for name, old in before.items() if digest(project / name) != old]
        file_hashes = {name: digest(project / name) for name in changed}
        error_text = redact(exc)
        if outcome['executor_finished']:
            # The executor already returned its real outcome; the failure is a
            # local record/IO problem (for example a transient sharing violation
            # while writing the final status).  Keep the real status and record
            # the IO error beside it instead of misreporting completed work as an
            # executor failure.
            fields = {'io_error': error_text, 'io_error_at': registry.now(),
                      'changed_files': changed, 'file_hashes': file_hashes,
                      'summary': '执行已结束，但本地记录写入遇到瞬时错误；执行结论以已落盘结果为准，'
                                 '不把本地IO问题当作执行器失败。'}
            if not outcome['status_persisted']:
                fields['status'] = outcome['intended_status']
                fields['ended_at'] = registry.now()
                fields['exit_code'] = 0 if outcome['intended_status'] == 'completed' else 1
            try:
                registry.patch_task(run, **fields)
            except Exception:  # never overwrite a finished execution with a false failure
                pass
            print(json.dumps({'task_id': state['id'], 'status': outcome['intended_status'],
                              'io_error': error_text}, ensure_ascii=False))
            return 0 if outcome['intended_status'] == 'completed' else 1
        registry.patch_task(run, status='failed', phase=('Pi 执行失败' if engine == 'pi' else 'DSH 执行失败'), ended_at=registry.now(),
                            error=error_text, answer=answer, changed_files=changed, file_hashes=file_hashes,
                            summary='执行未完成；已保留的文件改动需要验收。' if changed else '执行未完成。',
                            tool_calls=list(tools.values())[-100:])
        if not (run / 'cancelled.flag').exists():
            record_review(run, False, None, changed)
        print(json.dumps({'task_id': state['id'], 'error': error_text}, ensure_ascii=False))
        return 1
    finally:
        if process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
            process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
        if allowed_subagent:
            try:
                record_subagent_ledger(run)
            except Exception:  # never let observation break the recorded outcome
                pass


def run_task(run, state, model):
    import execution_endpoints
    execution_endpoints.gate(state.get('engine') or 'dsh', model, workflow_settings.snapshot())
    # One per-task thinking-metadata resolution.  A degraded engine default must
    # not kill an otherwise eligible task, but an exact model mismatch and an
    # explicitly chosen level without trusted metadata still stop before any
    # inference request is sent.
    cancel = lambda: (run / 'cancelled.flag').exists()
    try:
        info, thinking_evidence = thinking_config.prepare_for_task(model, cancel=cancel)
    except thinking_config.ThinkingCancelled:
        registry.patch_task(run, status='cancelled', phase='已停止执行', ended_at=registry.now(),
                            thinking_probe={'schema': 1, 'status': 'cancelled'})
        return 1
    except thinking_config.ThinkingError as error:
        registry.patch_task(run, status='failed', phase='思考等级元数据不可用',
                            ended_at=registry.now(), error=str(error)[:1000],
                            thinking_probe={'schema': 1, 'status': 'refused',
                                            'detail': str(error)[:400]})
        print(json.dumps({'task_id': state['id'], 'error': str(error)}, ensure_ascii=False))
        return 1
    model['thinking_info'] = info
    registry.patch_task(run, thinking_probe=thinking_evidence)
    thinking_config.validate(model)
    settings = workflow_settings.snapshot()
    if state.get('route', {}).get('mode') == 'auto' and not settings['enabled']:
        raise ValueError('协作总开关已关闭，自动派发未启动')
    # Global execution-model scope: the last guard before any inference request,
    # including a worker started directly from the CLI and a resumed session.  It
    # only refuses; the model, authorization and budgets stay untouched.
    model_scope.check(settings.get('model_scope', model_scope.DEFAULT_SCOPE), model_router.is_paid(model))
    if model_router.is_paid(model):
        if not settings['paid_allowed'] or not model_router.rates_for(model):
            raise ValueError('付费执行未开启或缺少单价，请检查工作台预算设置')
        if deepseek_billing.is_official(model):
            # Fresh, read-only balance check before spending; no cached balance as authorization.
            public = registry.public_model(model)
            public['billing'] = {'balance': {'status': 'ready', **deepseek_billing.read_balance(model)}}
            balance = model_router.balance_limit(public)
            if balance <= 0:
                raise ValueError('账户可用余额不足，请充值后重新派发')
            cap = min(state.get('route', {}).get('budget_cny', 0), float(balance)) if settings.get('enforce_paid_budget', True) else 0
            if settings.get('enforce_paid_budget', True) and cap <= 0:
                raise ValueError('账户可用余额或任务预算不足')
            state = registry.patch_task(run, route={**state['route'], 'budget_cny': cap})
    with budget_relay.Relay(run, model, {**settings, 'timeout': state['timeout']}, state.get('relay_port', 0)) as relay:
        registry.patch_task(run, relay_port=relay.server.server_port)
        return _run_task(run, state, model, relay)


def worker_transport():
    """Bind the worker's real entrypoints to the capability adapter.

    The adapter is a façade, not a second engine: every callable here is the
    function the CLI already uses (``run_task``/``_run_task``) or the offline
    read/flag operation the adapter implements.  Nothing is started by building
    this mapping; a caller still has to invoke the adapter deliberately.
    """
    return {
        'dispatch': run_task,
        'session': _run_task,
        'events': executor_adapter.read_events,
        'cancel': executor_adapter.request_cancel,
        'outcome': executor_adapter.read_outcome,
        'usage': executor_adapter.read_usage,
    }


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if args.status:
        print(json.dumps(availability(), ensure_ascii=False)); return 0
    if not args.run_id:
        parser.error('Use dispatch_task.py to submit jobs, or provide --run-id')
    run = registry.task_dir(args.run_id)
    state = json.loads((run / 'task.json').read_text(encoding='utf-8'))
    if (run / 'cancelled.flag').exists():
        return 1
    return run_task(run, state, registry.get_model(state['model_id']))


if __name__ == '__main__':
    raise SystemExit(main())
