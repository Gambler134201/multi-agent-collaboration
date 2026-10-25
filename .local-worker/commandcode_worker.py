"""Pinned official Command Code CLI with the workbench's exact-file native mod."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

import model_registry as registry
import workflow_settings
import delegation_policy
import opencode_worker

HERE = Path(__file__).resolve().parent
CONFIG = 'commandcode-runtime.json'
GUARD = HERE / 'agent-runtime/commandcode-guard.mjs'
VERSION = '1.86.1'
MODELS = ('deepseek/deepseek-v4.1-flash',)


def configuration():
    try:
        data = json.loads((HERE / CONFIG).read_text(encoding='utf-8'))
        if set(data) != {'node', 'cli'}:
            raise ValueError()
        for value in data.values():
            if not Path(value).is_absolute() or not Path(value).is_file():
                raise ValueError()
        return data
    except (OSError, ValueError, TypeError):
        raise ValueError('请配置官方 Command Code CLI 与 Node 的绝对路径') from None


def availability():
    try:
        c = configuration()
        package = Path(c['cli']).parent.parent / 'package.json'
        metadata = json.loads(package.read_text(encoding='utf-8'))
        if metadata.get('name') != 'command-code' or metadata.get('version') != VERSION or not GUARD.is_file():
            raise ValueError('当前仅验证 Command Code ' + VERSION + '；升级后需重新验收')
        return {'ready': True, 'name': 'CommandCode', 'engine': 'commandcode', 'version': VERSION,
                'transport': 'Official native headless NDJSON', 'access_mode': 'project',
                'capabilities': {'chat': True, 'scoped_files': True, 'terminal': False,
                                 'subagents': False, 'session_reuse': False},
                'configuration_values': c, 'message': '限定文件读写；免费DeepSeek型号未提供；费用与原生请求计数未同步'}
    except (OSError, ValueError, KeyError) as exc:
        return {'ready': False, 'name': 'CommandCode', 'engine': 'commandcode', 'message': str(exc)}


def configure(values):
    if not isinstance(values, dict) or set(values) != {'node', 'cli'}:
        raise ValueError('需要 Node 和官方 CLI 的绝对路径')
    if any(not Path(p).is_absolute() or not Path(p).is_file() for p in values.values()):
        raise ValueError('Command Code 文件不存在')
    result = subprocess.run([values['node'], values['cli'], '--version'], capture_output=True,
        text=True, encoding='utf-8', timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode or result.stdout.strip() != VERSION:
        raise ValueError('指定 CLI 不是已适配的 Command Code ' + VERSION)
    registry.write_json(HERE / CONFIG, values)
    return {'ok': True, 'endpoint': 'commandcode', 'version': VERSION}


def validate_task(body, settings, model):
    import native_models
    entry=native_models.entry('commandcode',model.get('model'))
    if model.get('provider') != 'custom' or model.get('model') not in MODELS and not entry:
        raise ValueError('CommandCode 型号未在当前原生目录中确认，请刷新后再选择；不替换模型或套餐')
    control=model.get('native_reasoning') or {}
    if control.get('value') not in (None,'default') and (not entry or control['value'] not in {r['value'] for r in entry['reasoning']}):
        raise ValueError('CommandCode 未声明所选思考强度')
    if not settings.get('paid_allowed'):
        raise ValueError('CommandCode 云端执行授权未开启')
    if settings.get('enforce_paid_budget', True) or settings.get('enforce_model_call_limit', True):
        raise ValueError('CommandCode 无逐请求人民币/计次拦截，硬上限开启时拒绝；不会自动关闭限制')
    if body.get('continue_task') or body.get('reuse_session') or body.get('test_command'):
        raise ValueError('CommandCode 当前不开放续接、终端或测试命令')
    project = Path(body.get('project') or '')
    if not project.is_absolute() or not project.is_dir():
        raise ValueError('CommandCode 需要明确存在的项目绝对目录')
    opencode_worker.targets(project, body.get('files'))


def run_task(run, state, model):
    import execution_endpoints
    import model_scope
    import model_router
    settings = workflow_settings.snapshot()
    before, changed, hashes, events, result = {}, [], {}, [], {}
    status, error, answer, process = 'failed', None, '', None
    secret = model.get('api_key', '')
    started = time.monotonic()
    try:
        if state.get('engine') != 'commandcode':
            raise ValueError('不能替换任务记录的执行器')
        if state.get('route', {}).get('mode') == 'auto' and not settings.get('enabled'):
            raise ValueError('协作已关闭，自动排队任务不启动')
        execution_endpoints.gate('commandcode', model, settings)
        if not model_scope.matches(model_scope.normalize(settings.get('model_scope', 'all')), model_router.is_paid(model)):
            raise ValueError('模型已被当前全局范围排除')
        validate_task(state, settings, model)
        info = availability()
        if not info['ready']:
            raise ValueError(info['message'])
        c = configuration()
        project = Path(state['project']).resolve()
        files = opencode_worker.targets(project, state.get('files'))
        before = {str(p): opencode_worker.digest(p) for p in files}
        for i, p in enumerate(files):
            if state['kind'] == 'edit' and p.is_file():
                backup = run / 'commandcode-backups' / (str(i) + '.raw')
                backup.parent.mkdir(exist_ok=True)
                backup.write_bytes(p.read_bytes())
        registry.write_json(run / 'commandcode-before.json', before)
        job = {'task_id': state['id'], 'project': str(project), 'files': [str(p) for p in files],
               'kind': state['kind'], 'audit': str(run / 'commandcode-guard.ndjson')}
        jobfile = run / 'commandcode-job.json'
        registry.write_json(jobfile, job)
        prompt = ('模型工作台任务。项目：' + str(project) + '\n仅允许列明文件的' +
                  ('读写' if state['kind'] == 'edit' else '只读') + '：' +
                  json.dumps(job['files'], ensure_ascii=False) +
                  '\n禁止终端、目录遍历、MCP、子代理、配置与工作台记录修改。遇到工具拒绝立即停止，不换工具重试。\n\n' +
                  (run / 'prompt.txt').read_text(encoding='utf-8'))
        env = {k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ('COMMAND', 'API_KEY', 'TOKEN', 'SECRET'))}
        # Child-only home isolation; never rewrite the user's global home/config.
        private_home = run / 'commandcode-home'
        private_home.mkdir()
        env.update({'HOME': str(private_home), 'USERPROFILE': str(private_home),
                    'COMMAND_CODE_API_KEY': secret, 'MODEL_DESK_COMMANDCODE_JOB': str(jobfile),
                    'DO_NOT_TRACK': '1'})
        args = [c['node'], c['cli'], '--skip-onboarding', '--no-auto-update', '--no-session',
                '--output-format', 'json', '--model', model['model'], '--mod', str(GUARD)]
        effort=(model.get('native_reasoning') or {}).get('value','default')
        if effort!='default':args.extend(['--effort',effort])
        if state['kind'] == 'edit':
            args.append('--yolo')  # official headless write gate; native guard remains mandatory
        args.append('-p')
        registry.patch_task(run, status='running', phase='启动 CommandCode 原生客户端',
            started_at=registry.now(), worker_pid=os.getpid(), executor_version=VERSION,
            model_calls=None, native_request_count_unknown=True, billing_source='commandcode-native',
            commandcode_model=model['model'], commandcode_guard_sha256=opencode_worker.digest(GUARD),
            commandcode_cli_sha256=opencode_worker.digest(Path(c['cli'])),
            sandbox_mode='native-exact-file-tool-guard', cost_cny=None)
        with (run / 'commandcode-stderr.log').open('w', encoding='utf-8') as stderr, \
             (run / 'commandcode-stdout.ndjson').open('w', encoding='utf-8') as stdout:
            process = subprocess.Popen(args, cwd=project, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=stderr, text=True, encoding='utf-8',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            registry.patch_task(run, process_pid=process.pid, workbench_prompt_submissions=1)
            messages = queue.Queue()
            def reader():
                try:
                    for line in process.stdout:
                        messages.put(line)
                finally:
                    messages.put(None)
            threading.Thread(target=reader, daemon=True).start()
            process.stdin.write(prompt)
            process.stdin.close()
            while True:
                if (run / 'cancelled.flag').exists():
                    raise opencode_worker.Cancelled('已取消本任务的 CommandCode 进程')
                if state.get('timeout') and time.monotonic() - started > state['timeout']:
                    raise TimeoutError('达到用户明确设置的任务时限')
                try:
                    line = messages.get(timeout=.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                stdout.write(line)
                stdout.flush()
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue  # diagnostics remain raw; never fabricate an outcome
                if frame.get('type') == 'result':
                    if result:
                        raise ValueError('出现多个原生最终结果，执行身份不确定')
                    result = frame
                elif frame.get('type') == 'event':
                    events.append(frame.get('event') or {})
            process.wait(timeout=15)
        proof_path = run / 'commandcode-guard.ndjson'
        proof = [json.loads(line) for line in proof_path.read_text(encoding='utf-8').splitlines() if line]
        ready = [p for p in proof if p.get('type') == 'guard_ready']
        if (len(ready) != 1 or ready[0].get('task_id') != state['id'] or
                ready[0].get('job_sha256') != opencode_worker.digest(jobfile) or
                Path(ready[0].get('project', '')).resolve() != project or ready[0].get('kind') != state['kind']):
            raise ValueError('CommandCode 未返回本任务准确的原生范围保护加载证据')
        if any(p.get('allowed') is False for p in proof):
            raise ValueError('CommandCode 工具被范围/权限规则拒绝；不重试或换工具')
        if process.returncode or result.get('subtype') != 'success' or result.get('stopReason') != 'end_turn' or not result.get('sessionId'):
            raise ValueError('CommandCode 未正常完成：' + str(result.get('error') or result.get('subtype') or process.returncode)[:1000])
        answer = result.get('finalText') or ''
        if not answer.strip():
            raise ValueError('CommandCode 未返回验收正文')
        status = 'completed'
    except opencode_worker.Cancelled as exc:
        status, error = 'cancelled', str(exc)
    except Exception as exc:
        error = str(exc).replace(secret, '[已隐藏]') if secret else str(exc)
    finally:
        if process is not None and process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
            process.wait(timeout=15)
        for path, h in before.items():
            actual = opencode_worker.digest(Path(path))
            if actual != h:
                name = str(Path(path).relative_to(Path(state['project']).resolve()))
                changed.append(name)
                hashes[name] = actual
        if state['kind'] == 'chat' and changed:
            status, error = 'failed', '只读任务的输入出现修改，不能视为完成'
        if answer:
            (run / 'answer.txt').write_bytes(answer.encode('utf-8'))
        registry.patch_task(run, status=status, phase='执行完成，待独立验收' if status == 'completed' else 'CommandCode 执行未完成',
            ended_at=registry.now(), error=error, answer=answer, changed_files=changed, file_hashes=hashes,
            commandcode_session_id=result.get('sessionId'), commandcode_result=result,
            commandcode_events=events, usage=result.get('usage'), model_calls=None, cost_cny=None, cost_settled_cny=None,
            summary='CommandCode 原生执行结束，等待主脑独立验收；聚合用量不等于请求次数，账单未同步',
            exit_code=0 if status == 'completed' else 1)
        if status == 'completed':
            registry.update_check(model['id'], {'ok': True, 'kind': 'native_generation', 'inference': True,
                'at': registry.now(), 'message': 'CommandCode 原生任务返回成功，质量仍需独立验收'})
        delegation_policy.record_executor(run, {'completed': status == 'completed', 'test': None, 'changed_files': changed})
    return 0 if status == 'completed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    directory = registry.task_dir(args.run_id)
    task = registry.read_task(directory)
    raise SystemExit(run_task(directory, task, registry.get_model(task['model_id'])))
