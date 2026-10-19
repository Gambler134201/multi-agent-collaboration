'use strict';
/**
 * Model Desk workbench-record write policy.
 *
 * Pure decisions: this module never writes, never mutates and never retries.
 * `decide(toolName, args, config)` returns a reason string when one DSH tool
 * call must be denied before its body runs, and `undefined` when the call is
 * not one of the entries this policy governs.
 *
 * Scope of the rule: the project's `.model-desk` workbench record directory.
 * A path is covered when any of its components is literally `.model-desk`
 * (case-insensitively) or when it resolves into such a directory through a
 * symlink/junction alias.  `.model-desk-backup`, `.model-desk2` and
 * `.model-desktop` are deliberately *not* matched.
 *
 * This is a static, best-effort guard on the arguments the model supplied.  It
 * is not OS-level isolation: a terminal command whose target is computed at
 * runtime (variables, encoded text, an external program, a script file) cannot
 * be proven from the command text, and such commands are refused conservatively
 * only when they are unclassifiable *and* mention the protected directory.
 */

const fs = require('fs');
const path = require('path');

const PROTECTED_NAME = '.model-desk';
const DENY_MARKER = '该目录由工作台管理';
const PROBE_TOOL = '__model_desk_record_guard_probe__';
const PROBE_REASON = 'model-desk-record-guard-probe-ok';

/** Tool name → argument keys that carry the write target. */
const WRITE_TARGETS = {
  write: ['file_path'],
  edit: ['file_path'],
  str_replace_editor: ['path'],
};
/** Tools whose whole command text is judged. */
const TERMINAL_TOOLS = new Set(['bash', 'pwsh']);

/**
 * Commands that can only read.  A terminal command that mentions the protected
 * directory is allowed only when every pipeline segment starts with one of
 * these verbs and the text carries no redirection, substitution or separator.
 */
const READONLY_VERBS = new Set([
  'get-content', 'gc', 'cat', 'type', 'get-childitem', 'gci', 'dir', 'ls',
  'select-string', 'sls', 'findstr', 'rg', 'grep', 'get-filehash', 'test-path',
  'get-item', 'resolve-path', 'get-itemproperty', 'measure-object', 'wc',
  'head', 'tail', 'more', 'stat', 'du',
  'echo', 'write-output', 'write-host', 'select-object',
  'format-list', 'format-table', 'out-string', 'where', 'which', 'strings',
  'od', 'cmp',
]);

/** Verbs that destroy or replace data. */
const DESTRUCTIVE_RE = new RegExp(
  '(\\b(remove-item|remove|rmdir|rd|rm|del|erase|clear-content|set-content|add-content'
  + '|out-file|new-item|mkdir|md|move-item|move|mv|ren|rename-item|rename|copy-item'
  + '|copy|cp|xcopy|robocopy|chmod|chown|attrib|icacls|takeown|rmtree|unlink|truncate'
  + '|shutil|dd|format|diskpart|fsutil)(?![\\w-])'
  + '|\\bgit\\b[^\\r\\n]*\\b(rm|clean|checkout|reset|restore|stash)\\b'
  + '|\\bfind\\b[^\\r\\n]*-(delete|exec|execdir)\\b'
  + '|\\b(sed|perl)\\b[^|]*-i\\b)',
  'i',
);
/** Recursive deletion/overwrite markers. */
const RECURSIVE_RE = /(-recurse\b|\/s\b|--recursive\b|(^|\s)-[a-z]{0,3}r[a-z]{0,3}\b|-rf\b|-fr\b)/i;
/** Raw path-shaped mention of the protected directory inside a command. */
const RAW_MENTION_RE = /\.model-desk(?![-A-Za-z0-9_.])/i;

function isWindows() {
  return process.platform === 'win32';
}

function separator() {
  return isWindows() ? '\\' : '/';
}

function stripQuotes(value) {
  let text = String(value == null ? '' : value).trim();
  if (text.length >= 2
      && ((text[0] === '"' && text[text.length - 1] === '"')
          || (text[0] === "'" && text[text.length - 1] === "'"))) {
    text = text.slice(1, -1).trim();
  }
  return text;
}

/** Canonical, comparison-ready form: separators normalised, case folded. */
function normalizePath(value) {
  const text = stripQuotes(value);
  if (!text) return '';
  if (isWindows()) {
    let win = text.replace(/^\\\\\?\\/, '').replace(/\//g, '\\');
    win = path.win32.normalize(win).replace(/\\+$/, '');
    if (/^[a-zA-Z]:$/.test(win)) win += '\\';
    return win.toLowerCase();
  }
  const posix = path.posix.normalize(text).replace(/\/+$/, '');
  return posix || '/';
}

/** Resolve one candidate path against a base directory (default: process cwd). */
function resolveAgainst(base, candidate) {
  const text = stripQuotes(candidate);
  if (!text) return '';
  const root = base ? String(base) : process.cwd();
  if (isWindows()) {
    if (/^[a-zA-Z]:[\\/]/.test(text) || text.startsWith('\\\\')) return normalizePath(text);
    return normalizePath(path.win32.resolve(root, text));
  }
  if (text.startsWith('/')) return normalizePath(text);
  return normalizePath(path.posix.resolve(root, text));
}

function components(value) {
  return String(value == null ? '' : value).split(/[\\/]+/).filter(Boolean);
}

/** True when any component is literally the workbench record directory name. */
function hasProtectedComponent(value) {
  return components(value).some((part) => part.toLowerCase() === PROTECTED_NAME);
}

/**
 * Real path of `target` even when the leaf does not exist yet: the longest
 * existing prefix is resolved (following symlinks/junctions) and the remaining
 * segments are appended.  Never throws.
 */
function realpathDeep(target) {
  let current = path.resolve(String(target));
  const suffix = [];
  for (let step = 0; step < 40; step += 1) {
    try {
      const real = typeof fs.realpathSync.native === 'function'
        ? fs.realpathSync.native(current)
        : fs.realpathSync(current);
      return suffix.length ? path.join(real, ...suffix.slice().reverse()) : real;
    } catch (error) {
      const parent = path.dirname(current);
      if (parent === current) {
        return suffix.length ? path.join(current, ...suffix.slice().reverse()) : current;
      }
      suffix.push(path.basename(current));
      current = parent;
    }
  }
  return path.resolve(String(target));
}

/** Every location the protected directory occupies for this project. */
function protectedRoots(config) {
  const roots = [];
  const root = config && config.root ? String(config.root).trim() : '';
  if (root) {
    const lexical = path.join(root, PROTECTED_NAME);
    roots.push(normalizePath(lexical));
    roots.push(normalizePath(realpathDeep(lexical)));
    const realRoot = realpathDeep(root);
    if (normalizePath(realRoot) !== normalizePath(root)) {
      roots.push(normalizePath(path.join(realRoot, PROTECTED_NAME)));
      roots.push(normalizePath(realpathDeep(path.join(realRoot, PROTECTED_NAME))));
    }
  }
  return roots.filter((value, index) => value && roots.indexOf(value) === index);
}

function isEqualOrInside(target, root) {
  if (!target || !root) return false;
  if (target === root) return true;
  return target.startsWith(root + separator());
}

/** Strict ancestor: the directory that *contains* the protected directory. */
function isStrictAncestor(target, roots) {
  if (!target) return false;
  return roots.some((root) => root.startsWith(target + separator()));
}

function insideProtected(target, roots) {
  if (!target) return false;
  return roots.some((root) => isEqualOrInside(target, root));
}

function mentionsProtected(target, roots) {
  if (!target) return false;
  if (hasProtectedComponent(target)) return true;
  const real = normalizePath(realpathDeep(target));
  return hasProtectedComponent(real) || insideProtected(target, roots) || insideProtected(real, roots);
}

function denyInside(display) {
  return '已拒绝：' + display + ' 位于工作台记录目录 .model-desk 内，'
    + DENY_MARKER + '，执行模型不得写入、修改、删除或移动其中的记录。'
    + '请改用项目内的正常源码文件；不要换用其他工具、脚本或命令重试这个目录。';
}

function denyAncestor(display) {
  return '已拒绝：' + display + ' 是工作台记录目录 .model-desk 的上级目录，'
    + DENY_MARKER + '，删除或移动它会连同工作台记录一起破坏。'
    + '请停止这一操作并报告主脑；不要换用其他工具、脚本或等价命令重试。';
}

/** Decide one resolved target path. `destructive` enables the ancestor rule. */
function inspectTarget(target, config, destructive) {
  const roots = protectedRoots(config);
  const real = normalizePath(realpathDeep(target));
  if (hasProtectedComponent(target) || hasProtectedComponent(real)) return denyInside(target);
  if (insideProtected(target, roots) || insideProtected(real, roots)) return denyInside(target);
  if (destructive && (isStrictAncestor(target, roots) || isStrictAncestor(real, roots))) {
    return denyAncestor(target);
  }
  return undefined;
}

function cwdFor(args, config) {
  const declared = args && typeof args.workdir === 'string' ? args.workdir.trim() : '';
  const base = config && config.root ? String(config.root) : process.cwd();
  if (declared) return resolveAgainst(base, declared);
  return normalizePath(base);
}

/** Every path-shaped candidate inside a terminal command text. */
function extractTokens(command) {
  const tokens = [];
  const quoted = /"([^"]*)"|'([^']*)'/g;
  let match = quoted.exec(command);
  while (match !== null) {
    const value = match[1] !== undefined ? match[1] : match[2];
    if (value && value.trim()) tokens.push(value.trim());
    match = quoted.exec(command);
  }
  for (const raw of String(command).split(/\s+/)) {
    const token = raw.replace(/^[|&;()<>]+/, '').replace(/[|&;()<>]+$/, '');
    if (token) tokens.push(token);
  }
  return tokens;
}

/**
 * True when a token could name a filesystem location.  Only switches and shell
 * punctuation are excluded: a bare relative name (`build`, `.model-desk-backup`)
 * is a real target and must be judged against the protected directory.
 */
function looksLikeTarget(token) {
  const text = stripQuotes(token);
  if (!text) return false;
  if (text.startsWith('-')) return false;
  if (/^\/[A-Za-z]{1,3}$/.test(text)) return false;
  if (/[|&;<>]+/.test(text)) return false;
  return true;
}

/**
 * A command is provably read-only when every pipeline segment starts with a
 * known read verb and the text contains no redirection, command separator,
 * substitution or subexpression that could hide a write.
 */
function isProvablyReadOnly(command) {
  const text = String(command);
  // Read-oriented programs also expose mutating/executing options.  Do not
  // grant a write exemption from the verb alone (find -delete, sort -o,
  // tree -o, diff --output, rg --pre, and script-capable pagers/editors).
  if (/(?:--pre(?:[=\s]|$)|--pre-glob(?:[=\s]|$)|--passthru(?:[=\s]|$))/i.test(text)) return false;
  if (/[<>`$%^]/.test(text)) return false;
  if (/[\r\n]/.test(text)) return false;
  if (/&&|\|\||;|&/.test(text)) return false;
  if (/[(){}]/.test(text)) return false;
  const segments = text.split('|');
  for (const segment of segments) {
    const head = segment.trim().split(/\s+/)[0];
    if (!head) return false;
    const verb = head.split(/[\\/]/).pop().replace(/\.exe$/i, '').toLowerCase();
    if (!READONLY_VERBS.has(verb)) return false;
  }
  return true;
}

/** Narrow compound-command handling, not a shell interpreter. Unknown syntax
 * falls back to the original conservative whole-command check. In particular,
 * never split location changes, scripts, substitutions or mutating pipelines:
 * their targets cannot safely be resolved independently against the same cwd.
 */
function simpleCommands(command) {
  const text = String(command);
  if (/[`$%^<>(){}\r\n]/.test(text)) return null;
  let quote = '', start = 0;
  const parts = [];
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === '"' || ch === "'") {
      if (text[i - 1] === '\\') return null; // unfamiliar escaping
      if (!quote) quote = ch;
      else if (quote === ch) quote = '';
    }
    if (quote) continue;
    if (ch === ';' || text.slice(i, i + 2) === '&&' || text.slice(i, i + 2) === '||') {
      parts.push(text.slice(start, i).trim());
      if (ch !== ';') i += 1;
      start = i + 1;
    } else if (ch === '&') return null;
  }
  if (quote) return null;
  parts.push(text.slice(start).trim());
  if (parts.length < 2 || parts.some(part => !part)) return null;
  const literalMutation = /^(?:remove-item|rm|rmdir|rd|del|erase|clear-content|set-content|add-content|out-file|new-item|mkdir|md|move-item|mv|ren|rename-item|copy-item|cp)(?:\.exe)?\s/i;
  if (!parts.every(part => isProvablyReadOnly(part)
      || (literalMutation.test(part) && !/[|&]/.test(part)))) return null;
  return parts;
}

/** Deleting/moving an ancestor of the protected directory.  Fail closed. */
function destructiveAncestor(command, base, tokens, config) {
  const text = String(command);
  const parts = simpleCommands(text);
  if (parts) {
    for (const part of parts) {
      const reason = destructiveAncestor(part, base, extractTokens(part), config);
      if (reason) return reason;
    }
    return undefined;
  }
  if (!DESTRUCTIVE_RE.test(text)) return undefined;
  const roots = protectedRoots(config);
  if (!roots.length) return undefined;
  const recursive = RECURSIVE_RE.test(text);
  const here = resolveAgainst(base, '.');
  let targets = tokens.filter(looksLikeTarget).map((token) => resolveAgainst(base, token)).filter(Boolean);
  // Wildcards and implicit pipeline/git targets can include the record store
  // without ever spelling its name (e.g. Remove-Item .model*, git clean -fdx).
  if ((/\bgit\b/i.test(text)&&!/(?:^|\s)--\s+/.test(text))
      || (/\|/.test(text)&&/\b(get-childitem|gci|dir|ls|find)\b/i.test(text))
      || /\bfind\s+\.(?:\s|$)/i.test(text)
      || /(?:rmtree|RemoveDirectory|remove_all)\s*\(\s*['"]\.['"]/i.test(text)) {
    if (isStrictAncestor(here, roots) || insideProtected(here, roots)) return denyAncestor(here);
  }
  const wildcard = tokens.some((token) => {
    const value = stripQuotes(token);
    return value === '.' || value === '..' || value === '*' || value === './*' || value === '.\\*' || value === '.*';
  });
  if (wildcard || (!targets.length && recursive)) targets = targets.concat([here]);
  for (const target of targets) {
    if (wildcardOverlaps(target, roots)) return denyAncestor(target);
    if (isStrictAncestor(target, roots) || roots.some((root) => root === target)) {
      return isStrictAncestor(target, roots) ? denyAncestor(target) : denyInside(target);
    }
  }
  return undefined;
}

function wildcardOverlaps(target, roots) {
  if (!/[*?\[]/.test(target)) return false;
  const prefixes=value=>{
    const rows=[];let current=value;
    while(current){rows.push(current);const parent=path.dirname(current);if(parent===current)break;current=parent;}
    return rows;
  };
  for(const prefix of prefixes(target)){
    if(!/[*?\[]/.test(prefix))continue;
    // Conservative glob overlap; * may span separators. Character classes
    // match any character here so an unfamiliar class cannot bypass protection.
    let expression='';
    for(let i=0;i<prefix.length;i++){
      const ch=prefix[i];
      if(ch==='*')expression+='.*';
      else if(ch==='?')expression+='.';
      else if(ch==='['){const end=prefix.indexOf(']',i+1);if(end>=0){expression+='.';i=end;}else expression+='\\[';}
      else expression+=ch.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    }
    const matcher=new RegExp('^'+expression+'$','i');
    if(roots.some(root=>prefixes(root).some(value=>matcher.test(value))))return true;
  }
  return false;
}

function decideTerminal(args, config) {
  const command = args && typeof args.command === 'string' ? args.command : '';
  if (!command.trim()) return undefined;
  const base = cwdFor(args, config);
  const roots = protectedRoots(config);
  const tokens = extractTokens(command);
  const targets = tokens.map((token) => resolveAgainst(base, token)).filter(Boolean);
  const mentioned = targets.filter((target) => mentionsProtected(target, roots));
  if (mentioned.length || RAW_MENTION_RE.test(command)) {
    if (isProvablyReadOnly(command)) return undefined;
    const display = mentioned[0] || tokens.find((token) => RAW_MENTION_RE.test(token)) || PROTECTED_NAME;
    return denyInside(display);
  }
  return destructiveAncestor(command, base, tokens, config);
}

/** Tool names whose calls this policy judges. */
function isGuardedTool(toolName) {
  const name = String(toolName || '');
  return name === PROBE_TOOL
    || TERMINAL_TOOLS.has(name)
    || Object.prototype.hasOwnProperty.call(WRITE_TARGETS, name);
}

/**
 * Decide one tool call.
 * @returns {string|undefined} a denial reason, or undefined to leave the call alone.
 */
function decide(toolName, args, config) {
  const name = String(toolName || '');
  if (name === PROBE_TOOL) return PROBE_REASON;
  const input = args && typeof args === 'object' ? args : {};
  if (TERMINAL_TOOLS.has(name)) return decideTerminal(input, config || {});
  const keys = WRITE_TARGETS[name];
  if (!keys) return undefined;
  if (name === 'str_replace_editor' && String(input.command || '').toLowerCase() === 'view') {
    return undefined;
  }
  const base = cwdFor(input, config || {});
  for (const key of keys) {
    const value = input[key];
    if (typeof value !== 'string' || !value.trim()) continue;
    const verdict = inspectTarget(resolveAgainst(base, value), config || {}, false);
    if (verdict) return verdict;
  }
  return undefined;
}

/** One executing worker's refusal boundary. After a record-policy refusal,
 * pause writes and terminal calls across tools, so a model cannot retry the
 * same operation through a different tool. Direct reads and the final report
 * remain possible. A new controller-started execution gets a fresh guard.
 * This is deliberately not a persistent session lock or OS sandbox.
 */
function createGuard(config) {
  let stopped = false;
  return (toolName, args) => {
    const name = String(toolName || '');
    if (name === PROBE_TOOL) return PROBE_REASON;
    if (!isGuardedTool(name)) return undefined;
    if (name === 'str_replace_editor' && String(args && args.command || '').toLowerCase() === 'view') return undefined;
    if (stopped) return '已暂停本轮执行的写入和终端工具：此前触发工作台记录保护，'
      + DENY_MARKER + '。请保留拒绝原因并向主脑报告，不要换工具或改写命令重试；仍可直接读取文件。';
    let reason;
    try { reason = decide(name, args, config); }
    catch (_) { reason = '工作台记录保护判断失败，已拒绝调用；请报告主脑。'; }
    if (reason) {
      stopped = true;
      return reason + ' 本轮写入和终端工具已暂停；请直接报告主脑，非必要缓存清理可以省略。';
    }
    return undefined;
  };
}

module.exports = {
  PROTECTED_NAME,
  DENY_MARKER,
  PROBE_TOOL,
  PROBE_REASON,
  WRITE_TARGETS,
  TERMINAL_TOOLS,
  READONLY_VERBS,
  normalizePath,
  resolveAgainst,
  realpathDeep,
  protectedRoots,
  hasProtectedComponent,
  isProvablyReadOnly,
  isGuardedTool,
  createGuard,
  decide,
};
