"""Read a bounded, project-owned explanation; never execute instructions in it."""
import hashlib
import json
import ntpath
from pathlib import PurePosixPath
import re

FILENAME = 'project-architecture.json'
MAX_BYTES = 65536


def _text(value, limit, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError('text')
    import context_manager
    return context_manager._redact(value)


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('hash')
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate_key')
        result[key] = value
    return result


def read(root, nodes, snapshot_hash, safe):
    """Only indexed code hashes are evidence; unindexed refs are never opened."""
    base = {'source': FILENAME, 'provenance': 'project_authored',
            'note': '项目维护者或 Agent 整理的讲解；哈希核对不证明业务描述或验收正确。'}
    path = root / FILENAME
    try:
        if not path.exists() and not path.is_symlink():
            return {**base, 'state': 'missing'}
        if not safe(root, path) or not path.is_file():
            raise ValueError('unsafe')
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or not safe(root, path):
            raise ValueError('size_or_link')
        data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs)
        if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1:
            raise ValueError('schema')
        title, summary = _text(data.get('title'), 100), _text(data.get('summary'), 800)
        based_on = _hash(data.get('snapshot_hash'))
        rows, links = data.get('nodes'), data.get('edges')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 24:
            raise ValueError('nodes')
        if not isinstance(links, list) or len(links) > 60:
            raise ValueError('edges')
        available = {node['path']: node for node in nodes}
        result, ids = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('node')
            identity = row.get('id')
            if not isinstance(identity, str) or not re.fullmatch('[A-Za-z0-9_-]{1,48}', identity) or identity in ids:
                raise ValueError('id')
            ids.add(identity)
            refs = row.get('evidence')
            if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
                raise ValueError('evidence')
            evidence = []
            for ref in refs:
                if not isinstance(ref, dict):
                    raise ValueError('evidence')
                name = ref.get('path')
                if (not isinstance(name, str) or not name or len(name) > 1024 or '\\' in name
                        or '\0' in name or ntpath.isabs(name) or ntpath.splitdrive(name)[0]
                        or '..' in PurePosixPath(name).parts or ':' in name
                        or PurePosixPath(name).as_posix() != name):
                    raise ValueError('path')
                expected = _hash(ref.get('sha256'))
                node = available.get(name)
                evidence.append({'path': name, 'sha256': expected,
                                 'state': 'unindexed' if node is None else 'unchanged' if node['sha256'] == expected else 'changed'})
            state = 'stale' if any(ref['state'] == 'changed' for ref in evidence) else 'unknown' if any(ref['state'] == 'unindexed' for ref in evidence) else 'matched'
            result.append({'id': identity, 'title': _text(row.get('title'), 80),
                           'role': _text(row.get('role', ''), 100, False),
                           'description': _text(row.get('description'), 1000),
                           'output': _text(row.get('output', ''), 300, False),
                           'evidence': evidence, 'state': state})
        edges = []
        for link in links:
            if not isinstance(link, dict) or link.get('source') not in ids or link.get('target') not in ids:
                raise ValueError('edge_identity')
            confidence = link.get('confidence')
            if confidence not in {'documented', 'inferred'}:
                raise ValueError('confidence')
            edges.append({'source': link['source'], 'target': link['target'],
                          'label': _text(link.get('label'), 160), 'confidence': confidence})
        changed = based_on != snapshot_hash or any(node['state'] == 'stale' for node in result)
        state = 'stale' if changed else 'unknown' if any(node['state'] == 'unknown' for node in result) else 'matched'
        return {**base, 'state': state, 'title': title, 'summary': summary,
                'snapshot_hash': based_on, 'content_hash': hashlib.sha256(raw).hexdigest(),
                'nodes': result, 'edges': edges}
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {**base, 'state': 'invalid', 'message': '讲解文件格式无效、超过限制或路径不安全；已退回基础地图。'}
