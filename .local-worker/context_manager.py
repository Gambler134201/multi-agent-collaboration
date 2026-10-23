"""Context control for the local execution gateway: compact index + tool offload.

Two bounded, offline jobs live here:

1. **Scope-centred project index** - a compact file/symbol/signature index with a
   content hash and version. A new agent session gets the small index and reads
   the original files it actually needs instead of receiving the whole project.
   Unsupported languages degrade to an explicit file-level entry, never to a
   silent guess. The index never contains file bodies, environment variables,
   registry files or credentials.

2. **Large tool-result offloading** - before a request is written to the model
   API, every large ``role=tool`` result is saved atomically to a stable,
   task-readable file and the message keeps a bounded preview plus path, byte and
   line counts and an extraction note. Only the minimum message content changes:
   system/user goals, tool protocol, ``tool_call_id``, message order and
   multimodal parts are never altered. Repeating the same request reuses the same
   file, so provider prompt caching is not broken and the evidence is not
   regenerated.

Design boundaries
-----------------
* Byte reductions recorded here are *audit* numbers, never token savings. Token
  measurement (``token_meter``) and the provider's reported ``usage`` stay
  authoritative and are what cost settlement uses.
* Offloading preserves evidence: the full original tool result is copied, never
  redacted or dropped. The index, by contrast, is a derived summary and excludes
  sensitive files entirely.
* No new model calls, no embeddings, no heavy services, no Git, no subprocess.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

CONTEXT_SCHEMA = 1
INDEX_VERSION = 1
OFFLOAD_VERSION = 1

# Fixed, short, stable execution constraints. The same text the executor was
# already given is kept verbatim so behaviour does not change; it lives here so
# prompt assembly and tests share one definition.
EXECUTION_CONSTRAINTS = ('执行约束：只处理这份任务。不要切换模型或创建子代理；'
                         '搜索项目时默认排除 .model-desk 工作台记录目录（默认搜索排除，不是硬隔离）；'
                         '不要安装软件、提交代码、部署或充值。')
# The delegation clause above is only true when the realtime decision denied a
# child mount.  When the same decision actually allowed one, sending the blanket
# "不要创建子代理" makes the prompt contradict the mounted tool config.  This
# variant keeps every other constraint identical and replaces only that clause,
# still requiring a real expected benefit before a child is used.
EXECUTION_CONSTRAINTS_DELEGATE = ('执行约束：只处理这份任务。本次运行已按实测能力与用户策略放行子代理：'
                                  '仅当任务确有收益（可拆分的独立子问题、需要额外检索或交叉校验）时，'
                                  '才创建一个一次性、禁止后台的子代理，并复用它必须与当前父任务相同的模型和路由；'
                                  '没有明确收益时不要创建，也不要切换模型；'
                                  '搜索项目时默认排除 .model-desk 工作台记录目录（默认搜索排除，不是硬隔离）；'
                                  '不要安装软件、提交代码、部署或充值。')


def execution_constraints(delegate=False):
    """The constraint text for the decision that was actually enforced."""
    return EXECUTION_CONSTRAINTS_DELEGATE if delegate else EXECUTION_CONSTRAINTS
FILE_SCOPE_PREFIX = '\n允许修改的文件：'
FILE_SCOPE_NOTE = ('\n只修改这些文件。需要其他文件变更时在结果中报告。'
                   '完成后简短说明实际改动、已验证结果及未验证/未解决项；自测通过仍需主脑独立验收。')
SELF_TEST_NOTE = ('\n自测以本次验收条件和项目要求为准，优先使用项目已有检查；缺少合适工具时采用最小有效验证。'
                  '区分产品缺陷与检查方法自身报错，分别修正并复测。'
                  '同一代码、输入和环境下已通过的检查，无明确任务要求或新风险时无需重复；修改后或换条件时做必要复测。'
                  '必要验证已有足够证据且无待处理问题时及时交付；受阻或未验证的项目如实报告，不冒称通过。')
TEST_BY_CONTROLLER_NOTE = '\n验收测试由控制端运行，请不要自行执行测试命令。'
READONLY_NOTE = '\n这是只读咨询任务，不得修改文件或执行写操作。'
# Supplementary notice only.  The real enforcement is the DSH tool guard mounted
# by ``record-protection.cjs``; this line exists so the model does not spend a
# turn discovering the boundary, and it never claims OS-level isolation.
RECORD_PROTECTION_NOTE = ('\n工作台记录目录 .model-desk 由工作台管理：不要写入、修改、删除或移动其中的'
                          '任务记录、日志、通知、验收与交接文件。DSH 工具层会在执行前拒绝'
                          '可识别的直接写入和终端操作。触发记录保护后，本轮写入与终端工具暂停；'
                          '保留拒绝原文并报告主脑，不要换用其他工具、脚本或等价命令重试。'
                          '非必要缓存清理可以省略；怀疑误拦截也应报告，由主脑检查策略。'
                          '直接读取文件与最终报告仍可继续。这是工具参数保护，不是操作系统沙箱。')

# First line of every replaced tool message; also the idempotency marker so a
# second pass over an already-offloaded message is a no-op.
OFFLOAD_MARKER = '[本地网关已外置的工具结果]'

DEFAULT_OFFLOAD_MIN_BYTES = 8192
DEFAULT_PREVIEW_LINES = 30
DEFAULT_PREVIEW_TAIL_LINES = 8
DEFAULT_PREVIEW_BYTES = 2000
MAX_INDEX_FILES = 2000
MAX_SYMBOLS_PER_FILE = 120
MAX_SIGNATURE_CHARS = 160
MAX_INDEX_FILE_BYTES = 512 * 1024

EXCLUDED_DIR_NAMES = frozenset({
    '.git', '.hg', '.svn', '.idea', '.vscode', 'venv', '.venv', 'env',
    'node_modules', '__pycache__', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    '.tox', 'runs', 'dist', 'build', '.next', '.nuxt', '.cache', 'backups',
    'dsh-home', 'agent-runtime', 'site-packages', '.codex',
    '.tmp', 'tmp', 'target', 'coverage', '.coverage', '.nyc_output', 'vendor',
    # Workbench records: task DB/logs/handovers never enter the code index or a
    # dispatch context.  This is a default search exclusion, not an OS sandbox.
    '.model-desk',
})

SENSITIVE_NAMES = frozenset({
    'models.json', 'credentials.json', 'secrets.json', 'auth.json', 'id_rsa',
    'id_ed25519', 'id_ecdsa', '.netrc', '.npmrc', '.pypirc',
})
SENSITIVE_SUFFIXES = ('.key', '.pem', '.pfx', '.p12', '.jks', '.kdbx', '.keystore')
SENSITIVE_TOKENS = ('secret', 'credential', 'password', 'passwd')

_REDACT_PATTERNS = (
    re.compile(r'sk-[A-Za-z0-9_\-]{6,}'),
    re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}'),
    re.compile(r'(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]{6,}'),
)

_LANGUAGE_BY_SUFFIX = {
    '.py': 'python', '.pyi': 'python',
    '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript',
    '.ps1': 'powershell', '.psm1': 'powershell',
    '.md': 'markdown', '.markdown': 'markdown',
    '.yml': 'yaml', '.yaml': 'yaml',
    '.toml': 'toml', '.ini': 'ini', '.cfg': 'ini',
    '.json': 'json',
}
_DATA_LANGUAGES = frozenset({'json', 'yaml', 'toml', 'ini'})

_JS_PATTERNS = (
    ('function', re.compile(
        r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)\n]*)\)', re.M)),
    ('class', re.compile(
        r'^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)', re.M)),
    ('function', re.compile(
        r'^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)\n]*)\)\s*=>', re.M)),
)
_PS_PATTERN = re.compile(
    r'^\s*function\s+([A-Za-z_][\w-]*)\s*(?:\(([^)\n]*)\))?', re.M)
_MD_PATTERN = re.compile(r'^(#{1,6})\s+(.+?)\s*$', re.M)

_SIGNAL_PATTERNS = (
    ('exit', re.compile(
        r'(?i)\b(?:exit[_ ]?code|returncode|exit status)\b["\']?\s*[:=]?\s*(-?\d{1,3})')),
    ('error', re.compile(
        r'(?m)^\s*(?:[A-Za-z_][\w.]*(?:Error|Exception)|Traceback \(most recent call last\))[^\n]{0,60}')),
    ('failure', re.compile(r'(?i)\b(?:failed|failure)\b[^\n]{0,50}')),
)


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _truncate(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:max(0, limit - 1)] + '…'


def _redact(text):
    """Remove credential-looking fragments from derived summary text."""
    text = _REDACT_PATTERNS[0].sub('[已隐藏]', str(text))
    text = _REDACT_PATTERNS[1].sub(r'\1[已隐藏]', text)
    text = _REDACT_PATTERNS[2].sub(r'\1[已隐藏]', text)
    return text


def _atomic_write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(data)
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                return path
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.04)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path, value):
    return _atomic_write_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


def serialized_bytes(value):
    """UTF-8 byte length of the same serialization the relay sends."""
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def is_sensitive_path(path):
    """Registry/credential files are never read into any derived artifact."""
    name = Path(path).name
    lowered = name.lower()
    if lowered in SENSITIVE_NAMES or lowered.startswith('.env'):
        return True
    if any(lowered.endswith(suffix) for suffix in SENSITIVE_SUFFIXES):
        return True
    return any(token in lowered for token in SENSITIVE_TOKENS)


# ---------------------------------------------------------------------------
# Project index
# ---------------------------------------------------------------------------

def is_excluded_dir(name):
    return name in EXCLUDED_DIR_NAMES


def _type_text(node):
    """Conservative type rendering; literal constants are never rendered."""
    import ast
    if node is None:
        return ''
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _type_text(node.value)
        return (base + '.' if base else '') + node.attr
    if isinstance(node, ast.Subscript):
        base = _type_text(node.value)
        inner = _type_text(node.slice)
        return f'{base}[{inner}]' if base else ''
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _type_text(node.left), _type_text(node.right)
        return f'{left} | {right}' if left and right else (left or right)
    if isinstance(node, ast.Tuple):
        return ', '.join(filter(None, (_type_text(element) for element in node.elts)))
    if isinstance(node, ast.Constant):
        return 'None' if node.value is None else type(node.value).__name__
    return '...'


def _argument_names(args):
    names = [argument.arg for argument in list(args.posonlyargs) + list(args.args)]
    if args.vararg:
        names.append('*' + args.vararg.arg)
    elif args.kwonlyargs:
        names.append('*')
    names += [argument.arg for argument in args.kwonlyargs]
    if args.kwarg:
        names.append('**' + args.kwarg.arg)
    return names


def _function_symbol(node, kind='function'):
    import ast
    prefix = 'async def ' if isinstance(node, ast.AsyncFunctionDef) else 'def '
    rendered = ', '.join(_argument_names(node.args))
    if node.args.defaults or any(default is not None for default in node.args.kw_defaults):
        rendered += (', ...' if rendered else '...')
    returned = _type_text(node.returns)
    signature = prefix + node.name + '(' + rendered + ')' + (f' -> {returned}' if returned else '')
    return {'name': node.name, 'kind': kind, 'line': int(node.lineno),
            'signature': _redact(_truncate(signature, MAX_SIGNATURE_CHARS))}


def _python_symbols(text):
    import ast
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    symbols = []

    def add(node, kind):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_function_symbol(node, kind))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ', '.join(filter(None, (_type_text(base) for base in node.bases)))
            signature = 'class ' + node.name + (f'({bases})' if bases else '')
            symbols.append({'name': node.name, 'kind': 'class', 'line': int(node.lineno),
                            'signature': _redact(_truncate(signature, MAX_SIGNATURE_CHARS))})
            for child in node.body:
                add(child, 'method')
        else:
            add(node, 'function')
    return symbols


def _regex_symbols(text, patterns):
    symbols = []
    seen = set()
    for kind, pattern in patterns:
        for match in pattern.finditer(text):
            name = match.group(1)
            if name in seen:
                continue
            seen.add(name)
            args = match.group(2) if match.lastindex and match.lastindex >= 2 else ''
            line = text.count('\n', 0, match.start()) + 1
            if kind == 'class':
                signature = 'class ' + name
            elif args:
                signature = name + '(' + ', '.join(part.strip() for part in args.split(',') if part.strip()) + ')'
            else:
                signature = name + '()'
            symbols.append({'name': name, 'kind': kind, 'line': line,
                            'signature': _redact(_truncate(signature, MAX_SIGNATURE_CHARS))})
    return symbols


def _markdown_symbols(text):
    symbols = []
    for match in _MD_PATTERN.finditer(text):
        line = text.count('\n', 0, match.start()) + 1
        title = match.group(2).strip()
        symbols.append({'name': title, 'kind': 'heading', 'line': line,
                        'signature': _redact(_truncate('#' * len(match.group(1)) + ' ' + title,
                                                       MAX_SIGNATURE_CHARS))})
    return symbols


def extract_symbols(path, text):
    """Language-aware symbol extraction with explicit, safe degradation."""
    suffix = Path(path).suffix.lower()
    language = _LANGUAGE_BY_SUFFIX.get(suffix)
    if language == 'python':
        symbols = _python_symbols(text)
        if symbols is None:
            return {'language': 'python', 'extraction': 'file_index', 'degraded': True,
                    'reason': 'parse_failed', 'symbols': []}
        return {'language': 'python', 'extraction': 'ast', 'degraded': False,
                'reason': '', 'symbols': symbols}
    if language in {'javascript', 'typescript'}:
        return {'language': language, 'extraction': 'regex', 'degraded': False,
                'reason': 'regex_best_effort', 'symbols': _regex_symbols(text, _JS_PATTERNS)}
    if language == 'powershell':
        return {'language': language, 'extraction': 'regex', 'degraded': False,
                'reason': 'regex_best_effort', 'symbols': _regex_symbols(text, (('function', _PS_PATTERN),))}
    if language == 'markdown':
        return {'language': language, 'extraction': 'regex', 'degraded': False,
                'reason': 'headings_only', 'symbols': _markdown_symbols(text)}
    if language in _DATA_LANGUAGES:
        return {'language': language, 'extraction': 'file_index', 'degraded': False,
                'reason': 'data_file_no_symbols', 'symbols': []}
    return {'language': language or 'unknown', 'extraction': 'file_index', 'degraded': True,
            'reason': 'unsupported_language', 'symbols': []}


def file_digest(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_text_bounded(path, limit=MAX_INDEX_FILE_BYTES):
    data = Path(path).read_bytes()
    if len(data) > limit:
        return None, len(data)
    if b'\x00' in data[:4096]:
        return None, len(data)
    return data.decode('utf-8', errors='replace'), len(data)


def _normalize_scope(project, files):
    project = Path(project).resolve()
    scope = []
    for item in files or []:
        try:
            path = Path(str(item))
        except (TypeError, ValueError):
            continue
        path = path if path.is_absolute() else project / path
        try:
            scope.append(path.resolve().relative_to(project).as_posix())
        except ValueError:
            continue
    return sorted(dict.fromkeys(scope))


def _project_listing(project, max_files=MAX_INDEX_FILES):
    """Path-only discovery listing; sensitive files are never even named."""
    project = Path(project).resolve()
    listing = []
    sensitive_skipped = 0
    truncated = False
    for root, dirs, names in os.walk(project):
        dirs[:] = sorted(name for name in dirs if not is_excluded_dir(name))
        for name in sorted(names):
            if len(listing) >= max_files:
                return listing, sensitive_skipped, True
            candidate = Path(root) / name
            if is_sensitive_path(candidate):
                sensitive_skipped += 1
                continue
            try:
                listing.append(candidate.resolve().relative_to(project).as_posix())
            except (ValueError, OSError):
                continue
    return listing, sensitive_skipped, truncated


def _index_hash(project, scope, listing, entries):
    digest = hashlib.sha256()
    digest.update(f'context-index:{INDEX_VERSION}'.encode('utf-8'))
    digest.update(str(Path(project).resolve()).encode('utf-8'))
    for relative in scope:
        digest.update(b'S' + relative.encode('utf-8'))
    for relative in listing:
        digest.update(b'L' + relative.encode('utf-8'))
    for entry in entries:
        if not entry.get('scope'):
            continue
        payload = json.dumps({'path': entry['path'], 'sha256': entry.get('sha256'),
                              'degraded': bool(entry.get('degraded'))},
                             ensure_ascii=False, sort_keys=True)
        digest.update(payload.encode('utf-8'))
    return digest.hexdigest()


def _file_entry(project, path, in_scope, symbols_limit=MAX_SYMBOLS_PER_FILE):
    project = Path(project).resolve()
    path = Path(path)
    relative = path.resolve().relative_to(project).as_posix()
    stat = path.stat()
    entry = {'path': relative, 'scope': bool(in_scope), 'size': int(stat.st_size),
             'mtime_ns': int(stat.st_mtime_ns), 'sha256': None,
             'language': '', 'extraction': 'file_index', 'degraded': False,
             'reason': '', 'symbols': []}
    if not in_scope:
        entry['reason'] = 'listing_only'
        return entry
    entry['sha256'] = file_digest(path)
    text, size = _read_text_bounded(path)
    if text is None:
        entry['degraded'] = True
        entry['reason'] = 'file_too_large' if size > MAX_INDEX_FILE_BYTES else 'binary_file'
        suffix = path.suffix.lower()
        entry['language'] = _LANGUAGE_BY_SUFFIX.get(suffix, 'unknown')
        return entry
    extracted = extract_symbols(path, text)
    entry.update(extracted)
    entry['symbols'] = extracted['symbols'][:symbols_limit]
    return entry


def build_index(project, files=None, *, max_files=MAX_INDEX_FILES, symbols_limit=MAX_SYMBOLS_PER_FILE):
    """Build a compact, scope-centred index; never reads sensitive files.

    Only in-scope files get a detailed entry (language, symbols, digest). Every
    other project file appears as a path in ``listing`` so the agent can discover
    and read it on demand, without the index carrying its body or metadata.
    """
    project = Path(project).resolve()
    scope = _normalize_scope(project, files)
    listing, sensitive_skipped, truncated = _project_listing(project, max_files=max_files)
    entries = []
    sensitive_scope = []
    missing_scope = []
    for relative in scope:
        path = project / relative
        if not path.is_file():
            missing_scope.append(relative)
            continue
        if is_sensitive_path(path):
            sensitive_scope.append(relative)
            continue
        entries.append(_file_entry(project, path, True, symbols_limit=symbols_limit))
    index = {
        'schema': CONTEXT_SCHEMA,
        'version': INDEX_VERSION,
        'project': str(project),
        'scope': scope,
        'generated_at': _utc_now(),
        'files': entries,
        'listing': listing,
        'excluded': {'directories': sorted(EXCLUDED_DIR_NAMES),
                     'sensitive_files_skipped': sensitive_skipped,
                     'sensitive_scope_files': sensitive_scope,
                     'missing_scope_files': missing_scope,
                     'listing_truncated': truncated},
        'limits': {'max_listing_files': max_files, 'max_symbols_per_file': symbols_limit,
                   'max_scope_file_bytes': MAX_INDEX_FILE_BYTES},
    }
    index['hash'] = _index_hash(project, scope, listing, entries)
    return index


def _summary_path(index_path):
    path = Path(index_path)
    return path.with_name(path.stem + '-summary.json')


def index_summary(index, *, reused=False, path=None, serialized=None):
    if not isinstance(index, dict):
        return {}
    files = index.get('files') or []
    if serialized is None:
        serialized = json.dumps(index, ensure_ascii=False)
    summary = {
        'schema': CONTEXT_SCHEMA,
        'index_version': INDEX_VERSION,
        'hash': index.get('hash'),
        'bytes': len(serialized.encode('utf-8')),
        'file_count': len(files),
        'scope_count': len(index.get('scope') or []),
        'symbol_count': sum(len(entry.get('symbols') or []) for entry in files),
        'degraded_count': sum(1 for entry in files if entry.get('degraded')),
        'listing_count': len(index.get('listing') or []),
        'reused': bool(reused),
    }
    if path is not None:
        summary['path'] = str(path)
    return summary


def load_index(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get('version') != INDEX_VERSION:
        return None
    if not isinstance(value.get('hash'), str) or not isinstance(value.get('files'), list):
        return None
    return value


def write_index(index, path):
    path = Path(path)
    serialized = json.dumps(index, ensure_ascii=False, indent=2)
    _atomic_write_bytes(path, serialized.encode('utf-8'))
    summary = index_summary(index, path=path, serialized=json.dumps(index, ensure_ascii=False))
    _atomic_write_json(_summary_path(path), summary)
    return summary


def read_index_summary(run):
    try:
        value = json.loads(_summary_path(default_index_path(run)).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_fresh(existing, project, files):
    project = Path(project).resolve()
    if existing.get('version') != INDEX_VERSION:
        return False
    if existing.get('project') != str(project):
        return False
    expected = _normalize_scope(project, files)
    if sorted(existing.get('scope') or []) != expected:
        return False
    entries = {entry.get('path'): entry for entry in existing.get('files') or []
               if isinstance(entry, dict) and entry.get('scope')}
    excluded = existing.get('excluded') if isinstance(existing.get('excluded'), dict) else {}
    tolerated = set(excluded.get('sensitive_scope_files') or []) | \
        set(excluded.get('missing_scope_files') or [])
    if sorted(set(entries) | tolerated) != expected or (set(entries) & tolerated):
        return False
    listing, _, _ = _project_listing(project)
    if set(existing.get('listing') or []) != set(listing):
        return False
    for relative in expected:
        if relative in tolerated:
            if relative in set(excluded.get('missing_scope_files') or []) and (project / relative).is_file():
                return False
            continue
        path = project / relative
        entry = entries[relative]
        stat = path.stat()
        if int(stat.st_size) != int(entry.get('size', -1)) or int(stat.st_mtime_ns) != int(entry.get('mtime_ns', -1)):
            return False
        if file_digest(path) != entry.get('sha256'):
            return False
    return True


def ensure_index(project, files, path, *, force=False):
    """Reuse a fresh index, otherwise rebuild; returns ``(index, reused)``."""
    path = Path(path)
    existing = None if force else load_index(path)
    if existing is not None and _is_fresh(existing, project, files):
        _atomic_write_json(_summary_path(path), index_summary(existing, reused=True, path=path))
        return existing, True
    index = build_index(project, files)
    write_index(index, path)
    return index, False


def context_map_sections(index, index_path, run):
    """Prompt lines that point the agent at the index and offload directory."""
    if not isinstance(index, dict):
        return []
    summary = index_summary(index, path=index_path)
    return [
        '\n\n项目索引（精简，不含文件正文；hash %s，版本 %d）：%s'
        % (str(index.get('hash'))[:12], INDEX_VERSION, index_path),
        '\n索引统计：范围文件 %d（符号 %d，仅文件级 %d）；项目文件清单 %d 条，已剔除依赖、缓存、任务运行目录与敏感文件。'
        % (summary['scope_count'], summary['symbol_count'], summary['degraded_count'],
           summary['listing_count']),
        '\n工具输出外置目录：%s' % default_output_dir(run),
        '\n请按索引自行读取相关原文件；需要完整工具结果时用 read 工具读取外置目录中的文件。',
    ]


# ---------------------------------------------------------------------------
# Prompt assembly with no silent truncation
# ---------------------------------------------------------------------------

def compose_prompt(base_prompt, sections):
    parts = [base_prompt if isinstance(base_prompt, str) else '']
    for section in sections or []:
        if section:
            parts.append(str(section))
    return ''.join(parts)


def verify_prompt(final_prompt, base_prompt):
    """Fail loudly if prompt assembly dropped or rewrote a non-empty task text."""
    base = base_prompt if isinstance(base_prompt, str) else ''
    if not isinstance(final_prompt, str):
        raise ValueError('执行提示词拼接后丢失或改写了原始任务文本，已停止以避免静默截断')
    if base and not final_prompt.startswith(base):
        raise ValueError('执行提示词拼接后丢失或改写了原始任务文本，已停止以避免静默截断')
    return final_prompt


def prompt_fields(base_prompt, final_prompt):
    base = base_prompt or ''
    final = final_prompt or ''
    return {
        'prompt_base_bytes': len(base.encode('utf-8')),
        'prompt_base_sha256': hashlib.sha256(base.encode('utf-8')).hexdigest(),
        'prompt_bytes': len(final.encode('utf-8')),
        'prompt_sha256': hashlib.sha256(final.encode('utf-8')).hexdigest(),
        'prompt_preserved': final.startswith(base),
    }


# ---------------------------------------------------------------------------
# Tool-result offloading
# ---------------------------------------------------------------------------

def context_dir(run):
    return Path(run) / 'context'


def default_index_path(run):
    return context_dir(run) / 'project-index.json'


def default_output_dir(run):
    return context_dir(run) / 'tool-outputs'


def _empty_metrics():
    return {'schema': CONTEXT_SCHEMA, 'offload_version': OFFLOAD_VERSION,
            'messages': 0, 'offloaded_count': 0, 'offloaded_bytes': 0,
            'payload_saved_bytes': 0, 'replaced_bytes': 0, 'saved_bytes': 0,
            'reused_count': 0, 'created_count': 0, 'not_trimmed_count': 0,
            'already_offloaded_count': 0, 'input_bytes_before': None,
            'input_bytes_after': None, 'records': [], 'skipped': '', 'error': '',
            'index': {}}


def _line_count(raw):
    if not raw:
        return 0
    return raw.count(b'\n') + (0 if raw.endswith(b'\n') else 1)


def _signals(text, limit=6):
    found = []
    for label, pattern in _SIGNAL_PATTERNS:
        for match in pattern.finditer(text):
            if label == 'exit':
                value = 'exit code=' + match.group(1)
            else:
                value = ' '.join(match.group(0).split())
            value = _redact(_truncate(value, 100))
            if value and value not in found:
                found.append(value)
            if len(found) >= limit:
                return found
    return found


def build_preview(raw, path, *, preview_lines=DEFAULT_PREVIEW_LINES,
                  preview_tail_lines=DEFAULT_PREVIEW_TAIL_LINES,
                  preview_bytes=DEFAULT_PREVIEW_BYTES):
    """Deterministic bounded preview: head + tail, counts, path and signals."""
    text = raw.decode('utf-8', errors='replace')
    all_lines = text.split('\n')
    if all_lines and all_lines[-1] == '':
        all_lines = all_lines[:-1]
    total_lines = len(all_lines)
    head = list(all_lines[:min(preview_lines, total_lines)])
    tail_count = min(preview_tail_lines, max(0, total_lines - len(head)))
    tail = list(all_lines[total_lines - tail_count:]) if tail_count else []
    budget = max(200, int(preview_bytes))
    while len(('\n'.join(head + tail)).encode('utf-8')) > budget and (len(head) > 1 or len(tail) > 1):
        if len(head) >= len(tail) and len(head) > 1:
            head = head[:-1]
        elif len(tail) > 1:
            tail = tail[1:]
        else:
            break
    preview_text = '\n'.join(head + tail)
    preview_raw = preview_text.encode('utf-8')
    long_line_trimmed = len(preview_raw) > budget
    if long_line_trimmed:
        half = max(1, (budget - 100) // 2)
        preview_text = (preview_raw[:half].decode('utf-8', errors='ignore')
                        + '\n[长行中间内容已截取；完整原始内容见文件]\n'
                        + preview_raw[-half:].decode('utf-8', errors='ignore'))
    omitted_lines = max(0, total_lines - len(head) - len(tail))
    omitted_bytes = max(0, len(raw) - len(preview_text.encode('utf-8')))
    parts = [
        OFFLOAD_MARKER + ' 完整原始结果已保存，本消息仅为有限预览。',
        '文件: ' + str(path),
        '原始大小: %d 字节 / %d 行' % (len(raw), total_lines),
        '截取说明: 保留前 %d 行与后 %d 行，省略中间 %d 行；可用 read 工具按 offset/limit 读取完整文件。'
        % (len(head), len(tail), omitted_lines),
    ]
    signals = _signals(text)
    if signals:
        parts.append('检测到: ' + '；'.join(signals))
    return '\n'.join(parts) + '\n--- 预览开始 ---\n' + preview_text + \
        '\n--- 预览结束（中间已省略 %d 行 / %d 字节）---' % (omitted_lines, omitted_bytes)


def load_manifest(directory):
    try:
        value = json.loads((Path(directory) / 'index.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    value.setdefault('schema', CONTEXT_SCHEMA)
    value.setdefault('offload_version', OFFLOAD_VERSION)
    if not isinstance(value.get('outputs'), dict):
        value['outputs'] = {}
    return value


def write_manifest(directory, manifest):
    return _atomic_write_json(Path(directory) / 'index.json', manifest)


def _save_tool_output(raw, directory, manifest, *, preview_lines, preview_tail_lines, preview_bytes):
    digest = hashlib.sha256(raw).hexdigest()
    name = 'tool-' + digest[:20] + '.txt'
    path = Path(directory) / name
    created = False
    if not (path.is_file() and path.stat().st_size == len(raw)):
        _atomic_write_bytes(path, raw)
        created = True
    outputs = manifest.setdefault('outputs', {})
    record = outputs.get(digest)
    if not isinstance(record, dict):
        record = {'digest': digest, 'name': name, 'bytes': len(raw),
                  'lines': _line_count(raw), 'created_at': _utc_now(),
                  'offload_version': OFFLOAD_VERSION}
        outputs[digest] = record
    preview = build_preview(raw, path, preview_lines=preview_lines,
                            preview_tail_lines=preview_tail_lines, preview_bytes=preview_bytes)
    return preview, {'digest': digest, 'path': str(path), 'bytes': len(raw),
                     'lines': int(record['lines'])}, created


def offload_messages(messages, run=None, output_dir=None, *,
                     min_bytes=DEFAULT_OFFLOAD_MIN_BYTES,
                     preview_lines=DEFAULT_PREVIEW_LINES,
                     preview_tail_lines=DEFAULT_PREVIEW_TAIL_LINES,
                     preview_bytes=DEFAULT_PREVIEW_BYTES,
                     max_records=50):
    """Replace large tool results with bounded previews; returns audit metrics.

    Every large string ``role=tool`` content is saved once to a digest-named
    stable file. Non-text content (including multimodal parts) is left exactly as
    it is and annotated as not trimmable. Only ``content`` of ``role=tool``
    messages is ever assigned.
    """
    metrics = _empty_metrics()
    if not isinstance(messages, list):
        metrics['skipped'] = 'no_messages'
        return metrics
    metrics['messages'] = len(messages)
    directory = Path(output_dir) if output_dir is not None else default_output_dir(run)
    manifest = load_manifest(directory)
    manifest_before = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'tool':
            continue
        content = message.get('content')
        if not isinstance(content, str):
            metrics['not_trimmed_count'] += 1
            metrics['records'].append({'action': 'kept', 'reason': 'non_text_tool_content',
                                       'kind': type(content).__name__,
                                       'tool_call_id': message.get('tool_call_id')})
            continue
        if content.startswith(OFFLOAD_MARKER):
            metrics['already_offloaded_count'] += 1
            continue
        raw = content.encode('utf-8')
        if len(raw) < int(min_bytes):
            continue
        try:
            preview, record, created = _save_tool_output(
                raw, directory, manifest, preview_lines=preview_lines,
                preview_tail_lines=preview_tail_lines, preview_bytes=preview_bytes)
        except OSError as error:
            metrics['error'] = type(error).__name__
            metrics['records'].append({'action': 'kept', 'reason': 'save_failed',
                                       'tool_call_id': message.get('tool_call_id')})
            continue
        message['content'] = preview
        metrics['offloaded_count'] += 1
        metrics['offloaded_bytes'] += len(raw)
        metrics['replaced_bytes'] += len(preview.encode('utf-8'))
        metrics['created_count'] += 1 if created else 0
        metrics['reused_count'] += 0 if created else 1
        metrics['records'].append({**record, 'action': 'offloaded', 'created': bool(created),
                                   'tool_call_id': message.get('tool_call_id')})
    metrics['payload_saved_bytes'] = max(0, metrics['offloaded_bytes'] - metrics['replaced_bytes'])
    if json.dumps(manifest, ensure_ascii=False, sort_keys=True) != manifest_before:
        write_manifest(directory, manifest)
    metrics['records'] = metrics['records'][-max_records:]
    return metrics


def prepare_request(body, run=None, *, output_dir=None, **options):
    """Offload large tool results in place and return before/after audit bytes.

    Never raises for a malformed body: context control must not become a
    model-call failure. When it cannot act, the original content is kept and the
    reason is recorded.
    """
    metrics = _empty_metrics()
    if not isinstance(body, dict):
        metrics['skipped'] = 'body_not_object'
        return metrics
    messages = body.get('messages')
    if not isinstance(messages, list):
        metrics['skipped'] = 'no_messages'
        return metrics
    before = serialized_bytes(body)
    try:
        details = offload_messages(messages, run=run, output_dir=output_dir, **options)
    except Exception as error:  # noqa: BLE001 - never block the request on context control
        metrics['error'] = type(error).__name__
        metrics['input_bytes_before'] = before
        metrics['input_bytes_after'] = serialized_bytes(body)
        metrics['saved_bytes'] = max(0, metrics['input_bytes_before'] - metrics['input_bytes_after'])
        return metrics
    metrics.update(details)
    metrics['input_bytes_before'] = before
    metrics['input_bytes_after'] = serialized_bytes(body)
    metrics['saved_bytes'] = max(0, metrics['input_bytes_before'] - metrics['input_bytes_after'])
    metrics['index'] = read_index_summary(run) if run is not None else {}
    return metrics


def context_fields(metrics):
    """Bounded task-record fields; explicitly labels bytes as audit, not tokens."""
    metrics = metrics if isinstance(metrics, dict) else {}
    index = metrics.get('index') if isinstance(metrics.get('index'), dict) else {}
    return {
        'context_schema': CONTEXT_SCHEMA,
        'context_offload_version': OFFLOAD_VERSION,
        'context_bytes_before': metrics.get('input_bytes_before'),
        'context_bytes_after': metrics.get('input_bytes_after'),
        'context_bytes_saved': metrics.get('saved_bytes', 0),
        'context_offloaded_count': metrics.get('offloaded_count', 0),
        'context_offloaded_bytes': metrics.get('offloaded_bytes', 0),
        'context_preview_bytes': metrics.get('replaced_bytes', 0),
        'context_reused_count': metrics.get('reused_count', 0),
        'context_created_count': metrics.get('created_count', 0),
        'context_not_trimmed_count': metrics.get('not_trimmed_count', 0),
        'context_already_offloaded_count': metrics.get('already_offloaded_count', 0),
        'context_skipped_reason': metrics.get('skipped') or '',
        'context_error': metrics.get('error') or '',
        'context_outputs': list(metrics.get('records') or [])[-20:],
        'context_index': index,
        'context_note': ('context_* 是上下文外置的字节审计，不等于 token 精确节省；'
                        'token 计量见 meter_*，实际用量见 usage_*，费用按 usage 结算'),
    }


def read_offloaded(path, offset=1, limit=None):
    """Read a saved tool output by 1-based line offset, for verification/agents."""
    path = Path(path)
    text = path.read_text(encoding='utf-8', errors='replace')
    lines = text.split('\n')
    start = max(0, int(offset) - 1)
    selected = lines[start:] if limit is None else lines[start:start + max(0, int(limit))]
    return {'path': str(path), 'total_lines': len(lines), 'offset': start + 1,
            'text': '\n'.join(selected)}


def list_offloaded(run):
    directory = default_output_dir(run)
    manifest = load_manifest(directory)
    rows = []
    for digest, record in sorted((manifest.get('outputs') or {}).items()):
        if not isinstance(record, dict):
            continue
        rows.append({'digest': digest, 'path': str(directory / str(record.get('name') or '')),
                     'bytes': record.get('bytes'), 'lines': record.get('lines')})
    return rows
