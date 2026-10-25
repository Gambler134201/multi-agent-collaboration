"""OpenCode 2.1.3 native ACP executor. No HTTP impersonation or API relay.

Only the explicitly selected free native model is admitted initially. Native
policy plus client-side file mediation restricts tools to declared files;
shell, integrations, nested agents and session reuse remain unavailable.
This is not an OS sandbox. Native aggregate usage is not a request counter.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

import model_registry as registry
import record_protection
import workflow_settings
import delegation_policy

HERE = Path(__file__).resolve().parent
CONFIG = 'opencode-runtime.json'
MODEL = 'deepseek-v4.1-flash-free'
NATIVE_MODEL = 'opencode/' + MODEL
GO_MODEL = 'deepseek-v4.1-flash'
VERSION = '2.1.3'
ACP_VERSIONS = ('2.1.3','2.1.4')
_version_cache = {}


def executable():
    config = HERE / CONFIG
    if config.exists():
        data = json.loads(config.read_text(encoding='utf-8'))
        path = Path(data['executable'])
    else:
        value = os.environ.get('MODEL_DESK_OPENCODE_EXE') or shutil.which('opencode')
        path = Path(value) if value else Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/@opencode-aidesktop/resources/opencode-cli.exe'
    if not path.is_absolute() or not path.is_file():
        raise ValueError('请设置 OpenCode CLI 的绝对路径（MODEL_DESK_OPENCODE_EXE），不是桌面 GUI 程序')
    return path.resolve()


def availability():
    try:
        path = executable()
        key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        if key not in _version_cache:
            p = subprocess.run([str(path), '--version'], capture_output=True, text=True,
                               encoding='utf-8', timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            _version_cache[key] = p.stdout.strip().removeprefix('opencode v') if p.returncode == 0 else ''
        version = _version_cache[key]
        if version != VERSION:
            raise ValueError('当前仅适配 OpenCode %s；实际版本 %s，需重新验证' % (VERSION, version or '未知'))
        return {'ready': True, 'name': 'OpenCode', 'engine': 'opencode', 'version': version,
                'executable': str(path), 'transport': 'Native ACP', 'access_mode': 'project',
                'capabilities': {'chat': True, 'scoped_files': True, 'terminal': False,
                                 'subagents': False, 'session_reuse': False},
                'message': '限定文件读写；终端、子代理与会话续接未开放；原生账户权限以实际调用为准'}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return {'ready': False, 'name': 'OpenCode', 'engine': 'opencode', 'version': '',
                'message': str(exc), 'access_mode': 'project'}


def configure(values):
    if not isinstance(values, dict) or set(values) != {'executable'}:
        raise ValueError('OpenCode 连接仅接受 executable 绝对路径')
    path = Path(values['executable'])
    if not path.is_absolute() or not path.is_file():
        raise ValueError('OpenCode CLI 文件不存在')
    # Validate before persisting, without model calls or reading credentials.
    p = subprocess.run([str(path), '--version'], capture_output=True, text=True, encoding='utf-8',
                       timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if p.returncode or p.stdout.strip() != 'opencode v' + VERSION:
        raise ValueError('此路径不是已经适配的 OpenCode ' + VERSION + ' CLI')
    registry.write_json(HERE / CONFIG, {'executable': str(path.resolve())})
    return {'ok': True, 'endpoint': 'opencode', 'executable': str(path.resolve())}


def validate_task(body, settings, model):
    native_model(model)
    if not settings.get('paid_allowed'):
        raise ValueError('云端执行授权未开启；免费模型也遵循现有服务范围与授权规则')
    if settings.get('enforce_model_call_limit', True):
        raise ValueError('OpenCode 未返回逐次请求计数，无法执行请求次数硬上限')
    if settings.get('enforce_paid_budget', True):
        raise ValueError('OpenCode 使用原生账户，不提供逐请求人民币预算控制；请显式关闭金额硬上限')
    if body.get('continue_task') or body.get('reuse_session'):
        raise ValueError('OpenCode 当前只支持新会话，不支持续接或会话复用')
    if body.get('test_command'):
        raise ValueError('OpenCode 初始接入不开放终端或测试命令')
    if not body.get('project') or not Path(body['project']).is_absolute() or not Path(body['project']).is_dir():
        raise ValueError('OpenCode 必须绑定已存在的项目绝对目录')


def targets(project, files):
    root = Path(project).resolve()
    result = []
    for name in files or []:
        path = Path(name)
        path = (root / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(root) or record_protection.is_record_path(path, root):
            raise ValueError('OpenCode 文件范围越界或指向工作台记录')
        if path.name.lower() in {'auth.json', 'models.json', 'opencode.json', 'opencode.jsonc'} or path.name.lower().startswith('.env'):
            raise ValueError('OpenCode 不接受认证、运行配置或环境密钥作为业务文件')
        if any(c in str(path) for c in '*?') or path.is_dir():
            raise ValueError('OpenCode 文件范围需要精确文件，不接受通配符或目录')
        result.append(path)
    return result


def native_model(model):
    if model.get('provider') != 'custom':
        raise ValueError('OpenCode 原生模型连接类型无效')
    if model.get('model') == MODEL:
        return NATIVE_MODEL
    if model.get('model') == GO_MODEL and model.get('api_base', '').rstrip('/') in (
            'https://opencode.ai/zen/go/v1', 'https://opencode.ai/inference/go/v1'):
        return 'opencode-go/' + GO_MODEL
    import native_models
    native_id=model.get('native_model_id')
    from urllib.parse import urlparse
    if urlparse(model.get('api_base','')).hostname!='opencode.ai':
        raise ValueError('OpenCode 原生目录仅绑定已配置的官方账户渠道')
    expected=('opencode-go/' if model.get('api_base','').rstrip('/') in ('https://opencode.ai/zen/go/v1','https://opencode.ai/inference/go/v1') else 'opencode/')
    if isinstance(native_id,str) and native_id==expected+model.get('model','') and native_models.entry('opencode',native_id):
        return native_id
    raise ValueError('OpenCode 型号未在当前原生目录中确认，请在模型选择器刷新；不会替换模型或套餐')


def native_config(project, files, kind, agent, model_id=NATIVE_MODEL):
    """Global native policies outrank project policies; no allow-all tools."""
    root = Path(project).resolve()
    policies = [
        {'action': 'provider.use', 'resource': '*', 'effect': 'deny'},
        {'action': 'provider.use', 'resource': model_id.split('/')[0], 'effect': 'allow'},
        {'action': 'integration.use', 'resource': '*', 'effect': 'deny'},
        {'action': 'tool.use', 'resource': '*', 'effect': 'deny'},
    ]
    permissions = [{'action': '*', 'resource': '*', 'effect': 'deny'}]
    for path in targets(root, files):
        # Core uses location-relative internal paths; absolute paths are checked
        # too for ACP delegation. No glob/search/shell permission is granted.
        for value in (path.relative_to(root).as_posix(), path.as_posix()):
            for action in ('read', 'edit') if kind == 'edit' else ('read',):
                policies.append({'action': 'tool.use', 'resource': action + ':' + value, 'effect': 'allow'})
                permissions.append({'action': action, 'resource': value, 'effect': 'allow'})
    return {'$schema': 'https://opencode.ai/config.json', 'model': model_id,
            'default_agent': agent, 'update': 'disable', 'snapshots': False,
            'formatter': False, 'warming': False, 'instructions': [],
            'agents': {agent: {'mode': 'primary', 'description': 'Model Desk scoped file worker',
                              'permissions': permissions}}, 'policies': policies}


class Cancelled(Exception):
    pass


class ACP:
    """Single owned stdio process; all unknown client methods fail closed."""
    def __init__(self, process, project, files, kind, cancel=lambda: False, timeout=0, model_id=NATIVE_MODEL):
        self.process, self.root, self.kind = process, Path(project).resolve(), kind
        self.allowed = targets(self.root, files)
        self.cancel, self.timeout, self.started = cancel, timeout, time.monotonic()
        self.inbox, self.next_id, self.session = queue.Queue(), 0, None
        self.answer, self.events, self.calls, self.denials, self.native_updates = [], [], {}, [], []
        self.active_prompt = False
        self.model_id = model_id
        def reader():
            try:
                for line in process.stdout:
                    try:
                        self.inbox.put(json.loads(line))
                    except ValueError:
                        self.inbox.put(ValueError('OpenCode 写入了无效的 ACP JSON'))
            finally:
                self.inbox.put(EOFError('OpenCode ACP 进程已退出'))
        threading.Thread(target=reader, daemon=True).start()

    def send(self, data):
        self.process.stdin.write(json.dumps(data, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def path(self, value, write=False):
        p = Path(value)
        p = (self.root / p).resolve() if not p.is_absolute() else p.resolve()
        # Re-resolve at every call: no stale junction/symlink grant.
        if not p.is_relative_to(self.root) or p not in self.allowed or record_protection.is_record_path(p, self.root):
            raise ValueError('文件不在本次明确范围内')
        if write and self.kind != 'edit':
            raise ValueError('只读任务不允许写入')
        return p

    def handle_request(self, item):
        method, params = item['method'], item.get('params') or {}
        try:
            if params.get('sessionId') != self.session or not self.active_prompt:
                raise ValueError('客户端请求不属于当前执行中的父会话')
            if method == 'fs/read_text_file':
                p = self.path(params['path'])
                data = p.read_bytes()
                if len(data) > 1024 * 1024:
                    raise ValueError('单文件上下文超过1MiB')
                content = data.decode('utf-8')
                if params.get('line') is not None or params.get('limit') is not None:
                    lines = content.splitlines(keepends=True)
                    start = max(0, int(params.get('line', 1)) - 1)
                    content = ''.join(lines[start:start + int(params.get('limit') or len(lines))])
                result = {'content': content}
                self.events.append({'method': method, 'path': str(p), 'sha256': hashlib.sha256(data).hexdigest()})
            elif method == 'fs/write_text_file':
                p = self.path(params['path'], write=True)
                content = params['content'].encode('utf-8')
                if len(content) > 1024 * 1024:
                    raise ValueError('单文件写入超过1MiB')
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(content)
                result = {}
                self.events.append({'method': method, 'path': str(p), 'sha256': hashlib.sha256(content).hexdigest()})
            elif method == 'session/request_permission':
                # Exact native policy should already grant only the scoped files.
                # Unexpected asks (including child tools) are never auto-approved.
                self.denials.append({'method': method, 'reason': 'unexpected permission request'})
                result = {'outcome': {'outcome': 'cancelled'}}
            else:
                raise ValueError('未开放的 OpenCode 客户端方法：' + method)
            self.send({'jsonrpc': '2.0', 'id': item['id'], 'result': result})
        except (OSError, ValueError, KeyError) as exc:
            self.denials.append({'method': method, 'reason': str(exc)})
            self.send({'jsonrpc': '2.0', 'id': item['id'], 'error': {'code': -32602, 'message': str(exc)}})

    def notification(self, item):
        if item.get('method') != 'session/update':
            return
        params = item.get('params') or {}
        if self.session is None and getattr(self, '_pending_new', False):
            return  # initialization notifications precede session/new response
        if params.get('sessionId') != self.session:
            raise ValueError('OpenCode 更新来自其他会话')
        u = params.get('update') or {}
        if (u.get('_meta') or {}).get('opencode/child-session'):
            raise ValueError('OpenCode 不允许子代理执行')
        kind = u.get('sessionUpdate')
        if kind == 'agent_message_chunk':
            content = u.get('content') or {}
            if content.get('type') == 'text':
                self.answer.append(content.get('text', ''))
        elif kind in ('tool_call', 'tool_call_update'):
            call = self.calls.setdefault(u.get('toolCallId'), {})
            call.update(u)
        elif kind in ('current_mode_update', 'config_option_update'):
            self.native_updates.append(u)
        elif kind == 'session_info_update' and (u.get('_meta') or {}).get('opencode/retry'):
            raise ValueError('原生客户端请求重试，已停止；不会自动重发')

    def request(self, method, params, startup=False):
        self.next_id += 1
        ident = self.next_id
        self.send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})
        wait_start = time.monotonic()
        while True:
            if self.cancel() or (self.timeout and time.monotonic() - self.started > self.timeout):
                if self.session:
                    self.send({'jsonrpc': '2.0', 'method': 'session/cancel', 'params': {'sessionId': self.session}})
                raise Cancelled('OpenCode 本任务已取消或达到明确时限')
            if startup and time.monotonic() - wait_start > 45:
                raise TimeoutError('OpenCode ACP 握手超时（未发送模型提示词）')
            try:
                item = self.inbox.get(timeout=.2)
            except queue.Empty:
                continue
            if isinstance(item, BaseException):
                raise item
            if 'method' in item:
                if 'id' in item:
                    self.handle_request(item)
                else:
                    self.notification(item)
            elif item.get('id') == ident:
                if 'error' in item:
                    raise ValueError('OpenCode ACP：' + str(item['error'])[:1200])
                return item.get('result') or {}

    def connect(self, agent):
        init = self.request('initialize', {'protocolVersion': 1, 'clientCapabilities': {
            'fs': {'readTextFile': True, 'writeTextFile': True}, 'terminal': False},
            'clientInfo': {'name': 'model-desk', 'version': '1'}}, startup=True)
        if init.get('protocolVersion') != 1 or (init.get('agentInfo') or {}).get('version') not in ACP_VERSIONS:
            raise ValueError('OpenCode ACP 协议版本不匹配')
        # session/new can emit notifications before its response. No model call
        # is made here; capture those until the response binds the session ID.
        self._pending_new = True
        created = self.request('session/new', {'cwd': str(self.root), 'mcpServers': []}, startup=True)
        self.session = created['sessionId']
        for option, value in (('model', self.model_id), ('mode', agent)):
            changed = self.request('session/set_config_option', {'sessionId': self.session,
                'configId': option, 'value': value}, startup=True)
            options = changed.get('configOptions') or []
            self.config_options = options
            row = next((o for o in options if o.get('id') == option), None)
            if not row or row.get('currentValue') != value:
                raise ValueError('OpenCode 未确认指定的原生模型或受限 Agent')
        self.native_updates.clear()
        return {'initialize': init, 'session_id': self.session, 'model': self.model_id, 'mode': agent}

    def close(self):
        # Closing stdin terminates only this task-owned private ACP server.
        try:
            self.process.stdin.close()
            self.process.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            if self.process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'], capture_output=True)
                else:
                    self.process.kill()
                self.process.wait(timeout=8)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def run_task(run, state, model):
    import execution_endpoints
    import execution_engines
    import model_scope
    import model_router
    settings = workflow_settings.snapshot()
    client = None
    error, answer, status, result = None, '', 'failed', {}
    changed, hashes, before = [], {}, {}
    secret = model.get('api_key', '')
    try:
        if state.get('engine') != 'opencode':
            raise ValueError('OpenCode worker 不能替换其他执行器')
        if (state.get('route') or {}).get('mode') == 'auto' and not settings.get('enabled'):
            raise ValueError('协作已关闭，自动排队任务不启动')
        if not model_scope.matches(model_scope.normalize(settings.get('model_scope', 'all')), model_router.is_paid(model)):
            raise ValueError('当前模型已被全局范围排除')
        execution_endpoints.gate('opencode', model, settings)
        validate_task(state, settings, model)
        info = availability()
        if not info['ready']:
            raise ValueError(info['message'])
        project = Path(state['project']).resolve()
        allowed = targets(project, state.get('files'))
        before = {str(p): digest(p) for p in allowed}
        for index, p in enumerate(allowed):
            if p.is_file() and state['kind'] == 'edit':
                backup = run / 'opencode-file-backups' / (str(index) + '.raw')
                backup.parent.mkdir(exist_ok=True)
                backup.write_bytes(p.read_bytes())
        registry.write_json(run / 'opencode-before.json', before)
        # Never inherit a user's OpenCode configuration or account database.
        home = run / 'opencode-home'
        agent = 'model-desk-' + state['id']
        model_id = native_model(model)
        config = native_config(project, state.get('files'), state['kind'], agent, model_id)
        config_path = home / 'config/opencode/opencode.json'
        registry.write_json(config_path, config)
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('OPENCODE', 'API_KEY', 'TOKEN', 'SECRET', 'XDG_'))}
        for name in ('config', 'data', 'cache', 'state'):
            p = home / name
            p.mkdir(parents=True, exist_ok=True)
            env['XDG_' + name.upper() + '_HOME'] = str(p)
        if secret:
            # OpenCode 2.1.3 uses this shared native credential variable for
            # both catalog integrations. The exact provider policy/model id
            # above distinguishes Go from the free/pay-as-you-go catalog.
            env['OPENCODE_API_KEY'] = secret
        prompt = (run / 'prompt.txt').read_text(encoding='utf-8')
        prompt = ('本任务由模型工作台派发，唯一项目：' + str(project) + '\n明确文件范围：' +
                  json.dumps([str(p) for p in allowed], ensure_ascii=False) +
                  '\n仅允许这些文件的' + ('读写' if state['kind'] == 'edit' else '只读') +
                  '。禁止终端、联网工具、子代理、修改配置或工作台记录。\n\n' + prompt)
        (run / 'opencode-prompt.txt').write_bytes(prompt.encode('utf-8'))
        registry.patch_task(run, status='running', phase='启动 OpenCode 原生客户端', started_at=registry.now(),
            worker_pid=os.getpid(), executor_version=info['version'], model_calls=None,
            native_request_count_unknown=True, billing_source='opencode-native', cost_cny=None,
            opencode_policy_sha256=digest(config_path), opencode_executable_sha256=digest(Path(info['executable'])),
            sandbox_mode='native-policy-and-exact-file-mediation', record_protection=False)
        with (run / 'opencode-stderr.log').open('w', encoding='utf-8') as stderr:
            process = subprocess.Popen([info['executable'], 'acp'], cwd=project, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                text=True, encoding='utf-8', errors='strict', bufsize=1,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            registry.patch_task(run, process_pid=process.pid)
            client = ACP(process, project, state.get('files'), state['kind'],
                         cancel=lambda: (run / 'cancelled.flag').exists(), timeout=state.get('timeout') or 0,
                         model_id=model_id)
            identity = client.connect(agent)
            import native_models
            native_models.apply_control(client,model.get('native_reasoning'))
            registry.patch_task(run, opencode_session_id=client.session, opencode_identity=identity,
                                phase='OpenCode 正在执行', workbench_prompt_submissions=1)
            client.active_prompt = True
            result = client.request('session/prompt', {'sessionId': client.session,
                'prompt': [{'type': 'text', 'text': prompt}]})
            answer = ''.join(client.answer)
            if result.get('stopReason') != 'end_turn' or client.denials:
                raise ValueError('OpenCode 未正常结束，或出现范围/权限拒绝；请独立检查，不自动重发')
            if not answer.strip():
                raise ValueError('OpenCode 没有返回可验收正文')
            # Reconfirm exact identity after execution; a mode/model switch is
            # visible in updates and is never silently accepted.
            for u in client.native_updates:
                if u.get('sessionUpdate') == 'current_mode_update' and u.get('currentModeId') != agent:
                    raise ValueError('OpenCode 执行中更换了 Agent')
                for option in u.get('configOptions') or []:
                    if option.get('id') in ('model', 'mode') and option.get('currentValue') != {'model': model_id, 'mode': agent}[option['id']]:
                        raise ValueError('OpenCode 执行中更换模型或 Agent')
            status = 'completed'
    except Cancelled as exc:
        status, error = 'cancelled', str(exc)
    except Exception as exc:
        error = str(exc).replace(secret, '[已隐藏]') if secret else str(exc)
    finally:
        if client:
            try:
                client.close()
            except Exception:
                status, error = 'failed', 'OpenCode 自有进程清理未确认；禁止自动重发'
        for name, old in before.items():
            h = digest(Path(name))
            if h != old:
                changed.append(str(Path(name).relative_to(Path(state['project']).resolve())))
                hashes[changed[-1]] = h
        if state['kind'] == 'chat' and changed:
            status, error = 'failed', '只读文件发生变化，不能验收'
        if answer:
            (run / 'answer.txt').write_bytes(answer.encode('utf-8'))
        proof = {'session_id': client.session if client else None, 'events': client.events if client else [],
                 'tool_calls': list(client.calls.values()) if client else [], 'denials': client.denials if client else [],
                 'policy_sha256': digest(config_path) if 'config_path' in locals() else None,
                 'cleanup': 'closed' if client and client.process.poll() is not None else 'not_started'}
        registry.write_json(run / 'opencode-proof.json', proof)
        registry.patch_task(run, status=status, phase='执行完成，待独立验收' if status == 'completed' else 'OpenCode 执行未完成',
            ended_at=registry.now(), answer=answer, error=error, changed_files=changed, file_hashes=hashes,
            usage=result.get('usage'), tool_calls=proof['tool_calls'], opencode_proof=proof,
            cost_cny=None, cost_settled_cny=None, model_calls=None, record_protection=False,
            summary='OpenCode 原生执行结束，需独立验收；聚合用量不等于实际调用次数；原生策略不是OS沙箱',
            exit_code=0 if status == 'completed' else 1)
        if status == 'completed':
            registry.update_check(model['id'], {'ok': True, 'kind': 'native_acp', 'inference': True,
                'at': registry.now(), 'message': 'OpenCode 原生模型任务返回成功；任务质量仍需独立验收'})
        delegation_policy.record_executor(run, {'completed': status == 'completed', 'test': None, 'changed_files': changed})
    return 0 if status == 'completed' else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    run = registry.task_dir(args.run_id)
    return run_task(run, registry.read_task(run), registry.get_model(registry.read_task(run)['model_id']))


if __name__ == '__main__':
    raise SystemExit(main())
