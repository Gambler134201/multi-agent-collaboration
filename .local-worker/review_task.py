"""Read and update outcome-delegation acceptance records.

This is the operable entry for the main mind. It only reads and writes local
task records: no model is called and no budget is spent.

Examples (venv Python, from the .local-worker directory or with full paths):

    review_task.py --task 20260101-120000-ab12cd
    review_task.py --task <id> --init --criteria-file acceptance.txt
    review_task.py --task <id> --accept --actor main --evidence-file evidence.json
    review_task.py --task <id> --final-accept --actor main --evidence-file evidence.json --notes "main takeover"
    review_task.py --task <id> --return-rework --actor main \\
        --failed-criteria c1,c2 --problems "..." --boundaries "..."
    review_task.py --task <id> --block --actor main --reason "..."
    review_task.py --task <id> --rework-prompt
    review_task.py --list
    review_task.py --stats
"""
import argparse
import json
import sys
from pathlib import Path

import delegation_policy as policy
import model_registry as registry


def _print(value, as_json):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def _read_criteria(args):
    if args.criteria_file:
        raw = args.criteria_file.read_text(encoding='utf-8-sig')
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, list):
            return [str(item) for item in data]
        if isinstance(data, dict) and isinstance(data.get('criteria'), list):
            return [str(item) for item in data['criteria']]
        return [line.strip() for line in raw.replace('\r', '\n').split('\n') if line.strip()]
    if args.criteria:
        text = args.criteria.replace('；', '\n').replace(';', '\n')
        return [line.strip() for line in text.split('\n') if line.strip()]
    return None


def _read_evidence(args):
    if not args.evidence_file:
        raise ValueError('验收通过需要 --evidence-file，逐条给出验收条件的结论和证据')
    raw = args.evidence_file.read_text(encoding='utf-8-sig')
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ValueError('证据文件需要是 UTF-8 JSON') from error
    if not isinstance(data, dict) or not isinstance(data.get('criteria'), list):
        raise ValueError('证据文件需要包含 criteria 数组')
    return data


def _split_keys(value):
    if not value:
        return []
    return [part.strip() for part in value.replace('；', ',').replace('、', ',').replace(';', ',').split(',') if part.strip()]


def _run(args):
    if args.list:
        return policy.review_index()
    if args.stats:
        return policy.aggregate()
    if not args.task:
        raise ValueError('请用 --task 指定任务，或使用 --list / --stats')
    run = registry.task_dir(args.task)
    if not run.is_dir():
        raise ValueError('任务不存在')
    if args.init:
        return policy.initialize(run, _read_criteria(args), args.actor)
    if args.accept:
        evidence = _read_evidence(args)
        notes = args.notes or evidence.get('notes') or ''
        return policy.accept(run, args.actor, evidence['criteria'], notes)
    if args.final_accept:
        evidence = _read_evidence(args)
        notes = args.notes or evidence.get('notes') or ''
        if not notes:
            raise ValueError('最终验收需要 --notes 说明主脑接管了什么、如何独立验证')
        return policy.final_accept(run, args.actor, evidence['criteria'], notes)
    if args.rework:
        return policy.request_rework(run, args.actor, _split_keys(args.failed_criteria),
                                     args.problems, args.boundaries, args.notes)
    if args.block:
        return policy.block(run, args.actor, args.reason)
    if args.rework_prompt:
        record = policy.load(run)
        if record is None:
            raise ValueError('这个任务还没有验收记录')
        return policy.rework_prompt(record)
    record = policy.load(run)
    if record is None:
        task = policy.task_state(run)
        if not policy.is_delegated(task):
            return '旧任务或非成果委派任务没有验收记录，也不能追认验收通过。'
        return '执行器尚未结束，暂无验收记录；可先用 --init 记录可判定的验收条件。'
    return record


def _render_list(index):
    lines = ['任务 ID\t状态\t整改\t执行器\t更新']
    for row in index['reviews']:
        lines.append('\t'.join([str(row.get('task_id')), str(row.get('state')),
                                f"{row.get('rework_count', 0)}/{row.get('max_auto_rework', 1)}",
                                str(row.get('executor_status') or '-'),
                                str(row.get('updated_at') or '-')]))
    stats = index['stats']
    lines.append('')
    lines.append(f"委派 {stats['delegated']} · 待验收 {stats[policy.EXECUTOR_COMPLETED]} · "
                 f"验收通过 {stats[policy.ACCEPTED]} · 退回整改 {stats[policy.RETURNED]} · "
                 f"受阻 {stats[policy.BLOCKED]} · 旧记录 {stats['legacy']}")
    lines.append(stats['note'])
    return '\n'.join(lines)


def _render_stats(stats):
    lines = [f"任务总数 {stats['tasks']} · 成果委派 {stats['delegated']} · 旧记录 {stats['legacy']}",
             f"状态：待执行 {stats[policy.DISPATCHED]} · 执行器完成 {stats[policy.EXECUTOR_COMPLETED]} · "
             f"执行器未完成 {stats[policy.EXECUTOR_FAILED]} · 验收通过 {stats[policy.ACCEPTED]} · "
             f"退回整改 {stats[policy.RETURNED]} · 受阻 {stats[policy.BLOCKED]}",
             f"已派发整改 {stats['rework_dispatched']} 次 · worker 模型请求 {stats['model_calls']} 次 · "
             f"worker 上报 tokens {stats['worker_tokens']}",
             stats['note']]
    return '\n'.join(lines)


def _render_record(record):
    if isinstance(record, str):
        return record
    lines = [f"任务 {record.get('task_id')} · {policy.STATE_LABELS.get(record.get('state'), record.get('state'))}",
             f"整改 {record.get('rework_count', 0)}/{policy.MAX_AUTO_REWORK}"]
    for item in record.get('criteria') or []:
        lines.append(f"  {item.get('id')} [{item.get('status')}] {item.get('text')} "
                     f"(证据 {len(item.get('evidence') or [])})")
    rework = record.get('rework') or {}
    if rework:
        lines.append('退回整改：' + '、'.join(rework.get('failed_texts') or []))
        lines.append('实际问题：' + str(rework.get('problems') or ''))
        lines.append('范围与边界：' + str(rework.get('boundaries') or ''))
        if rework.get('dispatched_task_id'):
            lines.append('已派发整改任务：' + str(rework['dispatched_task_id']))
    if record.get('blocked_reason'):
        lines.append('受阻原因：' + str(record['blocked_reason']))
    final = record.get('final') or {}
    if final:
        lines.append('主脑介入最终验收：' + str(final.get('accepted_at') or ''))
        lines.append('保留的执行器状态：' + str(final.get('executor_status_preserved')) +
                     '；整改计数保持 ' + str(final.get('rework_count_at_acceptance')))
        lines.append('介入说明：' + str(final.get('notes') or ''))
    if record.get('reviewer_notes'):
        lines.append('主脑说明：' + str(record['reviewer_notes']))
    return '\n'.join(lines)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--task')
    parser.add_argument('--list', action='store_true', help='列出全部验收记录')
    parser.add_argument('--stats', action='store_true', help='真实委派/验收计数，不估算 token 占比')
    parser.add_argument('--init', action='store_true', help='记录可判定的验收条件')
    parser.add_argument('--accept', action='store_true', help='主脑独立验收通过（需要逐条证据）')
    parser.add_argument('--final-accept', dest='final_accept', action='store_true',
                        help='执行器失败/受阻后，主脑介入完成时的独立最终验收（保留失败记录，不重置整改次数）')
    parser.add_argument('--return-rework', dest='rework', action='store_true', help='退回整改（最多自动一次）')
    parser.add_argument('--block', action='store_true', help='记录受阻 / 升级需主脑判断')
    parser.add_argument('--rework-prompt', action='store_true', help='输出可交给执行 Agent 的整改说明')
    parser.add_argument('--actor', default='main', help='记录人；执行器身份不能批准自己的成果')
    parser.add_argument('--criteria', help='验收条件，分号或换行分隔')
    parser.add_argument('--criteria-file', type=Path, help='UTF-8 验收条件文件或 JSON 数组')
    parser.add_argument('--evidence-file', type=Path, help='UTF-8 JSON：criteria 结论与证据')
    parser.add_argument('--failed-criteria', help='未通过的验收条件 ID 或原文，逗号分隔')
    parser.add_argument('--problems', help='实际观察到的问题')
    parser.add_argument('--boundaries', help='整改范围与边界')
    parser.add_argument('--reason', help='受阻原因')
    parser.add_argument('--notes', default='')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = _run(args)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.list:
        _print(_render_list(result) if not args.json else result, args.json)
    elif args.stats:
        _print(_render_stats(result) if not args.json else result, args.json)
    else:
        if args.json:
            _print(result, True)
        else:
            _print(_render_record(result), False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
