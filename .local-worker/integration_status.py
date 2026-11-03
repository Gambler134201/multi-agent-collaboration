"""Bounded, project-local integration metadata for read-only presentation."""
import json
import re
from pathlib import Path
from task_workspace import NAME

COMMIT = re.compile(r'^[0-9a-f]{40,64}$')
TASK_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
LIMIT = 256


def read_project(project):
    """Never initialize a project/store, run Git, follow record links or write."""
    snapshot = {'records': [], 'available': False}
    if not isinstance(project, str) or not Path(project).is_absolute():
        return snapshot
    try:
        root = Path(project).resolve()
        folder = root / '.model-desk' / 'integrations'
        if folder.resolve() != folder:
            return snapshot
        snapshot['project'] = str(root)
        if not folder.exists():
            snapshot['available'] = True
            return snapshot
        files = []
        for file in folder.iterdir():
            if file.suffix != '.json':
                continue
            files.append(file)
            if len(files) > LIMIT:
                return snapshot
        for file in sorted(files):
            if (file.is_symlink() or file.resolve() != file or not file.is_file()
                    or file.stat().st_nlink != 1 or file.stat().st_size > 1_000_000):
                return snapshot
            record = json.loads(file.read_text(encoding='utf-8'))
            if (not isinstance(record, dict) or type(record.get('schema')) is not int or record.get('schema') != 1
                    or record.get('kind') != 'integration-acceptance'
                    or record.get('name') != file.stem or not NAME.fullmatch(file.stem)
                    or not isinstance(record.get('project'), str)
                    or not Path(record['project']).is_absolute()
                    or Path(record['project']).resolve() != root):
                return snapshot
            snapshot['records'].append(record)
        snapshot['available'] = True
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    return snapshot


def for_task(snapshot, task):
    """Only current frozen inputs match; never infer a not-yet-created queue."""
    if task.get('kind') == 'chat':
        return {'state': 'not_applicable', 'records': [], 'detail': '咨询任务无需代码集成。'}
    if not snapshot.get('available'):
        return {'state': 'unavailable', 'records': [], 'detail': '集成记录无法完整核对。'}
    task_id = task.get('id')
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        return {'state': 'unavailable', 'records': [], 'detail': '缺少可核对的任务身份。'}
    workspace = task.get('workspace') if isinstance(task.get('workspace'), dict) else {}
    commit = workspace.get('commit')
    rows = []
    stale = False
    for record in snapshot['records']:
        ids = record.get('task_ids')
        if not isinstance(ids, list) or task_id not in ids:
            continue
        commits = record.get('commits')
        if (not isinstance(commits, dict) or not isinstance(commit, str)
                or not COMMIT.fullmatch(commit) or commits.get(task_id) != commit
                or record.get('goal_id') != task.get('goal_id')):
            stale = True
            continue
        verdict = record.get('verdict')
        integrated = record.get('integrated_commit')
        accepted = record.get('accepted')
        if verdict == 'passed' and accepted is False and isinstance(integrated, str) and COMMIT.fullmatch(integrated):
            state = 'awaiting_review'
        elif (verdict == 'passed' and accepted is True and isinstance(integrated, str)
              and COMMIT.fullmatch(integrated) and record.get('accepted_commit') == integrated):
            state = 'accepted'
        elif verdict == 'merge_conflict' and accepted is False:
            state = 'conflict'
        elif verdict == 'check_failed' and accepted is False:
            state = 'check_failed'
        elif verdict is None and accepted is False and isinstance(record.get('queue'),dict):
            job=record['queue'];code=job.get('state')
            if type(job.get('schema')) is not int or job['schema']!=1 or not isinstance(code,str) or code not in {'queued','preparing','merging','checking','blocked','recovery_required','cancelled'}:
                return {'state':'unavailable','records':[],'detail':'集成排队状态缺失或不一致。'}
            import integration_queue
            try:integration_queue._verify_job(record)
            except (ValueError,TypeError,KeyError):
                return {'state':'unavailable','records':[],'detail':'集成意图或冻结输入无法核对。'}
            code=integration_queue.observe(record)['state']
            state={'preparing':'running','merging':'running','checking':'running'}.get(code,code)
        else:
            return {'state': 'unavailable', 'records': [], 'detail': '集成记录的结论存在矛盾或缺失。'}
        rows.append({'name': record['name'], 'state': state})
    if stale:
        return {'state': 'stale', 'records': rows, 'detail': '集成输入与当前任务的冻结成果或目标不一致。'}
    states = {row['state'] for row in rows}
    state = next(iter(states)) if len(states) == 1 else 'multiple' if states else 'none'
    details = {'none': '尚未记录集成请求；不等于正在排队。',
        'awaiting_review': '隔离副本的集成检查已通过，等待主脑独立验收集成。',
        'accepted': '集成提交已独立验收；原项目并未自动更新。',
        'conflict': '隔离副本存在合并冲突，原任务成果仍保留。',
        'check_failed': '集成检查未通过，不等于各任务执行失败。',
        'multiple': '本任务参与多份集成且结论不同，需要分别查看。'}
    details.update(queued='已保存原集成计划，尚未开始。',running='原集成正在准备、合并或检查，尚未验收。',
        blocked='集成准备受阻，原任务成果保留。',recovery_required='原集成结果待核对，不自动重跑或释放占用。',
        cancelled='排队集成已取消，原任务成果保留。')
    return {'state': state, 'records': rows, 'detail': details[state]}
