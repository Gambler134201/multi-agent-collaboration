"""Bounded, offline code navigation. No imports of user code or project writes.

The existing context index owns symbol extraction and default exclusions. This
view adds static Python import evidence, keeping runtime/dynamic edges unknown.
Only an explicit query scans; there are no background jobs, model calls or disk
caches. A bounded in-memory cache reuses parsing after content-hash validation.
"""
from __future__ import annotations

import ast
from collections import OrderedDict
import copy
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import stat
import threading
import time

import context_manager as context
import project_scope

VERSION = 1
MAX_FILES = 300
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 20000
MAX_SECONDS = 5
MAX_EDGES = 2500
MAX_UNRESOLVED = 300
MAX_SYMBOLS = 24
CODE_SUFFIXES = frozenset({'.py', '.pyi', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.ps1', '.psm1'})
EXTRA_EXCLUDED = frozenset({'.dsh', '.workbuddy', '.pi', '.ssh', '.aws', '.azure', '.gnupg', '.local-worker-backups'})
WORKER_PRIVATE = frozenset({'tasks', 'desktop-integrations', 'agent-home', 'dsh-home', 'pi-home'})
_CACHE = OrderedDict()
_LOCK = threading.RLock()


def _link(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _safe(root, path):
    """Refuse links/junctions and real-path escapes before and after reading."""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if _link(current):
            return False
    return path.resolve().is_relative_to(root)


def _scan(root):
    start = time.monotonic()
    files, pending, visited, consumed = [], [root], 0, 0
    limits = {'files_truncated': False, 'edges_truncated': False,
              'bytes_truncated': False, 'scan_truncated': False,
              'unresolved_truncated': False, 'skipped_files': 0}
    while pending:
        directory = pending.pop()
        if visited >= MAX_ENTRIES or time.monotonic() - start > MAX_SECONDS:
            limits['scan_truncated'] = True
            break
        try:
            if directory != root and not _safe(root, directory):
                continue
            # Never materialize an arbitrarily large directory listing.
            entries = []
            with os.scandir(directory) as stream:
                for item in stream:
                    visited += 1
                    if visited > MAX_ENTRIES:
                        limits['scan_truncated'] = True
                        break
                    entries.append(item)
            entries.sort(key=lambda item: item.name)
        except OSError:
            limits['scan_truncated'] = True
            continue
        subdirs = []
        for item in entries:
            if time.monotonic() - start > MAX_SECONDS:
                limits['scan_truncated'] = True
                break
            path = directory / item.name
            try:
                if _link(path) or context.is_sensitive_path(path):
                    continue
                if item.is_dir(follow_symlinks=False):
                    if (context.is_excluded_dir(item.name.casefold()) or item.name.casefold() in EXTRA_EXCLUDED
                            or (directory.name.casefold() == '.local-worker' and item.name.casefold() in WORKER_PRIVATE)):
                        continue
                    subdirs.append(path)
                    continue
                if path.suffix.lower() not in CODE_SUFFIXES or not item.is_file(follow_symlinks=False):
                    continue
                if len(files) >= MAX_FILES:
                    limits['files_truncated'] = True
                    break
                if not _safe(root, path):
                    continue
                size = path.stat().st_size
                if size > MAX_FILE_BYTES:
                    limits['skipped_files'] += 1
                    limits['bytes_truncated'] = True
                    continue
                if consumed + size > MAX_TOTAL_BYTES:
                    limits['bytes_truncated'] = True
                    continue
                with path.open('rb') as stream:
                    body = stream.read(min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - consumed) + 1)
                if not _safe(root, path):
                    limits['skipped_files'] += 1
                    continue
                consumed += len(body)
                if len(body) > MAX_FILE_BYTES or consumed > MAX_TOTAL_BYTES or b'\0' in body:
                    limits['bytes_truncated'] = True
                    limits['skipped_files'] += 1
                    continue
                files.append((path.relative_to(root).as_posix(), body))
            except OSError:
                limits['skipped_files'] += 1
        pending.extend(reversed(subdirs))
        if limits['files_truncated'] or limits['scan_truncated']:
            break
    return files, limits


def _targets(base, available):
    if base in ('', '.'):
        candidates = ['__init__.py']
    else:
        candidates = [base + '.py', base + '/__init__.py', base + '.pyi']
    return [name for name in candidates if name in available]


def _relationships(nodes, trees, limits):
    available = {node['path'] for node in nodes}
    edges, unresolved, seen = [], [], set()

    def unknown(source, node, module, reason):
        if len(unresolved) >= MAX_UNRESOLVED:
            limits['unresolved_truncated'] = True
            return
        unresolved.append({'source': source, 'line': node.lineno,
                           'module': context._redact(module[:160]), 'reason': reason})

    def resolve(source, node, module, level=0):
        parent = PurePosixPath(source).parent
        part = module.replace('.', '/')
        if level:
            for _ in range(level - 1):
                if str(parent) == '.':
                    unknown(source, node, module, 'relative_import_outside_index')
                    return False
                parent = parent.parent
            options, resolution = _targets((parent / part).as_posix(), available), 'relative'
        else:
            options, resolution = _targets(part, available), 'project_root'
            if not options:
                options, resolution = _targets((parent / part).as_posix(), available), 'sibling_candidate'
            if not options:
                options, resolution = _targets('src/' + part, available), 'src_convention_candidate'
            if not options:
                options, resolution = [], 'ancestor_candidate'
                for ancestor in parent.parents:
                    if str(ancestor) != '.':
                        options.extend(_targets((ancestor / part).as_posix(), available))
                options = list(dict.fromkeys(options))
        if len(options) != 1:
            unknown(source, node, module, 'ambiguous' if options else 'external_or_unresolved')
            return False
        target = options[0]
        key = (source, target, node.lineno)
        if key not in seen and target != source:
            if len(edges) >= MAX_EDGES:
                limits['edges_truncated'] = True
                return True
            seen.add(key)
            edges.append({'source': source, 'target': target, 'line': node.lineno,
                          'kind': 'import', 'evidence': 'python_ast', 'resolution': resolution})
        return True

    for source, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    resolve(source, node, alias.name)
            elif isinstance(node, ast.ImportFrom):
                # A name may be a module or a symbol; only resolve actual files.
                module = node.module or ''
                found_child = False
                for alias in node.names:
                    if alias.name == '*':
                        continue
                    child = (module + '.' if module else '') + alias.name
                    before = len(unresolved)
                    if resolve(source, node, child, node.level):
                        found_child = True
                    elif len(unresolved) > before:
                        unresolved.pop()  # fallback to the imported module/symbol container
                if module or not found_child:
                    resolve(source, node, module, node.level)
            elif isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Name) and node.func.id == '__import__'
                    or isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module'):
                unknown(source, node, '<dynamic>', 'dynamic_import_not_resolved')
    return edges, unresolved


def _build(root, files, limits, digest):
    nodes, trees = [], {}
    for relative, body in files:
        text = body.decode('utf-8-sig', errors='replace')
        try:
            symbols = context.extract_symbols(relative, text)
            if Path(relative).suffix.lower() in {'.py', '.pyi'} and not symbols['degraded']:
                trees[relative] = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            symbols = {'language': 'python', 'symbols': [], 'extraction': 'file_index',
                       'degraded': True, 'reason': 'parse_failed'}
        safe_symbols = symbols['symbols'][:MAX_SYMBOLS]
        if len(symbols['symbols']) > MAX_SYMBOLS:
            limits['symbols_truncated'] = True
        if symbols['extraction'] != 'ast':
            # Regex signatures can contain default literal values; the map
            # never exports those literals from files it discovers broadly.
            safe_symbols = [{**symbol, 'signature': context._redact(symbol['name']) +
                             ('(…)' if symbol['kind'] == 'function' else '')}
                            for symbol in safe_symbols]
        nodes.append({'id': relative, 'path': relative,
                      'group': '测试' if _is_test(relative) else str(PurePosixPath(relative).parent),
                      **{key: symbols[key] for key in ('language', 'extraction', 'degraded', 'reason')},
                      'symbols': safe_symbols,
                      'symbols_truncated': len(symbols['symbols']) > MAX_SYMBOLS,
                      'sha256': hashlib.sha256(body).hexdigest(), 'tests': []})
    edges, unresolved = _relationships(nodes, trees, limits)
    lookup = {node['id']: node for node in nodes}
    for edge in edges:
        if _is_test(edge['source']) and edge['target'] in lookup:
            tests = lookup[edge['target']]['tests']
            if edge['source'] not in tests:
                tests.append(edge['source'])
    return {'schema': VERSION, 'project': str(root),
            'index': {'hash': digest, 'generated_at': datetime.now(timezone.utc).isoformat(),
                      'freshness': 'partial' if any(limits.values()) else 'validated', 'reused': False},
            'summary': {'files': len(nodes), 'edges': len(edges), 'unresolved': len(unresolved)},
            'nodes': nodes, 'edges': edges, 'unresolved': unresolved, 'limits': limits,
            'notes': ['仅静态源码导航，不是完整运行时调用链；没有执行项目代码或调用模型。',
                      'Python 导入来自 AST；sibling/src/ancestor 候选依赖路径约定，运行时未核实。',
                      '其他语言仅提取符号/文件；动态导入、未解析项不猜测关系。',
                      '测试关联来自静态导入，不代表覆盖率或测试通过；未读取任务/验收记录。',
                      '哈希仅表示本次受限扫描快照；排除项、超限或不可读文件不在覆盖内。']}


def _is_test(path):
    parts = PurePosixPath(path).parts
    return 'tests' in parts or 'test' in parts or PurePosixPath(path).name.startswith('test_')


def query(project, *, file=None, query='', refresh=False, limit=MAX_FILES):
    """Validate one explicit project, hash its bounded code snapshot, then query."""
    root = Path(project_scope.normalize_project(project))
    if any(part.casefold() in {'.model-desk', '.git', '.dsh', '.workbuddy', '.pi', '.ssh', '.aws', '.azure', '.gnupg'}
           for part in root.parts):
        raise ValueError('工作台记录、认证及宿主私有目录不提供代码地图')
    if not isinstance(query, str) or len(query) > 160:
        raise ValueError('地图搜索词须为不超过 160 字符的字符串')
    if type(refresh) is not bool or type(limit) is not int or not 1 <= limit <= MAX_FILES:
        raise ValueError('地图刷新或数量参数不正确')
    if file is not None:
        if (not isinstance(file, str) or not file or len(file) > 1024 or '\0' in file
                or ntpath.isabs(file) or ntpath.splitdrive(file)[0]
                or '..' in PurePosixPath(file.replace('\\', '/')).parts):
            raise ValueError('地图文件须为当前项目内的相对代码路径')
        file = PurePosixPath(file.replace('\\', '/')).as_posix()
    files, limits = _scan(root)
    digest = hashlib.sha256()
    digest.update(json.dumps(limits, sort_keys=True).encode())
    for path, body in files:
        digest.update(path.encode('utf-8') + b'\0' + hashlib.sha256(body).digest())
    fingerprint, key = digest.hexdigest(), os.path.normcase(str(root))
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached['index']['hash'] == fingerprint and not refresh:
            result = copy.deepcopy(cached)
            result['index']['reused'] = True
            _CACHE.move_to_end(key)
        else:
            result = _build(root, files, limits, fingerprint)
            _CACHE[key] = copy.deepcopy(result)
            _CACHE.move_to_end(key)
            while len(_CACHE) > 4:
                _CACHE.popitem(last=False)
    selected = result['nodes']
    # Explanation is re-read independently from the code cache on every query.
    # Metadata-only edits must not leave a stale cached explanation in place.
    import project_architecture
    result['architecture'] = project_architecture.read(root, selected, fingerprint, _safe)
    if file:
        if not any(node['path'] == file for node in selected):
            raise ValueError('文件不在当前受限代码索引中；未读取范围外文件')
        neighbors = {file}
        for edge in result['edges']:
            if file in (edge['source'], edge['target']):
                neighbors.update((edge['source'], edge['target']))
        selected = sorted((node for node in selected if node['id'] in neighbors),
                          key=lambda node: node['id'] != file)
    if query:
        text = query.casefold()
        selected = [node for node in selected if text in node['path'].casefold()
                    or any(text in symbol['name'].casefold() for symbol in node['symbols'])]
    result['nodes'] = selected[:limit]
    ids = {node['id'] for node in result['nodes']}
    result['edges'] = [edge for edge in result['edges'] if edge['source'] in ids and edge['target'] in ids]
    result['unresolved'] = [item for item in result['unresolved'] if item['source'] in ids]
    result['selection'] = {'file': file, 'query': query, 'matched': len(selected), 'truncated': len(selected) > limit}
    result['read_only'] = True
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='只读项目代码地图：不启动服务、执行代码或模型')
    parser.add_argument('--project', required=True)
    parser.add_argument('--query', default='')
    parser.add_argument('--file')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--limit', type=int, default=40)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(query(args.project, query=args.query, file=args.file,
                               refresh=args.refresh, limit=args.limit), ensure_ascii=False))
        return 0
    except (ValueError, OSError):
        print(json.dumps({'error':'项目或查询参数无效，或当前索引暂不可读；未执行项目代码。'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
