"""Durable, quiet completion subscriptions bound to one project and chat.

No worker dispatch, cancellation, model call, service start or import side
effects. A hidden local process waits without a task deadline, then makes at
most one native delivery attempt for an immutable batch. Unknown delivery is
never retried automatically. Files are local workflow evidence, not proof of
executor correctness; GPT must independently inspect and accept the result.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import completion_host as desktop
import model_registry as registry
import project_storage as storage

HERE = Path(__file__).resolve().parent
WAIT_ID = re.compile(r'wait-[0-9a-f]{24}')
ACTIVE = {'queued', 'running'}
TERMINAL = {'completed', 'failed', 'cancelled'}
STOPPED = {'delivered', 'received', 'host_rejected', 'delivery_unknown', 'stopped', 'route_refused', 'version_changed'}
LABELS = {'waiting_execution': '正在等待执行模型完成', 'waiting_chat_idle': '执行已结束，等待聊天空闲后通知',
          'awaiting_review': '执行已结束，待主脑独立验收', 'reviewing': '执行已结束，主脑正在独立验收',
          'accepted': '任务已完成，独立验收通过', 'returned': '已退回整改',
          'needs_attention': '需要处理，请检查任务与通知状态', 'stopped': '自动通知已暂停，执行任务未被取消'}


def now():
    return datetime.now(timezone.utc).isoformat()


def same_path(a, b):
    if not a or not b:
        return False
    try:
        if not Path(a).is_absolute() or not Path(b).is_absolute():
            return False
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (TypeError, ValueError, OSError):
        return False


def _strip_windows_prefix(text):
    """Undo the extended-length/device prefix of a Windows resolved path."""
    if text.startswith('\\\\?\\UNC\\'):
        return '\\\\' + text[8:]
    for prefix in ('\\\\?\\', '\\\\.\\'):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _containment(path):
    """Canonical path for a containment comparison.

    ``Path.resolve()`` can return an extended-length ``\\\\?\\`` path on Windows
    while the project came from ``os.path.realpath`` without that prefix, which
    made the same project look like a different one after a directory appeared.
    Strip that representation prefix and compare case-insensitively on Windows.
    """
    text = str(Path(path).resolve())
    if os.name == 'nt':
        text = _strip_windows_prefix(text)
    return Path(os.path.normcase(text))


def safe_child(project, *parts):
    project = Path(storage.normalize(project))
    current = project
    for part in ('.model-desk', *parts):
        current /= part
        if storage._is_link(current):
            raise ValueError('通知记录路径不允许链接或联接')
    if not _containment(current).is_relative_to(_containment(project)):
        raise ValueError('通知记录越出所属项目')
    return current


def paths(project, wait_id):
    if not WAIT_ID.fullmatch(wait_id):
        raise ValueError('通知编号无效')
    root = safe_child(project, 'notifications', wait_id)
    for name in ('subscription.json', 'state.lock', 'listener.lock', 'listener.log', 'send.claim', 'stop.flag'):
        if storage._is_link(root / name):
            raise ValueError('通知文件不允许链接或联接')
    return root


def task_record(project, task_id):
    if not storage.TASK_ID.fullmatch(task_id):
        raise ValueError('任务编号无效')
    run = safe_child(project, 'runs', task_id)
    safe_child(project, 'runs', task_id, 'task.json')
    safe_child(project, 'runs', task_id, 'task.lock')
    task = registry.read_task(run)
    if task.get('id') != task_id or not same_path(task.get('project', ''), project):
        raise ValueError('任务与当前项目身份不一致')
    return task


def digest():
    return {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
            for name in ('completion_wait.py', 'desktop_notify.py','completion_host.py','dsh_notify.py')}


def owned_task(data,task_id):
    task=task_record(data['project'],task_id)
    if data.get('host','codex')=='dsh' and any((task.get('brain') or {}).get(key)!=data.get(key)
                                               for key in ('owner','binding_key')):
        raise ValueError('任务原主脑责任绑定已变化，拒绝通知或改写记录')
    return task


def preflight(project):
    project = storage.normalize(project)
    # The CLI is invoked in the calling project, even when the integration is
    # installed elsewhere. Never fall back to the service's directory/history.
    if not same_path(project, Path.cwd()):
        raise ValueError('后台通知项目必须是调用聊天的当前绝对目录')
    identity = desktop.calling_thread()
    observed = desktop.read_current()
    if observed.get('thread_id') != identity or not same_path(observed.get('project', ''), project):
        raise ValueError('桌面聊天与项目绑定未确认，未启用后台通知')
    if not observed.get('turns'):
        raise ValueError('桌面回合身份未确认，未启用后台通知')
    return {'project': project, 'thread_id': identity, 'control_turn_id': observed['turns'][0]['id'],
            'host':desktop.host_id(),**desktop.authorization_context()}


def load(project, wait_id):
    data = registry.read_json(paths(project, wait_id) / 'subscription.json')
    if data.get('id') != wait_id or not same_path(data.get('project', ''), project):
        raise ValueError('通知记录身份不一致')
    if data.get('thread_id') != desktop.calling_thread():
        raise ValueError('仅原聊天可以操作其通知')
    if data.get('host','codex')!=desktop.host_id():
        raise ValueError('通知属于另一个宿主；不会回退投递')
    if desktop.host_id()=='dsh' and any(data.get(key)!=value for key,value in desktop.authorization_context().items()):
        raise ValueError('DSH 通知责任归属已变化')
    task_ids = data.get('task_ids', [])
    if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 32:
        raise ValueError('通知批次身份无效')
    if any(not isinstance(t, str) or not storage.TASK_ID.fullmatch(t) for t in task_ids):
        raise ValueError('通知任务身份无效')
    if task_ids != sorted(set(task_ids)):
        raise ValueError('通知批次身份无效')
    if batch_id(project, data['thread_id'], task_ids) != wait_id:
        raise ValueError('通知批次身份已改变')
    return data


def batch_id(project, identity, task_ids):
    parts=[storage.key(project), identity, sorted(task_ids)]
    if desktop.host_id()!='codex':parts.insert(0,desktop.host_id())
    key = json.dumps(parts, separators=(',', ':'))
    return 'wait-' + hashlib.sha256(key.encode()).hexdigest()[:24]


def save(data):
    data['updated_at'] = now()
    registry.write_json(paths(data['project'], data['id']) / 'subscription.json', data)


def project_status(data):
    """Small task fields exposed by the existing panel; no service reload."""
    value = {key: data.get(key) for key in ('id', 'phase', 'listener_status', 'heartbeat_at', 'updated_at', 'send_attempts')}
    value['label'] = LABELS.get(data.get('phase'), LABELS['needs_attention'])
    for task_id in data['task_ids']:
        try:
            task = owned_task(data,task_id)  # Never patch another responsibility.
            if (task.get('completion_wait') or {}).get('id') not in {None, data['id']}:
                continue
            registry.patch_task(safe_child(data['project'], 'runs', task_id), completion_wait=value)
        except (OSError, ValueError, RuntimeError):
            # A missing/deleted record must not prevent an attention event or
            # recreate that run just to display a notification badge.
            continue


def evaluate(tasks):
    states = [task.get('status') for task in tasks]
    if any(state == 'recovery_pending' for state in states):
        return 'needs_attention'
    if any(state not in ACTIVE | TERMINAL for state in states):
        raise ValueError('任务状态未确认，不能当作执行完成')
    if any(state in ACTIVE for state in states):
        return None
    if any(not task.get('ended_at') for task in tasks):
        raise ValueError('执行结束记录不完整，不能当作执行完成')
    return 'awaiting_review' if all(state == 'completed' for state in states) else 'needs_attention'


def _start(data):
    root = paths(data['project'], data['id'])
    with (root / 'listener.log').open('ab', buffering=0) as log:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--project', data['project'],
                                  '--run', data['id']], cwd=data['project'], env=desktop.child_environment(),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                                 **desktop.hidden_options())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = load(data['project'], data['id'])
        if current.get('listener_status') != 'starting':
            return current
        if child.poll() is not None:
            break
        time.sleep(.1)
    # The worker task is unaffected. The caller must not announce successful
    # waiting; attach to the original task or explicitly resume this listener.
    raise ValueError('后台监听启动未确认；任务没有重派或取消，请按原任务编号检查')


def arm(project, task_ids, context=None):
    context = context or preflight(project)
    project = storage.normalize(project)
    if not same_path(context['project'], project) or context['thread_id'] != desktop.calling_thread():
        raise ValueError('通知调用身份不一致')
    if context.get('host','codex')!=desktop.host_id():raise ValueError('通知宿主不一致')
    authorization=desktop.authorization_context()
    if any(context.get(key)!=value for key,value in authorization.items()):
        raise ValueError('通知责任绑定不一致')
    task_ids = sorted(set(task_ids))
    if not 1 <= len(task_ids) <= 32:
        raise ValueError('一次通知需要 1 至 32 个明确的任务编号')
    wait_id = batch_id(project, context['thread_id'], task_ids)
    root = paths(project, wait_id)
    root.mkdir(parents=True, exist_ok=True)
    with registry.file_lock(safe_child(project, 'notifications', 'enroll.lock')):
        if (root / 'subscription.json').exists():
            existing = load(project, wait_id)
            return {**existing, 'existing': True}  # Never start a duplicate watcher.
        for task_id in task_ids:
            task = task_record(project, task_id)
            if any((task.get('brain') or {}).get(key)!=value for key,value in authorization.items()):
                raise ValueError('只能订阅原主脑及责任会话的任务；历史只读不授予通知权限')
            owner = (task.get('completion_wait') or {}).get('id')
            if owner and owner != wait_id:
                raise ValueError('任务已经属于另一通知批次，拒绝重复唤醒')
        data = {'schema': 1, 'id': wait_id, 'project': project, 'thread_id': context['thread_id'],
                'host':desktop.host_id(),**authorization,
                'control_turn_id': context['control_turn_id'], 'task_ids': task_ids,
                'created_at': now(), 'phase': 'waiting_execution', 'listener_status': 'starting',
                'send_attempts': 0, 'source_hashes': digest(), 'automatic_retry': False}
        save(data)
        project_status(data)
    return _start(data)


def fixed_message(data):
    # Never include task prompts, answers, logs or executor claims as commands.
    reference = json.dumps({'project': data['project'], 'wait_id': data['id'], 'task_ids': data['task_ids']},
                           ensure_ascii=False)
    return ('[Model Desk completion event] ' + reference + '. '
            'This is the user-authorized completion subscription for this original chat, not a human message. '
            'Follow AGENTS.md and check the actual project/task/subscription records. '
            'First tell the user in Chinese that execution ended and independent review is starting, '
            'or accurately report the recorded failure/recovery condition. The user can send messages. '
            'Acknowledge receipt with completion_wait.py --project <project> --received <wait_id> '
            'from that project directory; inspect the compact delivery packet once and independently '
            'verify actual changes and each acceptance criterion. Executor completed is not acceptance. '
            'Record the real review with review_task.py. Never redispatch a task just because notification '
            'or delivery is unknown; preserve cancellation, recovery, stage dependencies and the one-rework limit. '
            'Do not send another notification or start another chat. Do not change model, budget or host permissions. '
            'Respect any newer human instructions; report completed only after independent acceptance.')


def _finish(data, status, phase=None):
    root = paths(data['project'], data['id'])
    with registry.file_lock(root / 'state.lock'):
        latest = load(data['project'], data['id'])
        if latest.get('receipt_at') and status != 'stopped':
            return latest
        data['listener_status'] = status
        if phase:
            data['phase'] = phase
        data['finished_at'] = now()
        save(data)
    project_status(data)
    return data


def run(project, wait_id, *, sleeper=time.sleep, poll_seconds=1):
    data = load(project, wait_id)
    root = paths(project, wait_id)
    # Held for the entire watcher lifetime; the OS releases it on exit. An
    # explicit resume can acquire it after a crash, without removing send.claim.
    with registry.file_lock(root / 'listener.lock'):
        data = load(project, wait_id)
        if (root / 'send.claim').exists() or data['listener_status'] in STOPPED:
            return data
        data.update(listener_status='listening', watcher_pid=os.getpid(), heartbeat_at=now())
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32')
            kernel.GetConsoleWindow.restype = ctypes.c_void_p
            data['console_window_present'] = bool(kernel.GetConsoleWindow())
        save(data)
        project_status(data)
        last_projection = time.monotonic()
        while True:
            if (root / 'stop.flag').exists():
                return _finish(data, 'stopped', 'stopped')
            if data['source_hashes'] != digest():
                return _finish(data, 'version_changed', 'needs_attention')
            data['heartbeat_at'] = now()
            try:
                tasks = [owned_task(data,task_id) for task_id in data['task_ids']]
                ready = evaluate(tasks)
            except (OSError, ValueError, RuntimeError):
                # Bounded local record retries, then one attention event. Do not
                # mistake an unreadable/deleted record for successful execution.
                data['record_read_errors'] = data.get('record_read_errors', 0) + 1
                save(data)
                if data['record_read_errors'] < 5:
                    sleeper(poll_seconds)
                    continue
                ready = 'needs_attention'
            else:
                data['record_read_errors'] = 0
            if ready is None:
                data.update(phase='waiting_execution', listener_status='listening')
                save(data)
                if time.monotonic() - last_projection >= 5:
                    project_status(data)
                    last_projection = time.monotonic()
                sleeper(poll_seconds)
                continue
            data['phase'] = ready
            save(data)
            try:
                observed = desktop.read_current()
                if observed.get('thread_id') != data['thread_id'] or not same_path(observed.get('project', ''), project):
                    return _finish(data, 'route_refused', 'needs_attention')
                turns = observed.get('turns', [])
                if not turns or turns[0].get('status') not in {'completed', 'interrupted', 'failed'}:
                    data.update(phase='waiting_chat_idle', listener_status='waiting_chat_idle')
                    save(data)
                    project_status(data)
                    sleeper(poll_seconds)
                    continue
                # Recheck cancellation/notification stop and source integrity
                # after the native read, before the irreversible send claim.
                if (root / 'stop.flag').exists():
                    return _finish(data, 'stopped', 'stopped')
                if data['source_hashes'] != digest():
                    return _finish(data, 'version_changed', 'needs_attention')
                try:
                    ready_now = evaluate([owned_task(data,task_id) for task_id in data['task_ids']])
                except (OSError, ValueError, RuntimeError):
                    ready_now = 'needs_attention'
                if ready_now is None:
                    sleeper(poll_seconds)
                    continue
                ready = ready_now
                data['phase'] = ready
                with registry.file_lock(root / 'state.lock'):
                    latest = load(project, wait_id)
                    if (root / 'stop.flag').exists() or latest.get('listener_status') == 'stopped':
                        latest.update(listener_status='stopped', phase='stopped')
                        save(latest)
                        project_status(latest)
                        return latest
                    if latest.get('receipt_at') or (root / 'send.claim').exists():
                        return latest
                    with (root / 'send.claim').open('x', encoding='utf-8') as claim:
                        claim.write(wait_id)
                        claim.flush()
                        os.fsync(claim.fileno())
                    data.update(listener_status='delivery_attempted', send_attempts=1,
                                delivery_started_at=now(), last_idle_turn=turns[0])
                    save(data)
                project_status(data)
                response = desktop.send_current(data['thread_id'], fixed_message(data), 'model-desk-' + wait_id)
                return _finish(data, 'delivered' if response.get('host_accepted') else 'host_rejected',
                               ready if response.get('host_accepted') else 'needs_attention')
            except Exception:
                # Ambiguous send is not safe to retry. A failed read is also
                # visible locally; it never changes execution or acceptance.
                data['error'] = '通知状态未确认，请检查原任务；未自动重发或重新派发'
                return _finish(data, 'delivery_unknown', 'needs_attention')


def received(project, wait_id):
    data = load(project, wait_id)
    root = paths(project, wait_id)
    with registry.file_lock(root / 'state.lock'):
        data = load(project, wait_id)
        if not (root / 'send.claim').exists() or data.get('send_attempts') != 1:
            raise ValueError('没有匹配的投递尝试，不能登记接收')
        try:
            result = evaluate([owned_task(data,task_id) for task_id in data['task_ids']])
        except (OSError, ValueError, RuntimeError):
            result = 'needs_attention'
        data.update(receipt_at=now(), listener_status='received',
                    phase='reviewing' if result == 'awaiting_review' else 'needs_attention')
        save(data)
        project_status(data)
    return data


def status(project, wait_id):
    data = load(project, wait_id)
    tasks = [owned_task(data,task_id) for task_id in data['task_ids']]
    import delegation_policy
    reviews = [delegation_policy.load(safe_child(project, 'runs', t['id'])) for t in tasks]
    if reviews and all(review and review.get('state') in {'accepted', 'final_accepted'} for review in reviews):
        data['phase'] = 'accepted'
    elif any(review and review.get('state') == 'returned' for review in reviews):
        data['phase'] = 'returned'
    data['label'] = LABELS.get(data['phase'], LABELS['needs_attention'])
    return data


def process_alive(pid):
    """Conservative three-state liveness; never signal or kill a process."""
    if not isinstance(pid, int) or pid <= 0 or os.name != 'nt':
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        code = ctypes.c_ulong()
        return code.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None
    finally:
        kernel.CloseHandle(handle)


def resume(project, wait_id):
    preflight(project)
    root = paths(project, wait_id)
    with registry.file_lock(root / 'state.lock'):
        data = load(project, wait_id)
        if (root / 'send.claim').exists() or data['listener_status'] in STOPPED:
            raise ValueError('已经投递、结果未知或已停止的通知不允许自动重发')
        if process_alive(data.get('watcher_pid')) is not False:
            raise ValueError('原监听仍在运行或进程身份未确认，未重复启动')
        if data['source_hashes'] != digest():
            raise ValueError('通知程序已变更，需要核对原任务，不自动重启旧订阅')
        data['listener_status'] = 'starting'
        save(data)
    return _start(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--tasks', nargs='+', help='登记一个批次并启动隐藏监听，不派发任务')
    operation.add_argument('--run', help=argparse.SUPPRESS)
    operation.add_argument('--status')
    operation.add_argument('--received')
    operation.add_argument('--stop', help='仅暂停通知，不取消执行任务')
    operation.add_argument('--resume', help='显式恢复原监听；投递过或结果未知时不重发')
    args = parser.parse_args()
    if args.tasks:
        result = arm(args.project, args.tasks)
    elif args.run:
        result = run(args.project, args.run)
    elif args.received:
        result = received(args.project, args.received)
    elif args.stop:
        result = load(args.project, args.stop)
        (paths(args.project, args.stop) / 'stop.flag').touch()
        result = _finish(result, 'stopped', 'stopped')
    elif args.resume:
        result = resume(args.project, args.resume)
    else:
        result = status(args.project, args.status)
    # The native pipe stays in the child environment. No host secrets are saved.
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError):
        print('后台通知操作未确认；执行任务没有重派或取消，请检查原任务与通知记录', file=sys.stderr)
        raise SystemExit(2)
