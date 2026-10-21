"""Submit Codex work to the same queue and session history as the model panel."""
import argparse
import json
import re
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlparse

import delegation_policy as policy
import model_registry as registry
import record_protection
import project_scope

HERE = Path(__file__).resolve().parent

# A transient unreadable record must never be mistaken for a finished task and
# never triggers a re-dispatch: the controller retries a bounded number of times
# and then fails with an explicit timeout error.
TASK_READ_ATTEMPTS = 20
TASK_READ_DELAY = 0.25


def read_task_state(request, task_id):
    """Bounded read of one real task record.

    The panel can momentarily skip a task whose ``task.json`` is being replaced;
    a single ``{"task": null}`` used to crash the wait loop with an
    ``AttributeError``.  Retry a bounded number of times, then raise a clear
    timeout error.  Never dispatch again.
    """
    payload = None
    for _ in range(TASK_READ_ATTEMPTS):
        payload = request('/api/tasks/' + task_id)
        task = payload.get('task') if isinstance(payload, dict) else None
        if isinstance(task, dict) and task.get('id') == task_id:
            return task
        time.sleep(TASK_READ_DELAY)
    detail = payload.get('error') if isinstance(payload, dict) else payload
    raise ValueError(f'读取任务 {task_id} 状态超时（已重试 {TASK_READ_ATTEMPTS} 次，未重新派发）：{detail}')


def wait_interrupted(task_id, error):
    print(f'等待任务 {task_id} 中断：{error}。执行任务未被取消或重新派发；'
          f'恢复等待：dispatch_task.py --wait-task {task_id}', file=sys.stderr)


def packet_for(task):
    """Best-effort compact delivery packet; a broken record must not break the waiter."""
    task_id = task.get('id') if isinstance(task, dict) else None
    try:
        import workflow_assist
        return workflow_assist.delivery_packet(task_id, (task or {}).get('project'), task=task)
    except Exception as error:  # best-effort summary only; real state stays in task.json
        return {'schema': 1, 'task_id': task_id, 'status': 'error',
                'errors': ['交付包生成失败：' + str(error)], 'read_only': True}


def wait_for_task(request, task_id, progress=False, verbose=False):
    """Poll locally until terminal; default output is the compact final result."""
    last_phase = None
    try:
        while True:
            task = read_task_state(request, task_id)
            if task['status'] not in {'queued', 'running'}:
                result = {k: task.get(k) for k in ('id', 'status', 'model', 'engine', 'summary', 'error', 'changed_files', 'test_result', 'dsh_session_id', 'pi_session_id')}
                if not verbose:
                    result['summary'] = str(result.get('summary') or '')[:1200]
                    if isinstance(result.get('test_result'), dict):
                        result['test_result'] = {'exit_code': result['test_result'].get('exit_code'), 'output': str(result['test_result'].get('output') or '')[-800:]}
                    result['review'] = task.get('review')
                    result['model_calls'] = task.get('model_calls')
                    result['packet'] = packet_for(task)
                if task['status'] == 'recovery_pending':
                    # Not completed and not started again: show the explicit,
                    # inspectable manual recovery entry instead of a silent 1.
                    result['recovery'] = {
                        'status': 'recovery_pending', 'auto_resend': False, 'creates_new_record': False,
                        'reason': (task.get('recovery') or {}).get('reason') if isinstance(task.get('recovery'), dict) else None,
                        'resume': f'POST /api/tasks/{task_id}/recover  {{"confirm": true}}',
                        'inspect': f'dispatch_task.py --wait-task {task_id}'}
                    print(f'任务 {task_id} 待恢复：执行未完成且不会自动重发；'
                          f'核对改动后可按原任务记录显式恢复（{result["recovery"]["resume"]}）。',
                          file=sys.stderr)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return 0 if task['status'] == 'completed' else 1
            phase = task.get('phase') or ''
            phase = '执行 Agent 处理中' if phase.startswith('工具：') else phase
            if progress and (task['status'], phase) != last_phase:
                last_phase = (task['status'], phase)
                print(json.dumps({'task_id': task_id, 'status': task['status'], 'phase': phase}, ensure_ascii=False), flush=True)
            time.sleep(1)
    except (OSError, ValueError) as error:
        wait_interrupted(task_id, error)
        return 2
    except KeyboardInterrupt:
        wait_interrupted(task_id, '用户停止等待')
        return 130


def request_view(path, body=None):
    """Bind explicit CLI work and known records without selecting all projects."""
    project=(body or {}).get('project') if isinstance(body,dict) else None
    if not project:
        match=re.fullmatch(r'/api/(tasks|stages|goals)/([A-Za-z0-9_-]+)(?:/[^/]*)?',path)
        if match:
            kind,record_id=match.groups()
            if kind=='tasks':
                row=registry.read_task(registry.task_dir(record_id))
            else:
                import stage_store
                row=(stage_store.get_stage(record_id) if kind=='stages' else stage_store.get_goal(record_id)) or {}
            project=row.get('project')
        elif isinstance(body,dict) and (body.get('continue_task') or body.get('rework_of')):
            row=registry.read_task(registry.task_dir(body.get('continue_task') or body['rework_of']))
            project=row.get('project')
    return project_scope.parse_view({'mode':'project','project':project} if project else None)


def connect_panel():
    runtime = json.loads((HERE / 'panel-runtime.json').read_text(encoding='utf-8'))
    base = runtime['url']
    address = urlparse(base)
    if address.scheme != 'http' or address.hostname != '127.0.0.1' or address.username or address.password or address.query or address.fragment:
        raise ValueError('工作台地址必须是本机回环 HTTP 服务')
    opener = build_opener(ProxyHandler({}))
    token = ''

    def request(path, body=None):
        req = Request(base + path, data=json.dumps(body).encode('utf-8') if body is not None else None,
                      headers={'Content-Type': 'application/json', 'X-Panel-Token': token,
                               'X-Model-Desk-View':project_scope.encode_view_header(request_view(path,body))})
        try:
            with opener.open(req, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            raise ValueError(json.load(error).get('error', str(error))) from None

    token = request('/api/session')['token']
    return request


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-id')
    parser.add_argument('--route', choices=['auto', 'manual'])
    parser.add_argument('--routing-file', type=Path, help='UTF-8 JSON: category, complexity, input_tokens, output_tokens, expected_calls, eligible_model_ids, assessment, acceptance')
    parser.add_argument('--preview', action='store_true', help='Only evaluate the route; never call a model')
    parser.add_argument('--engine', choices=['dsh', 'pi', 'workbuddy', 'aider', 'direct'], default=None,
                        help='本次显式覆盖；省略时按所选模型的默认 Agent，其次全局默认')
    parser.add_argument('--kind', choices=['chat', 'edit'], default='edit')
    parser.add_argument('--project')
    parser.add_argument('--files', nargs='+', default=[])
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--task-file', type=Path)
    operation.add_argument('--wait-task', help='只读等待已有任务 ID，不创建或重新派发')
    parser.add_argument('--title')
    parser.add_argument('--test-cmd')
    parser.add_argument('--timeout', type=int, default=0, help='可选任务总时限/秒；默认0表示不限')
    parser.add_argument('--continue-task')
    parser.add_argument('--rework-of', help='退回整改的任务 ID：保持原 Agent/模型/项目/文件范围，默认新会话')
    parser.add_argument('--reuse-session', action='store_true', help='例外：整改或续接时显式复用原 Agent 会话（默认不继承）')
    parser.add_argument('--stage', help='派发一个持久化阶段；项目/文件范围/候选模型来自阶段记录')
    parser.add_argument('--request-output-tokens', type=int, help='可选的任务级单请求输出额度，不修改模型 max_output 预设')
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--notify-on-completion', action='store_true',
                        help='隐藏后台监听终态并在原聊天通知一次；与 --wait 互斥，不设置任务总时限')
    parser.add_argument('--progress', action='store_true', help='等待时显式输出阶段变化；默认仅提交回执和最终结果')
    parser.add_argument('--verbose-result', action='store_true', help='Return the original long result; default is a compact acceptance brief')
    args = parser.parse_args()
    if args.notify_on_completion and (args.wait or args.wait_task or args.progress or args.verbose_result or args.preview):
        parser.error('--notify-on-completion 不能与同步等待、附着、进度或预览混用')
    if args.progress and not (args.wait or args.wait_task):
        parser.error('--progress 需要 --wait 或 --wait-task')
    if args.wait_task:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', args.wait_task):
            parser.error('--wait-task 需要有效的原任务 ID')
        if any((args.model_id, args.route, args.routing_file, args.preview, args.project, args.files,
                args.title, args.test_cmd, args.timeout, args.continue_task, args.rework_of,
                args.reuse_session, args.stage, args.request_output_tokens,
                args.engine is not None, args.kind != 'edit')):
            parser.error('--wait-task 不能与新分派、路由、续接或整改参数混用')
        print(json.dumps({'task_id': args.wait_task, 'attached': True}, ensure_ascii=False), flush=True)
        try:
            request = connect_panel()
        except (OSError, ValueError) as error:
            wait_interrupted(args.wait_task, error)
            return 2
        return wait_for_task(request, args.wait_task, args.progress, args.verbose_result)
    if args.rework_of and args.continue_task:
        raise ValueError('--rework-of 与 --continue-task 不能同时使用；整改默认新会话，续接请单独使用 --continue-task')
    if args.reuse_session and not (args.rework_of or args.continue_task):
        raise ValueError('--reuse-session 只能与 --rework-of 或 --continue-task 一起使用')
    if args.stage and (args.rework_of or args.continue_task):
        raise ValueError('阶段派发不能与续接或整改同时使用')
    if args.request_output_tokens is not None and not 1 <= args.request_output_tokens <= 131072:
        raise ValueError('--request-output-tokens 需要在 1 到 131072 之间')
    notification_context = None
    if args.notify_on_completion:
        if not args.project:
            parser.error('--notify-on-completion 需要调用项目的明确绝对目录 --project')
        import completion_wait
        notification_context = completion_wait.preflight(args.project)
    request = connect_panel()
    prompt_text = args.task_file.read_text(encoding='utf-8-sig')
    rework_record = None
    if args.rework_of:
        rework_run = registry.task_dir(args.rework_of)
        if not rework_run.is_dir():
            raise ValueError('整改原任务不存在')
        rework_record = policy.load(rework_run)
        if rework_record is None:
            raise ValueError('这个任务没有验收记录，不能派发整改')
        if rework_record.get('state') != policy.RETURNED:
            raise ValueError('任务当前不是退回整改状态，不能派发整改')
        if (rework_record.get('rework') or {}).get('dispatched_task_id'):
            raise ValueError('整改已经派发为任务 ' + str(rework_record['rework']['dispatched_task_id']))
        deliverable = json.loads((rework_run / 'task.json').read_text(encoding='utf-8'))
        if notification_context and not completion_wait.same_path(deliverable.get('project'), notification_context['project']):
            raise ValueError('整改任务与当前通知项目不一致，未重新派发')
        # Default: a new session carrying the deliverable, failure evidence and
        # correction scope.  --reuse-session is the explicit exception.
        prompt_text = policy.rework_prompt(rework_record, deliverable=deliverable,
                                           new_session=not args.reuse_session) + '\n\n主脑补充说明：\n' + prompt_text
    body = {'model_id': args.model_id, 'engine': args.engine, 'kind': args.kind, 'project': args.project or '',
            'files': args.files, 'prompt': prompt_text, 'source': 'codex',
            'title': args.title, 'timeout': args.timeout, 'test_command': args.test_cmd or '',
            'continue_task': args.continue_task or None, 'rework_of': args.rework_of or None,
            'reuse_session': bool(args.reuse_session), 'stage_id': args.stage or None,
            'request_output_tokens': args.request_output_tokens}
    if args.engine is None:
        body.pop('engine')  # The service resolves its saved default once, at submission.
    if args.stage:
        body['route_mode'] = args.route or ('manual' if args.model_id else 'auto')
    elif args.rework_of:
        body['route_mode'] = 'manual'
    else:
        body['route_mode'] = args.route or ('manual' if args.model_id or args.continue_task else 'auto')
    if args.routing_file:
        body['routing'] = json.loads(args.routing_file.read_text(encoding='utf-8-sig'))
    elif args.rework_of:
        body['routing'] = deliverable.get('route', {}).get('requirements', {})
    if args.continue_task:
        parent = read_task_state(request, args.continue_task)
        if args.engine is not None and args.engine != parent.get('engine'):
            raise ValueError('继续会话不能切换执行 Agent；请创建新任务')
        for field in ('model_id', 'engine', 'kind', 'project', 'files'):
            body[field] = parent[field]
        if 'routing' not in body:
            body['routing'] = parent.get('route', {}).get('requirements', {})
    if body.get('kind') == 'edit':
        # Fail here with a precise message instead of relying only on the panel:
        # the workbench record directory is never a writable dispatch target.
        record_protection.validate_files(body.get('files') or [], body.get('project'))
    if notification_context:
        if not completion_wait.same_path(body.get('project'), notification_context['project']):
            raise ValueError('任务与当前通知项目不一致，未派发')
        if args.stage:
            bound_stage = request('/api/stages/' + args.stage)
            if not completion_wait.same_path(bound_stage.get('project'), notification_context['project']):
                raise ValueError('阶段与当前通知项目不一致，未派发')
    if args.preview:
        if args.stage:
            stage = request('/api/stages/' + args.stage)
            body['project'] = stage.get('project')
            body['files'] = stage.get('files')
            if stage.get('fixed_model'):
                body['model_id'] = stage['fixed_model']
                body['route_mode'] = 'manual'
        print(json.dumps(request('/api/route-preview', body), ensure_ascii=False))
        return 0
    if args.stage:
        # Atomic claim + shared queue happen inside the panel; nothing is retried.
        task = request('/api/stages/' + args.stage + '/dispatch', body)['task']
    else:
        task = request('/api/tasks', body)
    route = task.get('route') or {}
    print(json.dumps({'task_id': task['id'], 'status': task['status'], 'engine': task['engine'], 'model': task['model'],
                      'route': {k: route.get(k) for k in ('mode', 'reason', 'estimated_cost_cny', 'budget_enforced')}}, ensure_ascii=False), flush=True)
    if args.notify_on_completion:
        try:
            subscription = completion_wait.arm(args.project, [task['id']], context=notification_context)
            waiting = subscription.get('listener_status') in {'listening', 'waiting_chat_idle'}
            print(json.dumps({'task_id': task['id'], 'wait_id': subscription['id'], 'waiting': waiting,
                              'label': completion_wait.LABELS.get(subscription['phase']),
                              'background_notification': True,
                              'message': '正在等待执行模型完成，结束后在本聊天通知并独立验收' if waiting else '请核对原任务通知状态'},
                             ensure_ascii=False), flush=True)
            return 0 if waiting else 2
        except (OSError, ValueError, RuntimeError):
            print(json.dumps({'task_id': task['id'], 'waiting': False, 'redispatched': False,
                              'error': '后台监听未确认；原任务继续执行，请按原任务 ID 检查或附着等待'},
                             ensure_ascii=False), flush=True)
            return 2
    if not args.wait:
        return 0
    return wait_for_task(request, task['id'], args.progress, args.verbose_result)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
