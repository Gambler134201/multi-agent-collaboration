"""WorkBuddy-native ACP discovery and isolated chat transport candidate.

This module is not registered as a ready Model Desk execution engine yet.
In particular it must not route existing API models through WorkBuddy's account,
or claim that an installation/model catalogue proves a logged-in account.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import threading
import time


SUPPORTED_VERSION = '5.8.2'
HOME_ENV = 'MODEL_DESK_WORKBUDDY_HOME'


class ProtocolError(RuntimeError):
    pass


class OutcomeUnknown(ProtocolError):
    """A written request has no authoritative response; do not resend it."""


def _asar_package(archive):
    with archive.open('rb') as stream:
        header = stream.read(16)
        if len(header) != 16:
            raise ValueError('Invalid ASAR header')
        _, header_size, _, json_size = struct.unpack('<4I', header)
        if not 0 < json_size <= 64 * 1024 * 1024:
            raise ValueError('Invalid ASAR index size')
        entry = json.loads(stream.read(json_size))['files']['package.json']
        size = int(entry['size'])
        if entry.get('unpacked') or not 0 < size <= 1024 * 1024:
            raise ValueError('Unsupported package metadata')
        stream.seek(8 + header_size + int(entry['offset']))
        return json.loads(stream.read(size))


def installation(home=None):
    value = str(home if home is not None else os.environ.get(HOME_ENV, 'E:/workbuddy'))
    root = Path(value)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('WorkBuddy 安装目录必须是已存在的绝对目录')
    root = root.resolve()
    exe = root / 'WorkBuddy.exe'
    archive = root / 'resources/app.asar'
    cli = root / 'resources/app.asar.unpacked/cli/bin/codebuddy'
    bundle = root / 'resources/app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs'
    product = root / 'resources/app.asar.unpacked/cli/product.json'
    if not all(p.is_file() for p in (exe, archive, cli, bundle, product)):
        raise ValueError('WorkBuddy 安装不完整；不会改用其他 CLI 或执行器')
    package = _asar_package(archive)
    product_meta = json.loads(product.read_text(encoding='utf-8'))
    if package.get('name') != '@genie/workbuddy-desktop' or package.get('version') != SUPPORTED_VERSION or product_meta.get('productName') != 'WorkBuddy':
        raise ValueError('WorkBuddy 安装身份或版本未经验证')
    fingerprint = hashlib.sha256()
    for p in (cli, bundle, product):
        with p.open('rb') as stream:
            fingerprint.update(hashlib.file_digest(stream, 'sha256').digest())
    return {'home': str(root), 'executable': str(exe), 'cli': str(cli),
            'version': package['version'], 'product_name': product_meta['productName'],
            'fingerprint': fingerprint.hexdigest()}


def availability(home=None):
    try:
        info = installation(home)
        return {**info, 'installed': True, 'engine': 'workbuddy', 'ready': False,
                'stage': 'native-chat-transport-candidate', 'account_verified': False,
                'message': '已识别 WorkBuddy；原生积分执行、记录保护和工作台派发尚未全部验收，暂不列为可派发 Agent'}
    except (OSError, ValueError, KeyError, struct.error):
        return {'installed': False, 'ready': False, 'engine': 'workbuddy',
                'account_verified': False, 'message': 'WorkBuddy 安装不可用或版本未经验证；未调用模型'}


def process_environment(config_home, *, isolated=False, installation_info=None):
    root = Path(config_home)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('WorkBuddy 配置目录必须明确指定为已存在的绝对目录')
    # Credentials are consumed only by the native client in its own home.
    # This module never opens auth storage or copies tokens into an environment.
    env = {k: v for k, v in os.environ.items() if not any(part in k.upper()
           for part in ('TOKEN', 'SECRET', 'API_KEY', 'CODEBUDDY', 'WORKBUDDY',
                        'MODEL_DESK', 'CODEX', 'ELECTRON', 'NODE_', 'ACC_PRODUCT', 'PROXY', 'CLIENT_INFO_'))}
    env.update({'CODEBUDDY_CONFIG_DIR': str(root), 'WORKBUDDY_CONFIG_DIR': str(root),
                'ELECTRON_RUN_AS_NODE': '1', 'CODEBUDDY_FORCE_LITE_WB_BUNDLE': '1',
                'CODEBUDDY_DISABLE_COMPILE_CACHE': '1', 'CODEBUDDY_DISABLE_CUSTOM_MODELS_FILE': '1',
                'DISABLE_TELEMETRY': '1', 'CODEBUDDY_DISABLE_IDE': '1',
                'SERVER__PORT': '0', 'SERVER__HOST': '127.0.0.1'})
    if installation_info and installation_info.get('product_name'):
        if installation_info.get('product_name') != 'WorkBuddy' or installation_info.get('version') != SUPPORTED_VERSION:
            raise ValueError('WorkBuddy 产品身份或版本未经验证；未启动进程')
        # Match the installed product's public identity, rather than inheriting
        # another caller's telemetry/product context. This is NOT desktop host
        # authority, account authorization or a credential-protection bootstrap.
        env.update({'CLIENT_INFO_PLATFORM': 'WorkBuddy',
                    'CLIENT_INFO_PLATFORM_VERSION': installation_info['version'],
                    'CLIENT_INFO_PRODUCT_NAME': installation_info['product_name'],
                    'CLIENT_INFO_PRODUCT_VERSION': installation_info['version'],
                    'CLIENT_INFO_IDE_TYPE': 'WorkBuddy',
                    'CLIENT_INFO_PLUGIN_NAME': 'model-desk-workbuddy-executor',
                    'CLIENT_INFO_PLUGIN_VERSION': '0.1.0'})
    if isolated:
        env.update({k: str(root) for k in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA')})
    else:
        # This is the desktop's own product/model-catalogue handover path,
        # published by workbuddy-product-config.js. Leave parsing to WorkBuddy;
        # never open/copy its contents or scan user authentication storage.
        product_cache = root / 'cache/acc-product-config-v3.json'
        if product_cache.is_file():
            env['ACC_PRODUCT_CONFIG_PATH'] = str(product_cache)
    return env


def command(info, model_id=None):
    # The bundled lite-wb build exits silently with --acp alone in 5.8.2.
    # --serve selects the server strategy; --acp retains NDJSON stdio transport.
    # Keep the native HTTP authentication default; never use --auth none.
    args = [info['executable'], info['cli'], '--serve', '--acp',
            '--no-session-persistence', '--tools', '', '--strict-mcp-config',
            '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '']
    if model_id is not None:
        if not isinstance(model_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', model_id):
            raise ValueError('WorkBuddy 模型 ID 无效')
        args.extend(['--model', model_id])
    return args


def authorize_native_chat(settings):
    import model_scope
    if not isinstance(settings, dict) or settings.get('paid_allowed') is not True:
        raise ValueError('WorkBuddy 原生账户调用需要已有付费/积分授权')
    model_scope.check(settings.get('model_scope', model_scope.DEFAULT_SCOPE), paid=True)
    # Native credits cannot enforce API request/CNY ceilings. Unknown != free.
    if settings.get('enforce_paid_budget') is not False or settings.get('enforce_model_call_limit') is not False:
        raise ValueError('WorkBuddy 积分/请求计量尚未接入，不能保证当前强制上限；不会绕过或关闭上限')


def model_catalog(result):
    models = result.get('models') if isinstance(result, dict) else None
    rows = models.get('availableModels') if isinstance(models, dict) else None
    if not isinstance(rows, list):
        raise ProtocolError('WorkBuddy 没有返回可核对的模型目录')
    public, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get('modelId')
        if not isinstance(model_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', model_id):
            # Native pickers also contain entries outside this adapter's model
            # ID contract. Exclude those entries, never forward them as flags.
            continue
        if model_id in seen:
            raise ProtocolError('WorkBuddy 模型标识重复')
        seen.add(model_id)
        name = row.get('name')
        public.append({'model_id': model_id, 'name': name[:200] if isinstance(name, str) and name else model_id})
    if rows and not public:
        raise ProtocolError('WorkBuddy 没有返回有效的模型标识')
    return public


def prompt_failure(result):
    """Classify known failure markers without retaining opaque auth payloads."""
    meta = result.get('_meta') if isinstance(result.get('_meta'), dict) else {}
    raw = meta.get('codebuddy.ai/errorMessage', result.get('errorMessage'))
    code = None
    business_code = None
    http_status = None
    native_category = None
    text = raw if isinstance(raw, str) else ''
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            error = decoded.get('error', decoded)
            if isinstance(error, dict):
                candidate = error.get('code')
                if type(candidate) is int:
                    code = candidate
                data = error.get('data')
                if isinstance(data, dict):
                    business_code = data.get('code') if type(data.get('code')) is int else None
                    http_status = data.get('statusCode') if type(data.get('statusCode')) is int else None
                    value = data.get('category')
                    if value in ('auth', 'quota', 'custom_model_auth', 'network', 'task_configuration'):
                        native_category = value
    lower = text.lower()
    if native_category == 'auth' or any(marker in lower for marker in ('not logged in', 'unauthorized', 'authentication required', '请先登录', '未登录', '登录失效')):
        category = 'authentication_required'
    elif native_category == 'quota' or any(marker in lower for marker in ('insufficient credits', '积分不足', '额度不足')):
        category = 'credits_unavailable'
    elif result.get('sensitiveInputDecision'):
        category = 'sensitive_input_refused'
    else:
        category = 'native_refusal' if result.get('stopReason') == 'refusal' else 'native_turn_not_completed'
    return {'category': category, 'native_error_code': code, 'business_code': business_code,
            'http_status': http_status, 'native_category': native_category,
            'details_present': raw is not None}


class NativeChatClient:
    """One task-owned native process, no inferred cwd, no model fallback."""

    def __init__(self, info, project, config_home, *, isolated=False, model_id=None, popen=subprocess.Popen,
                 tool_job=None):
        project = Path(project)
        if not project.is_absolute() or not project.is_dir():
            raise ValueError('WorkBuddy 必须绑定明确存在的项目绝对目录')
        self.project = str(project.resolve())
        self.tool_job = None
        self.mcp_servers = []
        if tool_job is not None:
            import workbuddy_tools
            tools = workbuddy_tools.FileTools(tool_job)
            if str(tools.project) != self.project:
                raise ValueError('WorkBuddy 文件工具与会话项目不匹配')
            self.tool_job = Path(tool_job).resolve()
            self.mcp_servers = [workbuddy_tools.server_config(self.tool_job)]
        self.session_id = None
        self.requested_model_id = model_id
        self.models = []
        self.counter = 0
        self.prompt_sent = False
        self.diagnostics = []
        self.inbox = queue.Queue()
        self.tainted = False
        self.process = popen(command(info, model_id), cwd=self.project,
                             env=process_environment(config_home, isolated=isolated, installation_info=info),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, encoding='utf-8', errors='replace',
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.threads = [threading.Thread(target=self._read, daemon=True),
                        threading.Thread(target=self._drain, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _read(self):
        protocol_started = False
        discarded_startup_bytes = 0
        try:
            for line in self.process.stdout:
                if not line.strip():
                    continue
                if len(line) > 4 * 1024 * 1024:
                    self.inbox.put(ProtocolError('WorkBuddy 消息超过上限'))
                    return
                try:
                    value = json.loads(line)
                except ValueError:
                    # --serve may print its private Web-UI startup banner
                    # before the first ACP packet. Discard it, bounded, without
                    # recording URLs/passwords. After ACP begins require NDJSON.
                    if not protocol_started:
                        discarded_startup_bytes += len(line.encode('utf-8'))
                        if discarded_startup_bytes <= 16384:
                            continue
                    self.inbox.put(ProtocolError('WorkBuddy 返回了非 JSON 协议消息'))
                    return
                if isinstance(value, dict) and value.get('jsonrpc') == '2.0':
                    protocol_started = True
                self.inbox.put(value)
        finally:
            self.inbox.put(EOFError())

    def _drain(self):
        # Never persist arbitrary native stderr: it can contain login details.
        for _ in self.process.stderr:
            pass

    def request(self, method, params, *, timeout=20, event=None):
        if self.tainted:
            raise OutcomeUnknown('本连接已有未知结果；不能继续或自动重发')
        if method not in {'initialize', 'session/new', 'session/set_config_option', 'session/prompt'}:
            raise ValueError('未支持的 WorkBuddy 请求')
        self.counter += 1
        request_id = self.counter
        try:
            self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': request_id,
                                                'method': method, 'params': params}, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            self.tainted = True
            raise OutcomeUnknown('WorkBuddy 请求发送结果未知；未重发') from None
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            remaining = max(0, deadline - time.monotonic()) if deadline else None
            try:
                value = self.inbox.get(timeout=remaining)
            except queue.Empty:
                self.tainted = True
                raise OutcomeUnknown('WorkBuddy 请求未返回权威结果；未重发') from None
            if isinstance(value, BaseException):
                self.tainted = True
                category = 'protocol' if isinstance(value, ProtocolError) else 'eof'
                raise OutcomeUnknown('WorkBuddy 通道中断（%s），结果未知；未重发' % category) from None
            if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
                self.tainted = True
                raise OutcomeUnknown('WorkBuddy 协议结构异常，结果未知')
            if value.get('id') == request_id and not value.get('method'):
                if 'error' in value:
                    # Error bodies may embed URLs/credentials; report only code.
                    error = value['error']
                    code = error.get('code') if isinstance(error, dict) else None
                    code = code if type(code) is int else None
                    self.diagnostics.append({'kind': 'rpc_error', 'method': method, 'code': code})
                    raise ProtocolError('WorkBuddy 拒绝请求（错误码 %s）；未重试' % code)
                result = value.get('result')
                if not isinstance(result, dict):
                    self.tainted = True
                    raise OutcomeUnknown('WorkBuddy 响应结构异常，结果未知')
                return result
            if 'id' in value:
                if value.get('method') == 'session/request_permission' and self.tool_job is not None:
                    self._file_permission(value)
                    continue
                self.tainted = True
                raise OutcomeUnknown('WorkBuddy 返回未授权的交互请求；未自动批准')
            if event and value.get('method') == 'session/update':
                params = value.get('params')
                if not isinstance(params, dict) or params.get('sessionId') != self.session_id:
                    self.tainted = True
                    raise OutcomeUnknown('WorkBuddy 事件会话不匹配')
                event(params.get('update', {}))

    def _file_permission(self, request):
        """Approve once, only for the own mounted file server and scoped target."""
        import workbuddy_tools
        params = request.get('params')
        granted = False
        option_id = None
        reason = 'session_or_request_invalid'
        if isinstance(params, dict) and params.get('sessionId') == self.session_id:
            call = params.get('toolCall')
            call = call if isinstance(call, dict) else {}
            meta = call.get('_meta')
            meta = meta if isinstance(meta, dict) else {}
            tool = meta.get('codebuddy.ai/toolName')
            names = {'mcp__modeldesk-files__' + n: n for n in workbuddy_tools.NAMES}
            names.update({'mcp__modeldesk_files__' + n: n for n in workbuddy_tools.NAMES})
            options = params.get('options')
            # ACP optionId is an opaque identifier, not the permission kind.
            # Native 5.8.2 MCP uses kind=allow_once, optionId=allow. Its sandbox
            # intercept uses another shape and must never be approved here.
            once = [o for o in options if isinstance(o, dict) and o.get('kind') == 'allow_once'
                    and o.get('optionId') in ('allow', 'allow_once')] if isinstance(options, list) else []
            reason = 'tool_or_once_option_not_supported'
            try:
                if isinstance(tool, str) and tool in names and len(once) == 1:
                    tools = workbuddy_tools.FileTools(self.tool_job)
                    proof = workbuddy_tools.verify(self.tool_job)
                    name = names[tool]
                    args = call.get('rawInput')
                    fields = {'read_file': {'path'}, 'write_file': {'path', 'content'},
                              'edit_file': {'path', 'old_text', 'new_text'}}
                    if not isinstance(args, dict) or set(args) != fields[name]:
                        raise ValueError('参数不匹配')
                    if name != 'read_file' and proof.get('writes_paused'):
                        raise ValueError('写入已暂停')
                    tools.target(args['path'], write=name != 'read_file')
                    granted = True
                    option_id = once[0]['optionId']
                    reason = 'scoped_file_once'
            except (OSError, ValueError):
                granted = False
                reason = 'scope_or_guard_check_refused'
        self.diagnostics.append({'kind': 'file_permission', 'decision': 'allow_once' if granted else 'deny',
                                 'reason': reason, 'option_id': option_id})
        outcome = {'outcome': 'selected', 'optionId': option_id} if granted else {'outcome': 'cancelled'}
        try:
            self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                'result': {'outcome': outcome}}) + '\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            self.tainted = True
            raise OutcomeUnknown('WorkBuddy 文件工具授权回执结果未知；未重发') from None
        if not granted:
            self.tainted = True
            raise ProtocolError('WorkBuddy 请求了任务范围外或未经挂载验证的工具；已拒绝并停止本轮')

    def connect(self):
        if self.session_id is not None:
            raise ValueError('当前连接已建立会话，不能隐式新建另一个会话')
        result = self.request('initialize', {'protocolVersion': 1,
            'clientInfo': {'name': 'model-desk-workbuddy-candidate', 'version': '0.1.0'},
            'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}})
        if type(result.get('protocolVersion')) is not int or result.get('protocolVersion') != 1:
            raise ProtocolError('WorkBuddy ACP 版本不匹配')
        result = self.request('session/new', {'cwd': self.project, 'mcpServers': self.mcp_servers})
        session_id = result.get('sessionId')
        if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,200}', session_id):
            raise ProtocolError('WorkBuddy 没有返回有效的新会话标识')
        self.session_id = session_id
        self.models = model_catalog(result)
        current = result.get('models', {}).get('currentModelId')
        # WorkBuddy's suggested picker list can omit an explicitly selected
        # built-in model. Require BOTH native current-model reports before
        # accepting that omission; a CLI flag alone is not confirmation.
        if self.requested_model_id and current == self.requested_model_id:
            options = result.get('configOptions', [])
            confirmed = any(isinstance(o, dict) and o.get('id') == 'model' and
                            o.get('currentValue') == current for o in options)
            if confirmed and current not in {m['model_id'] for m in self.models}:
                self.models.append({'model_id': current, 'name': current})
        return {'session_id': session_id, 'models': self.models, 'account_verified': False,
                'current_model_id': current, 'project': self.project, 'model_called': False}

    def chat(self, model_id, prompt, settings, *, timeout=0):
        authorize_native_chat(settings)
        if self.tool_job is not None:
            import workbuddy_tools
            if workbuddy_tools.verify(self.tool_job).get('writes_paused'):
                raise ProtocolError('本任务此前触发工具拒绝；不能重新请求模型绕过暂停')
        if self.prompt_sent:
            raise ValueError('候选一次只支持一个新会话轮次；不会重派或隐式续接')
        if model_id not in {row['model_id'] for row in self.models}:
            raise ValueError('WorkBuddy 未提供指定模型；不会更换或回退')
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError('WorkBuddy 提示词不能为空')
        selected = self.request('session/set_config_option', {'sessionId': self.session_id,
            'configId': 'model', 'value': model_id})
        options = selected.get('configOptions', [])
        if not any(isinstance(o, dict) and o.get('id') == 'model' and o.get('currentValue') == model_id for o in options):
            raise ProtocolError('WorkBuddy 未确认指定模型，未发送提示词')
        answer = []

        def update(value):
            if isinstance(value, dict) and value.get('sessionUpdate') == 'agent_message_chunk':
                content = value.get('content', {})
                if isinstance(content, dict) and content.get('type') == 'text' and isinstance(content.get('text'), str):
                    answer.append(content['text'])

        self.prompt_sent = True
        result = self.request('session/prompt', {'sessionId': self.session_id,
            'prompt': [{'type': 'text', 'text': prompt}]}, timeout=timeout, event=update)
        stop_reason = result.get('stopReason')
        raw_usage = result.get('usage')
        usage = {k: v for k, v in raw_usage.items()
                 if k in {'inputTokens', 'outputTokens', 'totalTokens', 'cachedReadTokens', 'cachedWriteTokens'}
                 and type(v) in (int, float) and math.isfinite(v) and v >= 0} if isinstance(raw_usage, dict) else {}
        tool_proof = None
        tool_refused = False
        if self.tool_job is not None:
            tool_proof = workbuddy_tools.verify(self.tool_job)
            tool_refused = tool_proof.get('writes_paused') is True
        return {'engine': 'workbuddy', 'model_id': model_id, 'session_id': self.session_id,
                'project': self.project, 'status': 'completed' if stop_reason == 'end_turn' and not tool_refused else 'failed',
                'stop_reason': stop_reason, 'answer': ''.join(answer),
                'usage': usage or None, 'cost_cny': None, 'credits_used': None,
                'execution_error': ({'category': 'file_tool_refused'} if tool_refused else
                                    prompt_failure(result) if stop_reason != 'end_turn' else None),
                'file_tool_proof': tool_proof,
                'adapter_diagnostics': list(self.diagnostics),
                'billing_source': 'workbuddy-native-account', 'accepted': False}

    def close(self):
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Only this client's child, never a process-name search/GUI kill.
            self.process.terminate()
            self.process.wait(timeout=5)
        for thread in self.threads:
            thread.join(timeout=2)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()
