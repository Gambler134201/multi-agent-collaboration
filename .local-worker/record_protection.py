"""Write-time protection for the workbench record directory (``.model-desk``).

The panel treats ``<project>/.model-desk`` as the project's own workbench
records: task records, logs, notifications, acceptance and handover evidence.
The *control plane* (panel, worker, bridges) writes them; the execution model
must not.

This module is the Python half of that boundary:

* it derives the *content identity* of the two runtime files that implement the
  guard (``agent-runtime/record-policy.cjs`` and
  ``agent-runtime/record-protection.cjs``);
* it renders the Cordis patch row that mounts the guard into the real DSH run;
* it refuses a dispatch whose writable ``files`` scope names the record
  directory (so the model can never be handed it as an allowed target);
* it verifies the runtime proof the guard writes, so a run that did not
  actually load the protection is never treated as protected.

Scope and honesty: the runtime guard is a static argument check in the DSH tool
registry.  It is not OS-level isolation, and it cannot prove the target of a
terminal command whose path is computed at run time.  See
``agent-runtime/record-policy.cjs`` for the exact refusal rules.
"""
import hashlib
import json
import os
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
POLICY_FILE = HERE / 'agent-runtime/record-policy.cjs'
PLUGIN_FILE = HERE / 'agent-runtime/record-protection.cjs'
PLUGIN_ID = 'model-desk-record-protection'
PROOF_NAME = 'record-proof.json'
PROTECTED_NAME = '.model-desk'
SOURCE = 'model-desk-record-protection'
DENY_MARKER = '该目录由工作台管理'

__all__ = ['PROTECTED_NAME', 'PROOF_NAME', 'PLUGIN_ID', 'SOURCE', 'revision', 'plugin_config',
           'patch_row', 'has_protected_component', 'is_record_path', 'record_files',
           'validate_files', 'verify_proof']


def revision():
    """Content identity of the loaded guard code (both runtime files)."""
    digest = hashlib.sha256()
    for path in (POLICY_FILE, PLUGIN_FILE, HERE / 'agent-runtime/endpoint-access.cjs'):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _clean_root(project):
    text = str(project or '').strip()
    if not text:
        return ''
    try:
        return str(Path(text).resolve())
    except OSError:
        return os.path.abspath(text)


def plugin_config(project):
    """Configuration for the mounted guard.

    Only the resolved project root and the content identity travel in the patch;
    the per-run proof path comes from ``MODEL_DESK_RECORD_PROOF`` because one
    patch file is shared by every task with the same session fingerprint.
    """
    return {'root': _clean_root(project), 'revision': revision()}


def patch_row(project):
    """One Cordis patch row that mounts the guard into the real DSH run."""
    return {'id': PLUGIN_ID, 'name': str(PLUGIN_FILE), 'config': plugin_config(project)}


def _norm(value):
    text = str(value or '').strip()
    if not text:
        return ''
    return os.path.normcase(os.path.abspath(text)).rstrip('\\/')


def has_protected_component(value):
    """True when any path component is literally ``.model-desk``."""
    return any(part.lower() == PROTECTED_NAME for part in re.split(r'[\\/]+', str(value or '')))


def _inside(target, root):
    target = _norm(target)
    root = _norm(root)
    if not target or not root:
        return False
    if target == root:
        return True
    return target.startswith(root + os.sep)


def is_record_path(path, project=None):
    """True when ``path`` is the workbench record directory or lives inside it.

    Both the lexical path and the fully resolved path are judged, so a
    symlink/junction alias into the record directory is refused as well.
    ``.model-desk-backup`` and other similar names are never matched.
    """
    text = str(path or '').strip()
    if not text:
        return False
    if has_protected_component(text):
        return True
    candidate = Path(text)
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    if has_protected_component(str(resolved)):
        return True
    if project:
        root = Path(_clean_root(project))
        protected = root / PROTECTED_NAME
        for probe in (candidate, resolved):
            if _inside(probe, protected):
                return True
            try:
                real = Path(probe).resolve()
            except OSError:
                continue
            if _inside(real, protected):
                return True
    return False


def record_files(files, project=None):
    """The subset of a writable file scope that names the record directory."""
    offending = []
    for name in files or []:
        text = str(name or '').strip()
        if not text:
            continue
        candidate = Path(text)
        if not candidate.is_absolute() and project:
            candidate = Path(_clean_root(project)) / text
        if is_record_path(candidate, project):
            offending.append(text)
    return offending


def validate_files(files, project=None):
    """Refuse a writable file scope that names the workbench record directory."""
    offending = record_files(files, project)
    if offending:
        raise ValueError('工作台记录目录 .model-desk 由工作台管理，不能列为可写目标：' + '、'.join(offending))
    return [str(name) for name in (files or [])]


def verify_proof(run, project, session_id=None):
    """Check that the *parent* DSH agent really loaded the global guard.

    Mirrors ``preset_runtime.verify``: a missing, stale or child-session proof
    is an evidence failure, never a pass.  The caller must not treat the task as
    protected (or completed) when this raises.
    """
    path = Path(run) / PROOF_NAME
    if not path.is_file():
        raise ValueError('DSH 没有返回工作台记录写入保护证据，任务不视为完成')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('工作台记录写入保护证据无法读取：' + str(exc))
    if not isinstance(data, dict):
        raise ValueError('工作台记录写入保护证据格式不正确')
    if data.get('source') != SOURCE or data.get('probe') != 'ok' or data.get('scope') != 'global':
        raise ValueError('DSH 工作台记录写入保护未在全局工具层生效，任务不视为完成')
    if data.get('revision') != revision():
        raise ValueError('DSH 加载的工作台记录写入保护内容与当前安装不一致，任务不视为完成')
    if _norm(data.get('root')) != _norm(project):
        raise ValueError('DSH 工作台记录写入保护的工作目录与任务项目不一致')
    proof_session = data.get('session_id')
    if not session_id:
        raise ValueError('缺少可信的父会话身份，不能确认工作台记录保护来自本任务')
    if proof_session and proof_session != session_id:
        raise ValueError('DSH 工作台记录写入保护证据来自子会话而非父会话，父会话未被保护')
    if not proof_session:
        raise ValueError('DSH 工作台记录写入保护证据缺少写入会话身份，无法证明来自父会话')
    return {**data, 'matched': 'parent'}
