"""Read-only startup signal for Codex's existing local CLI workflow.

Uses the same candidate resolver and signal formatter as native brain_preflight.
No service, model, broker capability, session or project record is created.
The explicit project is a calling-host declaration, not host-verified identity.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from types import SimpleNamespace

import brain_gateway
import brain_mcp_server
import project_scope
import workflow_settings

# One formatter in the host-neutral main-brain core; DSH already calls this.
build_signal = brain_mcp_server._preflight_signal


def snapshot(project):
    settings = workflow_settings.snapshot()
    normalized = None
    try:
        normalized = project_scope.normalize_project(project)
    except (ValueError, OSError, TypeError):
        pass  # Missing/ambiguous identity never falls back to cwd or service root.
    context = SimpleNamespace(project=normalized or '未选择', mutations_allowed=bool(normalized))
    candidates = {}
    failure = ''
    if settings.get('readable') is True and settings.get('enabled') is True and normalized:
        try:
            # This existing broker read operation does not consume context,
            # issue capabilities or submit anything. Model/account settings
            # are global; task records remain project-local and are not read.
            candidates = brain_gateway._candidates(None, {})
            current = candidates.get('workflow')
            if not isinstance(current, dict):
                raise ValueError('missing workflow snapshot')
            settings = current  # Same snapshot as candidate/scope evaluation.
        except Exception:
            # Never print arbitrary registry/adapter exceptions or key material.
            candidates = {}
            failure = '无法完成执行候选预检，请检查模型和执行 Agent 配置'
    signal = build_signal(context, settings, candidates)
    if settings.get('readable') is True and settings.get('enabled') is True:
        if not normalized:
            failure = '缺少可信的当前项目目录，未沿用其他项目'
        if failure:
            signal.update(state='unavailable', text='🔴 协作未就绪｜' + failure)
    signal['meaning'] = ('本机 Codex CLI 的项目、开关与执行候选只读预检；未建立主脑 broker 会话，'
                         '未调用模型或测试提供方接口，也不是任务派发/验收成功。')
    models = [{'model_id': row.get('model_id'), 'name': row.get('name'),
               'engine': row.get('engine'), 'dispatch_ready': row.get('dispatch_ready') is True}
              for row in candidates.get('models', []) if isinstance(row, dict)]
    return {'host': 'codex', 'project': normalized, 'project_source': 'explicit_calling_project',
            'host_verified': False, 'workflow': {key: settings.get(key) for key in
            ('readable', 'enabled', 'model_scope', 'paid_allowed')}, 'models': models,
            'user_signal': signal, 'preflight_at': datetime.now(timezone.utc).isoformat(),
            'model_called': False, 'task_dispatched': False, 'service_started': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', help='受信调用宿主提供的项目绝对目录；不默认 cwd')
    args = parser.parse_args(argv)
    try:
        payload = snapshot(args.project)
    except Exception:
        payload = {'user_signal': {'state': 'unavailable', 'text': '🔴 协作未就绪｜本机预检无法完成'},
                   'model_called': False, 'task_dispatched': False, 'service_started': False}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout.buffer.write((text + '\n').encode('utf-8'))
    else:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
