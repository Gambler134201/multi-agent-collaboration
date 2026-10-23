"""Reuse the installed Standard declaration and the user's actual Native bundle.

D2 adds a *content* identity for the mounted Agent preset.  The revision is a
hash of the exact preset declaration bytes, the exact shipped web overlay bytes
and the runtime bridge bytes, so a grant cannot survive a replaced preset even
when the DSH release version is unchanged.  The bytes are read directly from the
installed ``app.asar`` file table (the same table ``executor_adapter`` already
parses for the release version) instead of launching the executable, so the
panel and the worker derive the same identity without a subprocess.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import yaml

import record_protection
import runtime_paths

HERE = Path(__file__).resolve().parent
NATIVE = Path.home() / '.dsh/local-bundles/native-preset/cordis.patch.yml'
EXECUTABLE = runtime_paths.dsh_executable() or runtime_paths.UNSET
PRESET_BRIDGE = HERE / 'agent-runtime/preset-bridge.cjs'
SUBAGENT_BRIDGE = HERE / 'agent-runtime/subagent-bridge.cjs'
WEB_PRESET_DIR = 'dsh/node_modules/@deepseek-ai/dsh-web-app'
STANDARD_ENTRY = WEB_PRESET_DIR + '/presets/standard.patch.yml'
WEB_ENTRY = WEB_PRESET_DIR + '/cordis.patch.yml'
CACHE = {}
LOCK = threading.Lock()


class JS(str):
    pass


class Loader(yaml.SafeLoader):
    pass


class Dumper(yaml.SafeDumper):
    pass


Loader.add_constructor('tag:yaml.org,2002:js', lambda loader, node: JS(loader.construct_scalar(node)))
Dumper.add_representer(JS, lambda dumper, value: dumper.represent_scalar('tag:yaml.org,2002:js', str(value)))


def selected_id(model):
    return model.get('agent_preset') or ('native' if model.get('provider') == 'local' else 'standard')


def definition(text, preset):
    rows = yaml.load(text, Loader=Loader)
    for row in rows:
        for candidate in row.get('insert', []):
            if candidate.get('name') == '@deepseek-ai/dsh-agent-preset' and candidate.get('config', {}).get('id') == preset:
                return copy.deepcopy(candidate)
    raise ValueError(f'DSH 预设 {preset} 的实际定义未找到，未回退到其他模式')


# ------------------------------------------------------- installed ASAR read

def _asar_table(archive):
    """Parse the ASAR JSON file table once per (path, mtime, size)."""
    archive = Path(archive)
    stat = archive.stat()
    key = (str(archive), stat.st_mtime_ns, stat.st_size)
    with LOCK:
        cached = CACHE.get(key)
    if cached is not None:
        return cached
    with archive.open('rb') as handle:
        header = handle.read(16)
        if len(header) != 16:
            raise ValueError('无法读取已安装 DSH 的应用包')
        table_size = int.from_bytes(header[12:16], 'little')
        if not 16 < table_size < 64 * 1024 * 1024:
            raise ValueError('无法读取已安装 DSH 的应用包')
        table = json.loads(handle.read(table_size).decode('utf-8', 'replace'))
    value = (table, 16 + table_size)
    with LOCK:
        if len(CACHE) > 8:
            CACHE.clear()
        CACHE[key] = value
    return value


def _asar_read(archive, relative):
    table, data_offset = _asar_table(archive)
    node = table
    for part in relative.split('/'):
        node = (node or {}).get('files', {}).get(part)
    if not isinstance(node, dict) or 'offset' not in node:
        raise ValueError('已安装 DSH 中缺少 ' + relative)
    with Path(archive).open('rb') as handle:
        handle.seek(data_offset + int(node['offset']))
        return handle.read(int(node['size']))


def _standard_sources(archive):
    """Raw bytes of the shipped Standard declaration and web overlay."""
    archive = Path(archive)
    if not archive.exists():
        root = archive.with_name('app').resolve(strict=True)
        def read(relative):
            target = (root / relative).resolve(strict=True)
            if not target.is_relative_to(root):
                raise ValueError('已安装 DSH 的展开预设路径越界')
            with target.open('rb') as stream:
                value = stream.read(4 * 1024 * 1024 + 1)
            if len(value) > 4 * 1024 * 1024:
                raise ValueError('已安装 DSH 的预设过大')
            return value
        return read(STANDARD_ENTRY), read(WEB_ENTRY)
    return _asar_read(archive, STANDARD_ENTRY), _asar_read(archive, WEB_ENTRY)


def _preset_bytes(preset, archive):
    preset = str(preset or 'standard')
    if preset == 'native':
        if not NATIVE.is_file():
            raise ValueError('本机原生模式定义缺失：' + str(NATIVE))
        return NATIVE.read_bytes()
    if preset == 'standard':
        return _standard_sources(archive)[0]
    raise ValueError('工作台目前支持原生和标准 Agent 预设')


# ------------------------------------------------ preset content identity

def _revision(preset_bytes, web_bytes):
    digest = hashlib.sha256()
    digest.update(preset_bytes)
    digest.update(web_bytes)
    for bridge in (PRESET_BRIDGE, SUBAGENT_BRIDGE):
        try:
            digest.update(bridge.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()


def content_revision(preset, executable=None):
    """Content hash of one preset as actually mounted, or ``None`` if unknown.

    ``None`` means the caller must fail closed (an unresolved preset content
    identity is evidence that cannot be reused), never "matches anything".
    """
    try:
        preset = str(preset or 'standard')
        archive = Path(executable or EXECUTABLE).parent / 'resources' / 'app.asar'
        if not archive.is_file():
            # Expanded installations have no single archive stat to cache.
            # Re-read their actual bytes so identity changes remain observable.
            standard, web = _standard_sources(archive)
            return _revision(NATIVE.read_bytes() if preset == 'native' else standard, web) if preset in {'native', 'standard'} else None
        stat = archive.stat()
        native_stat = NATIVE.stat() if NATIVE.is_file() else None
        memo_key = (preset, str(archive), stat.st_mtime_ns, stat.st_size,
                    native_stat.st_mtime_ns if native_stat else None,
                    native_stat.st_size if native_stat else None)
        with LOCK:
            memo = CACHE.get(memo_key)
        if memo is not None:
            return memo or None
        if preset == 'native' and native_stat is None:
            return None
        preset_bytes = _preset_bytes(preset, archive)
        web_bytes = _standard_sources(archive)[1]
        value = _revision(preset_bytes, web_bytes)
        with LOCK:
            if len(CACHE) > 12:
                CACHE.clear()
            CACHE[memo_key] = value
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load(model, executable, subagent=False, child_config=None):
    """Load the declaration with the workflow's existing restrictions applied.

    ``subagent`` keeps the nested delegation group mounted (and, when a
    ``child_config`` is supplied, replaces the tool config with the controlled
    one-level child).  The returned ``revision`` deliberately does *not* depend
    on ``subagent``: it is the base content identity the panel also computes, so
    the grant stays comparable across the two processes.  The mount decision
    travels separately as ``subagent_mount``.
    """
    preset = selected_id(model)
    if preset not in {'native', 'standard'}:
        raise ValueError('工作台目前支持原生和标准 Agent 预设')
    executable = Path(executable)
    archive = executable.parent / 'resources/app.asar'
    standard_bytes, web_bytes = _standard_sources(archive)
    if preset == 'native':
        preset_bytes = _preset_bytes(preset, archive)
        source = str(NATIVE)
    else:
        preset_bytes = standard_bytes
        source = str(archive if archive.exists() else archive.with_name('app')) + '/' + STANDARD_ENTRY
    text = preset_bytes.decode('utf-8')
    web_text = web_bytes.decode('utf-8')
    declaration = definition(text, preset)
    # Retain this workflow's existing restrictions: GPT controls delegation and
    # extra online search must not escape the selected model's accounting relay.
    # When delegation is explicitly mounted, the group is enabled but every
    # sibling child backend stays disabled and the one tool gets the controlled
    # one-level configuration.
    constrain(declaration['config']['plugins'], subagent=subagent, child_config=child_config)
    web = yaml.load(web_text, Loader=Loader)
    overrides = [copy.deepcopy(row) for row in web if row.get('id') == 'system-prompt' or row.get('disabled') is True]
    # present is also supplied by the named declarations; avoid inherited tool contributions.
    overrides.append({'id': 'present', 'disabled': True})
    policy_overrides = ['online search disabled']
    policy_overrides.append('one-level controlled subagent mounted' if subagent else 'nested delegation disabled')
    # The shipped web overlay re-disables every tool (including the subagent
    # tool) after the worker's own rows.  These overrides are emitted last so the
    # enabled, route-pinned child tool really wins; the fork backend is never
    # re-enabled because fork would seed the child with the parent's history.
    subagent_overrides = []
    if subagent:
        subagent_overrides = [
            {'id': 'tool-subagent', 'disabled': False, 'config': copy.deepcopy(child_config)},
            {'id': 'tool-subagent-fork', 'disabled': True},
            {'id': 'tool-subagent-control', 'disabled': True},
            {'id': 'tool-subagent-list-agents', 'disabled': True},
        ]
    return {'id': preset, 'revision': _revision(preset_bytes, web_bytes), 'source': source,
            'declaration': declaration, 'overrides': overrides,
            'policy_overrides': policy_overrides, 'subagent_mount': bool(subagent),
            'subagent_overrides': subagent_overrides}


def constrain(rows, subagent=False, child_config=None):
    for row in rows:
        rid = row.get('id')
        if rid in {'tool-web', 'tool-plugin-manager'}:
            row['disabled'] = True
        elif rid == 'delegation':
            row['disabled'] = not subagent
        elif subagent and rid in {'tool-subagent-fork', 'tool-subagent-codex', 'tool-subagent-claude-code',
                                  'workflow-ptc', 'tool-workflow', 'tool-ralph',
                                  'tool-subagent-control', 'tool-subagent-list-agents'}:
            row['disabled'] = True
        if subagent and rid == 'tool-subagent' and child_config is not None:
            row['config'] = copy.deepcopy(child_config)
        if isinstance(row.get('config'), list):
            constrain(row['config'], subagent=subagent, child_config=child_config)


def patch_rows(preset, record_root=None):
    insert = [
        {'id': 'agent-preset-registry', 'name': '@deepseek-ai/dsh-agent-preset-registry', 'config': {'default': preset['id']}},
        preset['declaration'],
        {'id': 'model-desk-preset-bridge', 'name': str(PRESET_BRIDGE),
         'config': {'preset': preset['id'], 'revision': preset['revision'], 'source': preset['source'], 'policyOverrides': preset['policy_overrides']}},
    ]
    if preset.get('subagent_mount'):
        # Observe-only lifecycle ledger for the controlled probe and the E2 UI.
        insert.append({'id': 'model-desk-subagent-bridge', 'name': str(SUBAGENT_BRIDGE),
                       'config': {'revision': preset['revision'], 'policyOverrides': preset['policy_overrides']}})
    if record_root:
        # Write-before-the-fact protection for the project's ``.model-desk``
        # workbench records.  Mounted as a separate content-identity plugin, so
        # the native/standard preset bytes, ids and revisions are untouched.
        insert.append(record_protection.patch_row(record_root))
    return preset['overrides'] + [{'insert': insert}] + list(preset.get('subagent_overrides') or [])


def verify(run, preset, session_id=None, allowed_sessions=()):
    """Check that the *parent* ACP agent actually mounted this preset.

    ``preset-bridge.cjs`` writes the proof once per run, at the first awaited
    ``agent/created`` edge, so the recorded session must be the parent's.  A
    child session can never substitute for the parent proof: the previous
    ``allowed_sessions`` fallback accepted a child's overwrite, and that let a
    run be marked verified without proving the parent's preset at all.  The
    parameter is kept so old callers keep importing, but it is never a pass path.
    """
    del allowed_sessions  # deliberately unused: a child proof is not a parent proof
    path = Path(run) / 'preset-proof.json'
    if not path.is_file():
        raise ValueError('DSH 没有返回实际 Agent 预设绑定证据，任务不视为完成')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('preset') != preset['id'] or data.get('revision') != preset['revision']:
        raise ValueError('DSH 实际 Agent 预设与任务配置不一致')
    proof_session = data.get('session_id')
    if session_id and proof_session != session_id:
        raise ValueError('DSH 预设证据来自子会话而非父会话，父预设未被证实，任务不视为完成')
    if not data.get('session_id'):
        raise ValueError('DSH 预设证据缺少写入会话身份，无法证明来自父会话')
    return {**data, 'matched': 'parent'}
