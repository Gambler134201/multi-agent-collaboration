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
 * be proven from command text. Legacy/v1/v2 retain conservative text scanning;
 * v3 checks only structured paths and a bounded direct-operand contract, never
 * treating unsupported syntax as a proven violation.
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
  'true',
]);

/** Verbs that destroy or replace data. A filename suffix such as `.md`
 * is not the `md` command; keep executable paths and real command verbs covered.
 */
const DESTRUCTIVE_RE = new RegExp(
  '(\\b(?<!\\.)(remove-item|remove|rmdir|rd|rm|del|erase|clear-content|set-content|add-content'
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
  const declared = args && typeof (args.workdir || args.cwd) === 'string' ? (args.workdir || args.cwd).trim() : '';
  const base = config && config.root ? String(config.root) : process.cwd();
  if (declared) return resolveAgainst(base, declared);
  return normalizePath(base);
}

/** Optional task-level contract; missing means legacy, malformed never means full.
 * The controller freezes this independently of the deliverable file list.
 */
function taskPolicy(config) {
  if(!Object.prototype.hasOwnProperty.call(config,'task_policy'))return null;
  const p=config.task_policy;
  if(!['edit','chat'].includes(config.kind)||!p||typeof p!=='object'||Array.isArray(p)||![1,2,3].includes(p.schema)
    ||Object.keys(p).sort().join(',')!==(p.schema===1?'schema,write_scope':'deny_paths,home,schema,write_scope')
    ||!['workspace','declared_files','read_only'].includes(p.write_scope)
    ||(config.kind==='chat')!==(p.write_scope==='read_only'))throw Error('任务写入范围快照无效');
  if(p.schema>=2&&(typeof p.home!=='string'||!path.isAbsolute(p.home)||!Array.isArray(p.deny_paths)
    ||p.deny_paths.length>128||p.deny_paths.some(v=>typeof v!=='string'||!path.isAbsolute(v))))
    throw Error('任务受保护路径声明无效');
  for(const rows of [config.files,config.readonly_files])
    if(!Array.isArray(rows)||rows.some(s=>typeof s!=='string'||!s.trim()))throw Error('任务文件声明无效');
  return p;
}

function taskWriteReason(config,value) {
  const contract=taskPolicy(config);if(!contract)return undefined;
  if(contract.write_scope==='read_only')return '只读任务没有写入权限';
  if(typeof value!=='string'||!value.trim())return undefined; // native argument error, not a scope refusal
  const base=config.root||config.project;
  const identities=v=>{
    const lexical=resolveAgainst(base,v);
    return [lexical,normalizePath(realpathDeep(lexical))];
  };
  const target=identities(value);
  const matches=rows=>rows.some(v=>identities(v).some(p=>target.includes(p)));
  if(matches(config.readonly_files))return '任务只读上下文不可修改';
  if(contract.write_scope==='declared_files'&&!matches(config.files))return '文件未列入本任务可写范围';
  return undefined;
}

/** Frozen path policy; no generic auth/token filename or payload-word guessing.
 * All adapters consume this same matcher. v1 and legacy keep their old rules.
 */
function protectedPathChecker(config){
  const p=taskPolicy(config);if(!p||p.schema<2)return null;
  const base=config.root||config.project;
  const roots=[...p.deny_paths,...(config.protected_roots||[]),
    ...(p.schema===3?['.model-desk','.git'].map(n=>path.resolve(base,n)):[]),
    ...['.codebuddy','.workbuddy','.codex','.dsh','.zcode'].map(n=>path.resolve(base,n))]
    .flatMap(v=>[normalizePath(v),normalizePath(realpathDeep(v))]);
  return (value,cwd=base,destructive=false)=>{
    if(typeof value!=='string'||!value.trim())return undefined;
    // File APIs commonly accept ~. Bind it to the declaring controller's home,
    // not the hook subprocess's ambient user/profile environment.
    if(/^~(?:[\\/]|$)/.test(value))value=p.home+value.slice(1);
    const lexical=resolveAgainst(cwd,value),real=normalizePath(realpathDeep(lexical));
    if([lexical,real].some(v=>hasProtectedComponent(v)||components(v).some(
      n=>n.toLowerCase()==='.git'||/^\.env(?:\.|$)/i.test(n))))
      return '工作台记录、Git 元数据或认证配置不作为执行目标';
    if([lexical,real].some(v=>roots.some(root=>isEqualOrInside(v,root)
      ||(destructive&&isEqualOrInside(root,v)))))
      return '目标属于任务声明的受保护路径';
    return undefined;
  };
}

/** Reuse existing literal-cd analysis; this does not parse/execute shell code. */
function terminalFacts(command,base){
  const pending=[{command:terminalAnalysis(command),base}],contexts=[],locations=[],seen=new Set();
  while(pending.length&&seen.size<32){
    const item=pending.pop(),key=item.base+'\0'+item.command;
    if(seen.has(key))continue;seen.add(key);
    const change=leadingLocation(item.command,item.base);
    if(!change||change.error){contexts.push(item);continue;}
    for(const cwd of change.targets){locations.push(cwd);pending.push({command:change.remainder,base:cwd});}
    if(!change.onSuccessOnly)pending.push({command:change.remainder,base:item.base});
  }
  return {contexts,locations:[...new Set(locations)],overflow:!!pending.length};
}

function directTerminalPolicy(config){return taskPolicy(config)?.schema===3;}

/** v3 deliberately does NOT scan arbitrary words/quoted payloads for paths.
 * This is a small direct-invocation contract, not a shell parser or safety
 * proof. Unsupported syntax/options/programs yield no inferred targets. The
 * native tool still receives the exact original command. Legacy stays below.
 */
function directTerminalTargets(command,base){
  const targets=[];
  if(typeof command!=='string'||command.length>16384)return targets;
  const groups=commandGroups(command);
  if(!groups)return targets;
  let bases=[base],offset=0;
  for(const group of groups){
    const tail=command.slice(offset+group.length),separator=/^(?:&&|\|\||;)/.exec(tail)?.[0];
    offset+=group.length+(separator?.length||0);
    // Expansions, escapes, providers, pipelines and redirections are opaque.
    if(/[`$%^|&<>(){}*?\[\]\r\n]/.test(group)){bases=[];continue;}
    const raw=group.trim().match(/"[^"\r\n]*"|'[^'\r\n]*'|[^\s"']+/g)||[];
    if(raw.join(' ')!==group.trim().replace(/\s+/g,' ')){bases=[];continue;}
    const words=raw.map(stripQuotes),verb=(words.shift()||'').toLowerCase().replace(/\.exe$/,'');
    const read=['cat','type','get-content','gc','get-item','test-path','get-childitem','gci','ls','dir'];
    const remove=['rm','rmdir','rd','del','erase','remove-item','clear-content'];
    const content=['set-content','add-content','out-file'];
    const copy=['cp','copy','copy-item'],move=['mv','move','move-item','rename-item'];
    const location=['cd','chdir','set-location','sl'];
    if(verb==='git'){
      if(['push','commit','merge','worktree','switch','checkout','reset','clean'].includes(words[0]))
        targets.push({gitMutation:true});
      continue;
    }
    if(['echo','write-output','write-host'].includes(verb))continue;
    if(![...read,...remove,...content,...copy,...move,...location].includes(verb)){bases=[];continue;}
    const values=[];let ended=false,valid=true;
    const unary=new Set(['-force','-recurse','--force','--recursive','-f','-r','-rf','-fr','/s','/q','-raw','-hidden']);
    for(let i=0;i<words.length;i++){
      const word=words[i],flag=word.toLowerCase();
      if(!ended&&flag==='--'){ended=true;continue;}
      if(!ended&&['-path','-literalpath','-filepath','-destination'].includes(flag)){
        if(!words[i+1]){valid=false;break;}values.push(words[++i]);continue;
      }
      if(!ended&&['-value','-exclude','-include','-filter','-encoding','-erroraction'].includes(flag)){
        if(['-exclude','-include','-filter'].includes(flag)&&!read.includes(verb)){valid=false;break;}
        if(!words[i+1]){valid=false;break;}i++;continue;
      }
      if(!ended&&unary.has(flag))continue;
      if(!ended&&word.startsWith('-')){valid=false;break;}
      values.push(word);
    }
    if(!valid||!values.length){if(location.includes(verb))bases=[];continue;}
    let paths=values;
    if(content.includes(verb))paths=values.slice(0,1); // remaining positionals are data
    if((copy.includes(verb)||move.includes(verb))&&values.length!==2)continue;
    if(location.includes(verb)&&values.length!==1){bases=[];continue;}
    const rows=[];
    for(let i=0;i<paths.length;i++)for(const cwd of (bases.length?bases:
      path.isAbsolute(paths[i])||/^~(?:[\\/]|$)/.test(paths[i])?[base]:[]))rows.push({value:paths[i],base:cwd,
      write:remove.includes(verb)||content.includes(verb)||move.includes(verb)||(copy.includes(verb)&&i===1),
      destructive:remove.includes(verb)||move.includes(verb),location:location.includes(verb)});
    targets.push(...rows);
    if(location.includes(verb)){
      // Only && confirms that cd succeeded before the next invocation. A
      // failed/conditional/dynamic location is unknown, never guessed as cwd.
      bases=separator==='&&'&&!paths.some(p=>p.startsWith('~'))
        ?[...new Set(rows.map(r=>resolveAgainst(r.base,r.value)))]:[];
    }
  }
  return targets;
}

function directTerminalReason(config,args){
  const check=protectedPathChecker(config),base=cwdFor(args,config);
  if(args.workdir&&args.cwd&&resolveAgainst(config.root,args.workdir)!==resolveAgainst(config.root,args.cwd))
    return '工作目录参数 cwd 与 workdir 冲突；请报告主脑。';
  for(const row of directTerminalTargets(args.command,base)){
    if(row.gitMutation)return '执行任务不修改 Git 历史或向远端发布';
    const reason=check(row.value,row.base,row.destructive);if(reason)return reason;
    if(row.write){
      const value=/^~(?:[\\/]|$)/.test(row.value)?config.task_policy.home+row.value.slice(1):row.value;
      const scoped=taskWriteReason(config,resolveAgainst(row.base,value));if(scoped)return scoped;
    }
  }
  return undefined;
}

function protectedArgumentsReason(config,name,args={}){
  const check=protectedPathChecker(config);if(!check)return undefined;
  const base=resolveAgainst(config.root||config.project,args.workdir||args.cwd||'.');
  const read=['read','Read','grep','Grep','find','Glob','ls','NotebookRead'].includes(name)
    ||(name==='str_replace_editor'&&args.command==='view');
  const write=['write','edit','Write','Edit','MultiEdit','NotebookEdit'].includes(name)
    ||(name==='str_replace_editor'&&!read);
  const inspect=(value,cwd=base,destructive=false)=>{
    // Existing controller-owned read-only context remains readable. A link out
    // of that tree cannot use the exception.
    if(read&&config.context_root&&typeof value==='string'){
      const target=resolveAgainst(cwd,value),context=normalizePath(config.context_root);
      if(isEqualOrInside(target,context)&&isEqualOrInside(normalizePath(realpathDeep(target)),
          normalizePath(realpathDeep(context))))return undefined;
    }
    return check(value,cwd,destructive);
  };
  const locationReason=inspect(base);if(locationReason)return locationReason;
  for(const value of ['path','file_path','filePath','notebook_path','target','directory'].map(k=>args[k])){
    const reason=inspect(value,base,write);if(reason)return reason;
  }
  if(!['bash','pwsh','powershell','Bash','PowerShell'].includes(name)||typeof args.command!=='string')return undefined;
  if(directTerminalPolicy(config))return directTerminalReason(config,args);
  const facts=terminalFacts(args.command,base);
  if(facts.overflow)return '复合命令过于复杂，无法核实工作目录；请报告主脑。';
  for(const value of facts.locations){const reason=inspect(value);if(reason)return reason;}
  for(const item of facts.contexts)for(const token of extractTokens(item.command)){
    if(!looksLikeTarget(token)||/^[a-z]+:\/\//i.test(stripQuotes(token)))continue;
    const reason=inspect(stripQuotes(token),item.base,DESTRUCTIVE_RE.test(item.command));if(reason)return reason;
  }
  return undefined;
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
function commandGroups(text) {
  // Separate literal command groups before deciding whether a listing feeds a
  // mutation. Unknown quoting/substitution retains the conservative old check.
  if (/[`\r\n(){}]/.test(text)) return null;
  const parts=[];let quote='',start=0;
  for(let i=0;i<text.length;i++){
    const ch=text[i];
    if(ch==='"'||ch==="'"){
      if(text[i-1]==='\\')return null;
      if(!quote)quote=ch;else if(quote===ch)quote='';
    }
    if(quote)continue;
    if(ch===';'||text.slice(i,i+2)==='&&'||text.slice(i,i+2)==='||'){
      parts.push(text.slice(start,i));if(ch!==';')i++;start=i+1;
    }
  }
  if(quote)return null;
  parts.push(text.slice(start));
  return parts;
}

/** Analysis text only; the host still receives the original command. A whole
 * literal output invocation has no filesystem target. Do not let several
 * independent guards reinterpret its payload as a path or a destructive verb.
 * This common subset works without guessing Bash/PowerShell/cmd: no expansion,
 * escaped/concatenated quoting, unquoted operators, switches to another command,
 * redirection or compound invocations. Anything else retains the old analysis.
 * This is deliberately not a shell parser or a general read-only exemption.
 */
function terminalAnalysis(command) {
  if(typeof command!=='string'||command.length>16384)return command;
  if(/[\r\n`$%!^]/.test(command))return command;
  const head=/^[ \t]*(?:echo|write-output|write-host)(?=[ \t]|$)/i.exec(command);
  if(!head)return command;
  let i=head[0].length;
  while(i<command.length){
    if(/[ \t]/.test(command[i])){i++;continue;}
    if(command[i]==='"'){
      i++;
      while(i<command.length&&command[i]!=='"'){
        if(command[i]==='\\'&&command[i+1]==='"')return command;
        i++;
      }
      if(i===command.length)return command;
      i++;
      if(i<command.length&&!/[ \t]/.test(command[i]))return command;
    }else{
      const word=/^[A-Za-z0-9_./:+,=-]+/.exec(command.slice(i));
      if(!word)return command;
      i+=word[0].length;
      if(i<command.length&&!/[ \t]/.test(command[i]))return command;
    }
  }
  return 'echo MODEL_DESK_LITERAL_OUTPUT';
}

/** Only a leading literal location change is understood. This deliberately
 * does not interpret scripts, substitutions, providers or general shell code.
 * Semicolon/OR chains must also be checked at the old cwd: cd may fail.
 */
function leadingLocation(command, base) {
  const text = String(command);
  if (!/^\s*(?:cd|chdir|set-location|sl)(?:\s|$)/i.test(text)) return null;
  const parts = commandGroups(text);
  if (!parts || parts.length < 2) return null;
  const first = parts[0].trim();
  const invalid = () => ({ error: true });
  if (/[`$%^<>|&(){}*?\[\]~\r\n]/.test(first)) return invalid();
  const words = first.match(/"[^"\r\n]*"|'[^'\r\n]*'|[^\s"']+/g) || [];
  // Reject concatenated/escaped/unbalanced quoting instead of guessing.
  if (words.join(' ').replace(/\s+/g,' ') !== first.replace(/\s+/g,' ')) return invalid();
  const verb = words.shift()?.toLowerCase();
  if (verb === 'cd' || verb === 'chdir') {
    if (/^(?:--|\/d)$/i.test(words[0] || '')) words.shift();
  } else if (/^-(?:literalpath|path)$/i.test(words[0] || '')) words.shift();
  if (words.length !== 1) return invalid();
  const target = stripQuotes(words[0]);
  if (!target || target.startsWith('-') || (isWindows() && (/^[A-Za-z]:$/.test(target) || /^\//.test(target)))
      || (target.includes(':') && !/^[A-Za-z]:[\\/]/.test(target))) return invalid();
  const tail = text.slice(parts[0].length);
  const separator = /^(\&\&|\|\||;)/.exec(tail)?.[0];
  if (!separator) return invalid();
  const remainder = tail.slice(separator.length).trim();
  if (!remainder) return invalid();
  const resolved = resolveAgainst(base, target);
  // Shells can retain a logical PWD while filesystem operations use physical
  // cwd. Check both when a junction/symlink changes the directory identity.
  const targets = [...new Set([resolved, normalizePath(realpathDeep(resolved))])];
  return { target: resolved, targets, remainder, onSuccessOnly: separator === '&&' };
}

function listingMutationPipeline(text) {
  const broad = value => /\|/.test(value) && /\b(get-childitem|gci|dir|ls|find)\b/i.test(value);
  const parts=commandGroups(text);
  if(!parts)return broad(text);
  return parts.some(part=>broad(part)&&!isProvablyReadOnly(part));
}

function destructiveAncestor(command, base, tokens, config) {
  const text = String(command);
  // Words such as "copy" in an echo/formatting payload are not operations.
  if (isProvablyReadOnly(text)) return undefined;
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
      || listingMutationPipeline(text)
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

function decideTerminal(args, config, budget = { remaining: 32 }) {
  if(directTerminalPolicy(config))return directTerminalReason(config,args||{});
  const command = terminalAnalysis(args && typeof args.command === 'string' ? args.command : '');
  if (!command.trim()) return undefined;
  if (--budget.remaining < 0) return '复合命令过于复杂，无法核实工作目录；请报告主脑。';
  if (args.workdir && args.cwd && resolveAgainst(config.root, args.workdir) !== resolveAgainst(config.root, args.cwd)) {
    return '工作目录参数 cwd 与 workdir 冲突；请报告主脑。';
  }
  const base = cwdFor(args, config);
  const location = leadingLocation(command, base);
  if (location?.error) return '未能核实字面工作目录切换，已拒绝复合命令；请报告主脑。';
  if (location) {
    const bases = location.onSuccessOnly ? location.targets : [...new Set([base, ...location.targets])];
    for (const workdir of bases) {
      const reason = decideTerminal({ command: location.remainder, workdir }, config, budget);
      if (reason) return reason;
    }
    return undefined;
  }
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
  commandGroups,
  taskPolicy,
  taskWriteReason,
  protectedPathChecker,
  protectedArgumentsReason,
  terminalFacts,
  directTerminalPolicy,
  directTerminalTargets,
  directTerminalReason,
  terminalAnalysis,
  leadingLocation,
  createGuard,
  decide,
};

// Fixed classification of actual guard refusals, shared by the four adapters.
// Never persist the reason text: it can contain a private path or command.
function denialCode(reason){
  if(!reason)return null;
  if(reason==='目标属于任务声明的受保护路径')return 'protected_data';
  if(reason==='文件不属于本任务项目')return 'outside_project';
  if(reason==='文件未列入本任务可写范围')return 'undeclared_write';
  if(reason==='任务只读上下文不可修改')return 'readonly_file';
  if(reason.startsWith('本轮工具已暂停'))return 'prior_denial';
  if(reason==='本任务未授权此原生工具'||reason==='只读任务没有终端权限'||reason==='只读任务没有写入权限')return 'tool_not_allowed';
  if(/记录|认证|其他任务|原生配置|硬链接/.test(reason))return 'protected_data';
  return 'guard_denied';
}
function denialRule(reason){
  if(!reason)return null;
  if(reason==='目标属于任务声明的受保护路径')return 'protected_path';
  if(reason.includes('工作区隔离')||reason.includes('任务隔离'))return 'workspace_isolation';
  if(reason.startsWith('本轮工具已暂停'))return 'prior_denial';
  if(reason==='终端不能访问原生配置目录'||reason==='原生配置目录不作为执行目标')return 'native_profile';
  if(reason==='终端不能访问工作台记录或认证数据')return 'shell_protected_reference';
  if(reason.includes('是工作台记录目录 .model-desk 的上级目录'))return 'record_ancestor';
  if(reason.includes('位于工作台记录目录 .model-desk 内'))return 'record_target';
  if(reason==='工作台记录、Git 元数据或认证配置不作为执行目标')return 'protected_file';
  if(reason==='认证存储不作为执行目标')return 'credential_store';
  if(reason==='运行账户配置不作为执行目标')return 'runtime_config';
  if(reason==='不读取其他任务或会话材料')return 'other_task';
  if(reason==='文件存在硬链接别名')return 'hardlink';
  if(reason==='任务只读上下文不可修改')return 'readonly_file';
  if(reason==='文件不属于本任务项目')return 'outside_project';
  if(reason==='文件未列入本任务可写范围')return 'undeclared_write';
  if(reason==='执行任务不修改 Git 历史或向远端发布')return 'git_mutation';
  if(reason==='本任务终端须前台完成并返回退出结果')return 'background_job';
  if(reason==='不能批准原生沙箱绕过')return 'sandbox_override';
  if(reason==='递归清理目标包含未核实的变量或通配符，请保留临时文件并报告主脑')return 'unverified_recursive_cleanup';
  if(reason==='目录检索范围包含原生配置；请选择更具体的文件')return 'profile_search';
  if(reason==='搜索不包含记录或认证目录')return 'protected_search';
  if(/工作目录|复合命令过于复杂/.test(reason))return 'unverified_location';
  if(reason==='本任务未授权此原生工具'||reason==='只读任务没有终端权限'||reason==='只读任务没有写入权限')return 'tool_not_allowed';
  return 'unclassified';
}
function guardDenial(tool,reason){
  const known=['read','Read','grep','Grep','find','Glob','ls','NotebookRead','write','Write','edit','Edit',
    'MultiEdit','NotebookEdit','str_replace_editor','bash','Bash','powershell','PowerShell','pwsh'];
  return {schema:1,code:'TOOL_PERMISSION_DENIED',tool:known.includes(tool)?tool:'unknown',
    reason_code:denialCode(reason),rule_id:denialRule(reason)};
}
Object.assign(module.exports,{denialCode,denialRule,guardDenial});
