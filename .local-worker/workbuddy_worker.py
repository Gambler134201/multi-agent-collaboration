"""Task-owned WorkBuddy native-account executor; no API credential access."""
import argparse
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

import model_registry as registry
import workbuddy_executor as wb
import workbuddy_host_bridge as bridge

MODEL = 'deepseek-v4.1-flash'
MODEL_ROW = 'workbuddy-deepseek-flash'
CONFIG = 'workbuddy.json'
CATALOG = 'workbuddy-catalog.json'
ROUTING_MODES = {'auto', 'auto-model', 'fast-model', 'balanced-model', 'deep-model'}


def model_row_id(code):
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', code) or code.lower() in ROUTING_MODES:
        raise ValueError('请选择明确的原生模型，不使用 Auto 或路由模式')
    return MODEL_ROW if code == MODEL else 'workbuddy-' + hashlib.sha256(code.encode()).hexdigest()[:12]


def catalog():
    path = Path(registry.HERE) / CATALOG
    if not path.exists():
        return {'models': [], 'updated_at': None}
    try:
        data = registry.read_json(path)
        config = configuration()
        info = installation(config)
        if data['connection'] != config or data['fingerprint'] != info['fingerprint'] or data['version'] != info['version']:
            raise ValueError()
        rows = data['models']
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError()
        seen = set()
        for row in rows:
            model_row_id(row['model_id'])
            if row['model_id'] in seen or not isinstance(row['name'], str) or not 0 < len(row['name']) <= 200:
                raise ValueError()
            seen.add(row['model_id'])
        return {'models': rows, 'updated_at': data['updated_at']}
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('WorkBuddy 模型列表缺失、损坏或安装/账户配置已变，请重新读取原生列表') from None


def catalog_model(code):
    model_row_id(code)
    return next((row for row in catalog()['models'] if row['model_id'] == code), None)


def refresh_catalog():
    config = configuration()
    info = installation(config)
    client = None
    with tempfile.TemporaryDirectory(prefix='modeldesk-wb-catalog-') as project:
        try:
            client = bridge.create_client(info, Path(project), config['profile'], MODEL)
            response = client.connect()
            rows = []
            for row in response['models']:
                if row['model_id'].lower() in ROUTING_MODES:
                    continue
                model_row_id(row['model_id'])
                rows.append({'model_id': row['model_id'], 'name': row['name'][:200]})
            if not rows:
                raise ValueError('WorkBuddy 没有返回可选择的明确原生模型')
        finally:
            if client is not None:
                client.close()
    # Publish only after this owned metadata session is confirmed closed.
    registry.write_json(Path(registry.HERE) / CATALOG, {'models': rows, 'updated_at': registry.now(),
        'connection': config, 'version': info['version'], 'fingerprint': info['fingerprint']})
    return {'ok': True, **catalog(), 'model_called': False}


def select_model(code):
    row = catalog_model(code)
    if row is None:
        raise ValueError('模型不在当前 WorkBuddy 原生列表中；未更改选择')
    return registry.upsert_model({'provider': 'workbuddy', 'model': code,
        'name': 'WorkBuddy · ' + row['name']}, model_row_id(code))


def configuration():
    path = Path(registry.HERE) / CONFIG
    if not path.exists():
        return {'home': os.environ.get(wb.HOME_ENV, ''),
                'profile': os.environ.get('MODEL_DESK_WORKBUDDY_PROFILE', '')}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if set(data) != {'home', 'profile'} or any(not isinstance(v, str) for v in data.values()):
            raise ValueError()
        return data
    except (ValueError, TypeError, OSError):
        raise ValueError('WorkBuddy 连接配置无法读取；不会猜测账户或安装位置') from None


@lru_cache(maxsize=4)
def _installation(home, stamps):
    return wb.installation(home)


def installation(config):
    home = Path(config['home'])
    paths = [home / name for name in ('WorkBuddy.exe', 'resources/app.asar',
             'resources/app.asar.unpacked/cli/bin/codebuddy',
             'resources/app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs',
             'resources/app.asar.unpacked/cli/product.json')]
    stamps = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    return dict(_installation(str(home), stamps))


def availability():
    result = {'engine': 'workbuddy', 'name': 'WorkBuddy', 'ready': False, 'installed': False,
              'version': None, 'account_verified': False, 'transport': 'WorkBuddy native host ACP',
              'capabilities': {'chat': True, 'scoped_files': True, 'terminal': False,
                               'subagents': False, 'session_reuse': False},
              'billing_source': 'workbuddy-native-account', 'message': ''}
    try:
        config = configuration()
        result['configuration_values'] = config
        if not config['home'] or not config['profile']:
            raise ValueError('请配置 WorkBuddy 安装目录与当前用户原生配置目录；不会读取认证文件')
        info = installation(config)
        result.update(installed=True, version=info['version'], executable=info['executable'])
        bridge.select_channel(config['profile'], os.listdir('\\\\.\\pipe\\'))
        result.update(ready=True, message='已发现指定账户的原生宿主；登录授权在实际任务时由 WorkBuddy 核实')
    except (OSError, ValueError, wb.ProtocolError, KeyError):
        result['message'] = ('请配置已验证版本的完整 WorkBuddy，并打开同一配置目录的 WorkBuddy、保持登录；'
                             '不改用 API 或 Auto')
    try:
        result['native_catalog'] = catalog()
    except ValueError as error:
        result['native_catalog'] = {'models': [], 'updated_at': None, 'error': str(error)}
    return result


def configure(body):
    if not isinstance(body, dict) or set(body) != {'home', 'profile'}:
        raise ValueError('WorkBuddy 连接只接受安装目录与原生配置目录')
    clean = {}
    for key in ('home', 'profile'):
        if not isinstance(body[key], str) or not body[key].strip():
            raise ValueError('请填写明确的 WorkBuddy 安装与配置目录')
        path = Path(body[key].strip())
        if not path.is_absolute() or not path.is_dir():
            raise ValueError('WorkBuddy 目录必须是已存在的绝对目录')
        clean[key] = str(path.resolve())
    installation(clean)  # Public package identity only; never open account files.
    existing = next((m for m in registry.list_models()['models'] if m['id'] == MODEL_ROW), None)
    if existing and existing.get('provider') != 'workbuddy':
        raise ValueError('原生模型标识已被 API 模型占用；未更改连接配置')
    with registry.file_lock(Path(registry.HERE) / 'workbuddy-config.lock'):
        registry.write_json(Path(registry.HERE) / CONFIG, clean)
    model = registry.upsert_model({'provider': 'workbuddy', 'name': 'WorkBuddy · DeepSeek V4.1 Flash',
                                   'model': MODEL}, MODEL_ROW)
    return {'ok': True, 'model': model, 'configuration': clean, 'model_called': False}


def validate_task(body, settings):
    wb.authorize_native_chat(settings)
    if body.get('kind', 'chat') not in {'chat', 'edit'}:
        raise ValueError('WorkBuddy 仅支持咨询或限定文件修改')
    if body.get('test_command') or body.get('request_output_tokens'):
        raise ValueError('WorkBuddy 无命令或单请求输出限额控制；测试请由主脑独立运行')
    if body.get('continue_task') or body.get('reuse_session'):
        raise ValueError('WorkBuddy 暂不支持原会话续接；新任务或整改使用新原生会话')
    if not body.get('project') or not Path(body['project']).is_absolute() or not Path(body['project']).is_dir():
        raise ValueError('WorkBuddy 任务必须绑定已存在的项目绝对目录')


def cleanup_lease(lease, info, profile, pipe_factory=bridge.WindowsPipe):
    """Only this exact recorded lease; never list/reconnect or kill the GUI."""
    sid, rid = lease.get('session_id', ''), lease.get('runtime_id', '')
    if not re.fullmatch(r'modeldesk-wb-[a-f0-9]{32}', sid) or not re.fullmatch(r'[a-f0-9-]{36}', rid):
        raise ValueError('原生会话身份尚未确认，不能猜测或清理其他会话')
    channel = lease.get('channel', '')
    prefix = '\\\\.\\pipe\\' + bridge.profile_prefix(profile) + 'control-'
    if not channel.startswith(prefix) or not re.fullmatch(r'[a-f0-9]{8}', channel[len(prefix):]):
        raise ValueError('原生会话通道不属于本配置')
    pipe = pipe_factory(channel, info['executable'])
    try:
        control = bridge.LeaseControl(pipe, sid)
        control.runtime_id = rid
        control.request('session.kill', {'sessionId': sid, 'expectedRuntimeId': rid})
    finally:
        pipe.close()


def recovery_guard(state):
    if 'workbuddy' not in state:
        return  # Worker persists this field before any native session creation.
    native = state.get('workbuddy') or {}
    if native.get('prompt_attempted') or native.get('cleanup') not in {'closed', 'not_created'}:
        raise ValueError('WorkBuddy 原请求结果或会话清理未核实，不能重新派发；请主脑检查交付并接手')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def run_task(run, state, model, *, client_factory=None):
    import delegation_policy
    import execution_endpoints
    import record_protection
    import workflow_settings
    import workbuddy_tools
    run = Path(run).resolve()
    client = None
    lease = {}
    native = {'prompt_attempted': False, 'cleanup': 'not_created', 'model_id': model.get('model')}
    before, changed, hashes = {}, [], {}
    answer, result, error, status = '', {}, '', 'failed'
    started = time.monotonic()
    cancel = lambda: (run / 'cancelled.flag').exists()
    try:
        if (state.get('engine') != 'workbuddy' or model.get('provider') != 'workbuddy' or
                model.get('model') != state.get('model') or model_row_id(model.get('model')) != state.get('model_id')):
            raise ValueError('WorkBuddy 任务与原生模型身份不匹配；未请求模型')
        settings = workflow_settings.snapshot()
        execution_endpoints.gate('workbuddy', model, settings)
        validate_task(state, settings)
        if state.get('route', {}).get('mode') == 'auto' and not settings.get('enabled'):
            raise ValueError('协作总开关已关闭，排队自动任务未启动')
        if 'workbuddy' in state:
            recovery_guard(state)
        config = configuration()
        if state.get('workbuddy_connection') and state['workbuddy_connection'] != config:
            raise ValueError('WorkBuddy 安装或账户配置已变；排队任务保留原来源，拒绝切换')
        info = installation(config)
        project = Path(state['project']).resolve()
        if run.parent != project / '.model-desk' / 'runs' or run.name != state['id']:
            raise ValueError('WorkBuddy 任务记录不属于本项目')
        files = (state.get('files') or []) if state['kind'] == 'edit' else []
        readonly = state.get('readonly_files') or []
        record_protection.validate_files(files + readonly, project)
        before = {name: digest(project / name) for name in files}
        prompt = (run / 'prompt.txt').read_text(encoding='utf-8')
        prompt += ('\n\n工作边界：只使用所挂载的限定文件工具，不使用命令、网络、子代理或其他记录。'
                   '没有命令工具，不得声称已运行测试；请报告未验证项供主脑验收。'
                   '\n可修改文件：' + json.dumps(files, ensure_ascii=False) +
                   '\n额外只读文件：' + json.dumps(readonly, ensure_ascii=False))
        job = None
        if files or readonly:
            existing = run / 'workbuddy-tools-job.json'
            if existing.exists():
                tools = workbuddy_tools.FileTools(existing)
                if tools.project != project or tools.job['files'] != files or tools.job['readonly_files'] != readonly:
                    raise ValueError('原文件工具配置发生变化，拒绝恢复')
                job = existing
            else:
                job = workbuddy_tools.prepare(run, project, state['id'], files, readonly)
        registry.patch_task(run, status='running', phase='启动 WorkBuddy 原生会话', started_at=registry.now(),
            worker_pid=os.getpid(), executor_version=info['version'],
            billing_source='workbuddy-native-account', cost_settled_cny=None, cost_cny=None,
            cost_settled_calls=0, cost_unknown_calls=0, model_calls=None, native_credits_used=None,
            sandbox_mode='scoped-file-tools' if files else 'read-only-tools',
            subagent_decision={'decision': 'deny', 'reason': 'WorkBuddy 未加载子代理工具'}, workbuddy=native)

        def lease_event(value):
            nonlocal lease
            lease = value
            native.update(lease=value, cleanup='pending')
            registry.patch_task(run, workbuddy=dict(native))

        if cancel():
            raise wb.Cancelled('任务已取消')
        factory = client_factory or bridge.create_client
        client = factory(info, project, config['profile'], model['model'], tool_job=job, on_lease=lease_event)
        client.cancel_check = cancel
        connected = client.connect()
        registry.patch_task(run, workbuddy_session_id=connected['session_id'], phase='WorkBuddy 正在执行')

        def on_prompt():
            if cancel():
                raise wb.Cancelled('任务已取消')
            native['prompt_attempted'] = True
            registry.patch_task(run, workbuddy=dict(native), native_usage_unknown=True,
                                workbench_prompt_submissions=1)

        client.on_prompt = on_prompt
        total = state.get('timeout') or 0
        remaining = max(.001, total - (time.monotonic() - started)) if total else 0
        if total and remaining <= .001:
            raise TimeoutError()
        result = client.chat(model['model'], prompt, workflow_settings.snapshot(), timeout=remaining)
        answer = result.get('answer') or ''
        status = result.get('status') if result.get('status') in {'completed', 'failed'} else 'failed'
        if status == 'completed' and state['kind'] == 'chat' and not answer.strip():
            status = 'failed'
            error = 'WorkBuddy 没有返回咨询正文，请主脑检查；未重试'
        if status != 'completed':
            error = error or 'WorkBuddy 未正常完成：' + str((result.get('execution_error') or {}).get('category') or result.get('stop_reason') or 'unknown')
    except wb.Cancelled:
        status, error = 'cancelled', '已请求取消本任务的原生会话'
    except Exception as exc:
        status = 'failed'
        error = str(exc) if isinstance(exc, (ValueError, wb.ProtocolError)) else 'WorkBuddy 执行异常（%s）；未重试' % type(exc).__name__
        native['outcome_unknown'] = isinstance(exc, wb.OutcomeUnknown)
    finally:
        try:
            if client is not None:
                client.close()
                native['cleanup'] = 'closed'
            elif lease:
                cleanup_lease(lease, info, config['profile'])
                native['cleanup'] = 'closed'
        except Exception:
            native['cleanup'] = 'unknown'
            error = (error + '；原生会话清理未确认，不能自动重发').strip('；')
            if status == 'completed':
                status = 'failed'
        if 'project' in locals():
            try:
                for name, old_hash in before.items():
                    current = digest(project / name)
                    if current != old_hash:
                        changed.append(name); hashes[name] = current
            except OSError:
                status, error = 'failed', '交付文件状态无法核实，请主脑检查'
        if cancel():
            status = 'cancelled'
        if answer:
            (run / 'answer.txt').write_text(answer, encoding='utf-8')
        registry.patch_task(run, status=status, phase='执行完成' if status == 'completed' else 'WorkBuddy 执行失败',
            ended_at=registry.now(), workbuddy=dict(native), answer=answer, error=error or None,
            changed_files=changed, file_hashes=hashes, usage=result.get('usage'),
            tool_calls=[{'id': str(n), 'title': event.get('tool'), 'status': 'completed' if event.get('ok') else 'failed'}
                        for n, event in enumerate((result.get('file_tool_proof') or {}).get('events', []))],
            record_protection=bool(result.get('file_tool_proof')), native_credits_used=None,
            billing_source='workbuddy-native-account', cost_cny=None, cost_settled_cny=None,
            summary='原生执行结束，等待主脑独立验收；积分与人民币费用未返回', exit_code=0 if status == 'completed' else 1)
        try:
            delegation_policy.record_executor(run, {'completed': status == 'completed', 'test': None, 'changed_files': changed})
        except Exception as exc:
            registry.patch_task(run, review_error=type(exc).__name__)
    return 0 if status == 'completed' else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    run = registry.task_dir(args.run_id)
    state = registry.read_task(run)
    if (run / 'cancelled.flag').exists():
        return 1
    return run_task(run, state, registry.get_model(state['model_id']))


if __name__ == '__main__':
    raise SystemExit(main())
