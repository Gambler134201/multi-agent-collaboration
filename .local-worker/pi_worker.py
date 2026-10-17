"""Run the locally installed Pi through the shared accounting/outcome worker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import context_manager
import model_registry as registry
import record_protection
import runtime_paths

HERE = Path(__file__).resolve().parent
# Pi is the user's own install. MODEL_DESK_PI_HOME declares where it lives; the
# layout below is the Pi 1.3.0 layout this build was actually verified against,
# resolved relative to that root. No author path, drive letter or Node install
# is baked in, and an unconfigured root stays UNSET (never ready).
PI_HOME = runtime_paths.pi_home() or runtime_paths.UNSET
PACKAGE = PI_HOME / runtime_paths.PI_PACKAGE_DIR
NODE = PI_HOME / runtime_paths.PI_NODE
CLI = PI_HOME / runtime_paths.PI_CLI
EXTENSION = HERE / 'agent-runtime/pi-protection.ts'
RUNNER = HERE / 'agent-runtime/run-pi.mjs'


def availability():
    """Pi install check bound to MODEL_DESK_PI_HOME; never substitutes engines.

    Only the Pi release this build was verified against is accepted.  A missing
    root, a different package or a different version all report the exact
    configuration step instead of quietly using another engine or version.
    """
    # Use the same process-local paths as prepare(), fingerprint() and spawn.
    # Environment changes take effect in a newly started process/session.
    home = None if PI_HOME == runtime_paths.UNSET else PI_HOME
    package, node, cli = PACKAGE, NODE, CLI
    name, version = runtime_paths.pi_release(package)
    supported = name == runtime_paths.PI_PACKAGE and version == runtime_paths.PI_VERSION
    ready = home is not None and supported and all(
        path.is_file() for path in (node, cli, RUNNER, EXTENSION, record_protection.POLICY_FILE))
    if ready:
        message = ''
    elif home is None:
        message = (f'未配置 Pi 安装位置：请设置环境变量 {runtime_paths.PI_HOME_ENV} 指向本机 Pi 安装根目录；'
                   f'当前只适配 Pi {runtime_paths.PI_VERSION}，不会改用其他执行器')
    elif not supported:
        message = (f'Pi 安装不可用或版本未经验证（检测到 {name or "无包名"} {version or "无版本号"}）；'
                   f'当前只适配 {runtime_paths.PI_PACKAGE} {runtime_paths.PI_VERSION}，不会改用其他版本或执行器')
    else:
        message = (f'已检测到 Pi {version}，但缺少预期文件（{node} 或 {cli}）；'
                   f'请核对 {runtime_paths.PI_HOME_ENV} 是否指向完整安装根目录')
    return {'ready': ready, 'name': 'Pi Agent', 'engine': 'pi', 'version': version,
            'executable': str(cli), 'node': str(node), 'transport': 'Pi RPC',
            'edit_sandbox_mode': 'danger-full-access', 'chat_sandbox_mode': 'read-only',
            'message': message}


def revision():
    digest = hashlib.sha256()
    for file in (EXTENSION, RUNNER, record_protection.POLICY_FILE):
        digest.update(file.read_bytes())
    return digest.hexdigest()


def fingerprint(model, state, project):
    data = ['pi', availability()['version'], str(CLI), model['id'], model['model'], model['api_base'],
            model.get('max_input_tokens'), model.get('max_output_tokens'), model.get('thinking_level', 'default'),
            state['kind'], str(project), revision()]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def prepare(run, state, model, relay):
    import dsh_worker
    info = availability()
    if not info['ready']:
        raise ValueError(info['message'])
    project = Path(str(state.get('project') or ''))
    if not state.get('project') or not project.is_absolute() or not project.is_dir():
        raise ValueError('Pi 任务必须绑定已存在的项目绝对目录；不能猜测当前项目')
    project = project.resolve()
    if state['kind'] == 'edit':
        record_protection.validate_files(state.get('files') or [], project)
    before = {name: dsh_worker.digest(project / name) for name in state.get('files') or []}
    identity = fingerprint(model, state, project)
    if state.get('connection_fingerprint') and state['connection_fingerprint'] != identity:
        raise ValueError('Pi 会话的模型、项目、执行器版本或保护策略已改变，请创建新任务')
    session_file = Path(state['pi_session_file']).resolve() if state.get('pi_session_file') else None
    # A continued session remains in the same project's own record tree.
    import project_storage
    if session_file and not session_file.is_relative_to(project_storage.runs_dir(project).resolve()):
        raise ValueError('Pi 会话文件不属于当前项目的任务记录，已拒绝续接')
    if state.get('pi_session_id') and (not session_file or not session_file.is_file()):
        raise ValueError('Pi 原会话文件缺失，不能以空白会话冒充续接')
    index_path = context_manager.default_index_path(run)
    index = None
    try:
        index, reused = context_manager.ensure_index(project, state.get('files') or [], index_path)
        registry.patch_task(run, context_index=context_manager.index_summary(index, reused=reused, path=index_path))
    except Exception as exc:
        registry.patch_task(run, context_index_error=str(exc)[:300])
    prompt, base = dsh_worker.build_agent_prompt(run, state, index=index, index_path=index_path, delegate=False)
    prompt = prompt.replace('DSH 工具层', 'Pi 工具层')
    prompt_path = run / 'pi-prompt.txt'
    prompt_path.write_text(prompt, encoding='utf-8')
    registry.patch_task(run, **context_manager.prompt_fields(base, prompt))
    agent_dir = run / 'pi-agent'
    agent_dir.mkdir(parents=True, exist_ok=True)
    registry.write_json(agent_dir / 'models.json', {'providers': {'desk-worker': {
        'baseUrl': relay.url, 'api': 'openai-completions', 'apiKey': '${MODEL_DESK_PI_RELAY_KEY}',
        'models': [{'id': model['model'], 'name': model['model'], 'reasoning': False, 'input': ['text'],
                    'contextWindow': model.get('max_input_tokens', 32768), 'maxTokens': model.get('max_output_tokens', 4096),
                    'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}}]}}})
    registry.write_json(agent_dir / 'settings.json', {'retry': {'enabled': False}, 'packages': [],
                         'enableSkillCommands': False, 'quietStartup': True})
    job = {'task_id': state['id'], 'kind': state['kind'], 'cwd': str(project), 'model': model['model'],
           'node': str(NODE), 'cli': str(CLI), 'extension': str(EXTENSION), 'policy': str(record_protection.POLICY_FILE),
           'agent_dir': str(agent_dir), 'session_file': str(session_file) if session_file else None,
           'session_dir': str(run / 'pi-sessions'), 'session_id': state.get('pi_session_id'),
           'prompt_file': str(prompt_path), 'cancel_file': str(run / 'cancelled.flag'), 'api_base': relay.url,
           'proof_file': str(run / 'pi-proof.json'), 'revision': revision(), 'timeout': state.get('timeout', 0)}
    job_file = run / 'pi-job.json'
    (run / 'pi-sessions').mkdir(exist_ok=True)
    registry.write_json(job_file, job)
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'TOKEN', 'SECRET'))}
    env.update({'MODEL_DESK_PI_RELAY_KEY': relay.token, 'PI_CODING_AGENT_DIR': str(agent_dir),
                'MODEL_DESK_PI_JOB': str(job_file), 'PI_OFFLINE': '1', 'PYTHONIOENCODING': 'utf-8',
                'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'})
    registry.patch_task(run, engine='pi', status='running', phase='启动 Pi Agent', started_at=registry.now(),
                        worker_pid=os.getpid(), connection_fingerprint=identity,
                        executor_version=info['version'], sandbox_mode='danger-full-access' if state['kind'] == 'edit' else 'read-only',
                        subagent_decision={'decision': 'deny', 'reason': 'Pi 尚无实测子代理授权，未加载额外工具或扩展'})
    process = subprocess.Popen([str(NODE), str(RUNNER), str(job_file)], cwd=project, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    registry.patch_task(run, process_pid=process.pid)
    return {'project': project, 'before': before, 'process': process, 'preset': None, 'allowed_subagent': False}


def verify(run, project, session_id):
    proof = json.loads((run / 'pi-proof.json').read_text(encoding='utf-8'))
    if not session_id or proof.get('session_id') != session_id or proof.get('revision') != revision():
        raise ValueError('Pi 保护验证或父会话身份不匹配，任务不能视为完成')
    if Path(proof.get('root') or '').resolve() != project.resolve() or proof.get('source') != 'model-desk-pi-protection':
        raise ValueError('Pi 保护证据不属于当前项目')
    state = registry.read_task(run)
    if proof.get('tools_guard') is not True or proof.get('kind') != state.get('kind') or proof.get('session_file') != state.get('pi_session_file'):
        raise ValueError('Pi 工具保护证据与任务模式或原会话文件不匹配')
    return proof


def run_task(run, state, model):
    import dsh_worker
    if state.get('engine') != 'pi':
        raise ValueError('Pi worker 不能替换任务记录中指定的执行 Agent')
    return dsh_worker.run_task(run, state, model)


def _run_task(run, state, model, relay):
    import dsh_worker
    if state.get('engine') != 'pi':
        raise ValueError('Pi 会话接口不能替换任务的执行 Agent')
    return dsh_worker._run_task(run, state, model, relay)


def worker_transport():
    import executor_adapter
    return {'dispatch': run_task, 'session': _run_task,
            'events': executor_adapter.read_events, 'cancel': executor_adapter.request_cancel,
            'outcome': executor_adapter.read_outcome, 'usage': executor_adapter.read_usage}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if args.status:
        print(json.dumps(availability(), ensure_ascii=False)); return 0
    if not args.run_id:
        parser.error('请通过工作台提交任务或指定 --run-id')
    run = registry.task_dir(args.run_id)
    state = registry.read_task(run)
    if (run / 'cancelled.flag').exists():
        return 1
    return run_task(run, state, registry.get_model(state['model_id']))


if __name__ == '__main__':
    raise SystemExit(main())
