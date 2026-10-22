'use strict';
/**
 * Offline unit tests for the workbench-record write policy
 * (`.local-worker/agent-runtime/record-policy.cjs`).
 *
 * These tests never touch the real workbench records: every path lives in a
 * fresh OS temp directory.  Run with:
 *   node .local-worker/tests/test_record_protection.cjs
 */
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const policy = require(path.join(__dirname, '..', 'agent-runtime', 'record-policy.cjs'));
const plugin = require(path.join(__dirname, '..', 'agent-runtime', 'record-protection.cjs'));

const failures = [];
let checks = 0;

function test(name, body) {
  try {
    body();
    checks += 1;
    console.log('ok   - ' + name);
  } catch (error) {
    failures.push({ name, error });
    console.log('FAIL - ' + name + ' :: ' + (error && error.message));
  }
}

const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'md-record-policy-'));
const root = path.join(temp, 'proj');
const records = path.join(root, '.model-desk');
fs.mkdirSync(path.join(records, 'runs', 'task-1'), { recursive: true });
fs.writeFileSync(path.join(records, 'records.db'), 'real-bytes');
fs.mkdirSync(path.join(root, 'src'), { recursive: true });
fs.writeFileSync(path.join(root, 'src', 'ok.py'), 'print(1)\n');
fs.mkdirSync(path.join(root, '.model-desk-backup'), { recursive: true });
fs.mkdirSync(path.join(root, '.model-desktop'), { recursive: true });
const config = { root };

const deny = (tool, args, why) => {
  const reason = policy.decide(tool, args, config);
  assert.ok(typeof reason === 'string' && reason, (why || tool) + ' should be denied');
  assert.ok(reason.includes(policy.DENY_MARKER), 'denial must name the workbench: ' + reason);
  return reason;
};
const allow = (tool, args, why) => {
  const reason = policy.decide(tool, args, config);
  assert.strictEqual(reason, undefined, (why || tool) + ' should be allowed, got: ' + reason);
};

// ------------------------------------------------- write / edit path handling
test('literal cleanup and a separate read-only listing do not form a destructive pipeline', () => {
  const ordinary=path.join(temp,'ordinary');
  allow('bash',{command:`rm -rf "${ordinary}"; ls "${root}" | grep -c fixture || true`});
  allow('pwsh',{command:'Remove-Item -LiteralPath src/cache -Recurse -Force; Get-ChildItem . | Format-List'});
  deny('bash',{command:'ls . | xargs rm -rf'});
  deny('pwsh',{command:'Get-ChildItem . | Remove-Item -Recurse -Force'});
  deny('bash',{command:`rm -rf "${ordinary}"; ls . | xargs rm -rf`});
});

test('write into the record directory is denied (relative and absolute)', () => {
  deny('write', { file_path: path.join(records, 'records.db'), content: 'x' });
  deny('write', { file_path: '.model-desk/records.db', content: 'x' });
  deny('write', { file_path: '.model-desk\\runs\\task-1\\task.json', content: 'x' });
  deny('write', { file_path: '.MODEL-DESK/records.db', content: 'x' });
  deny('write', { file_path: 'src/../.model-desk/records.db', content: 'x' });
  deny('write', { file_path: './.model-desk', content: 'x' });
  deny('edit', { file_path: path.join(records, 'records.db'), old_string: 'a', new_string: 'b' });
  deny('edit', { file_path: '.model-desk/records.db', old_string: 'a', new_string: 'b' });
});

test('normal source files and similar directory names are not harmed', () => {
  allow('write', { file_path: path.join(root, 'src', 'ok.py'), content: 'x' });
  allow('write', { file_path: 'src/ok.py', content: 'x' });
  allow('edit', { file_path: 'src/ok.py', old_string: 'a', new_string: 'b' });
  allow('write', { file_path: '.model-desk-backup/records.db', content: 'x' });
  allow('write', { file_path: path.join(root, '.model-desktop', 'notes.txt'), content: 'x' });
  allow('write', { file_path: '.model-desk-notes.md', content: 'x' });
  allow('write', { file_path: 'src/records_model.py', content: 'x' });
});

test('str_replace_editor only refuses its mutating commands', () => {
  deny('str_replace_editor', { command: 'create', path: '.model-desk/x.txt', file_text: 'y' });
  deny('str_replace_editor', { command: 'str_replace', path: path.join(records, 'records.db'), old_str: 'a', new_str: 'b' });
  deny('str_replace_editor', { command: 'insert', path: '.model-desk/x.txt', insert_line: 1, new_str: 'y' });
  allow('str_replace_editor', { command: 'view', path: '.model-desk/records.db' });
});

test('read-only and unrelated tools are left untouched', () => {
  for (const tool of ['read', 'read_image', 'glob', 'grep', 'list', 'todo_write', 'present',
    'job_output', 'job_list', 'job_kill', 'ask_user_question', 'web_fetch', 'skill',
    'create_goal', 'get_goal', 'update_goal', 'subagent', 'ralph']) {
    allow(tool, { file_path: '.model-desk/records.db', path: '.model-desk/records.db' }, tool);
  }
  assert.strictEqual(policy.isGuardedTool('read'), false);
  assert.strictEqual(policy.isGuardedTool('write'), true);
  assert.strictEqual(policy.isGuardedTool('pwsh'), true);
});

test('the guard probe is answered by this policy alone', () => {
  assert.strictEqual(policy.decide(policy.PROBE_TOOL, {}, config), policy.PROBE_REASON);
  assert.strictEqual(policy.decide(policy.PROBE_TOOL, {}), policy.PROBE_REASON);
});

// ------------------------------------------------------------- alias handling
test('a junction/symlink alias into the record directory is denied', function () {
  const link = path.join(root, 'alias-records');
  try {
    fs.symlinkSync(records, link, 'junction');
  } catch (error) {
    console.log('     (junction creation unavailable, alias case skipped: ' + error.code + ')');
    return;
  }
  deny('write', { file_path: path.join(link, 'records.db'), content: 'x' });
  deny('write', { file_path: 'alias-records/records.db', content: 'x' });
  allow('write', { file_path: path.join(root, 'src', 'ok.py'), content: 'x' });
});

test('the record directory itself may be an alias to another location', function () {
  const aliasRoot = path.join(temp, 'aliased');
  const real = path.join(temp, 'elsewhere', '.model-desk');
  fs.mkdirSync(path.join(aliasRoot, 'src'), { recursive: true });
  fs.mkdirSync(real, { recursive: true });
  const aliasDir = path.join(aliasRoot, '.model-desk');
  try {
    fs.symlinkSync(real, aliasDir, 'junction');
  } catch (error) {
    console.log('     (junction creation unavailable, skipped: ' + error.code + ')');
    return;
  }
  const aliased = { root: aliasRoot };
  assert.ok(policy.decide('write', { file_path: path.join(aliasDir, 'records.db') }, aliased));
  // Writing straight to the alias target is denied too: both the lexical record
  // directory and the location it resolves to are protected.
  assert.ok(policy.decide('write', { file_path: path.join(real, 'records.db') }, aliased));
  assert.strictEqual(policy.decide('write', { file_path: path.join(aliasRoot, 'src', 'a.py') }, aliased), undefined);
});

// ----------------------------------------------------------- terminal command
test('typical terminal writes against the record directory are denied', () => {
  for (const command of [
    'Remove-Item -Recurse -Force .model-desk',
    'Remove-Item .model-desk\\records.db',
    'rm -rf .model-desk',
    'del .model-desk\\records.db',
    'Set-Content .model-desk\\records.db hacked',
    'Add-Content -Path ".model-desk/records.db" -Value x',
    'New-Item -ItemType Directory .model-desk\\evil',
    'Move-Item .model-desk\\records.db .\\stolen.db',
    'Rename-Item .model-desk .model-desk-old',
    'Copy-Item .\\src\\ok.py .model-desk\\',
    'robocopy .\\src .model-desk /E',
    'cmd /c del .model-desk\\records.db',
    'cd .model-desk; Remove-Item records.db',
    'python -c "open(\'.model-desk/records.db\', \'w\').write(\'x\')"',
    'node -e "require(\'fs\').writeFileSync(\'.model-desk/records.db\',\'y\')"',
    'git -C . clean -fdx .model-desk',
    'powershell -Command "Set-Content \'\'\'.model-desk\\x\'\'\' y"',
  ]) {
    deny('pwsh', { command }, command);
    deny('bash', { command }, command);
  }
});

test('terminal ancestors of the record directory are protected from deletion', () => {
  deny('pwsh', { command: 'Remove-Item -Recurse -Force .' }, 'delete cwd');
  deny('pwsh', { command: 'rm -rf *' }, 'delete cwd wildcard');
  deny('pwsh', { command: 'Remove-Item -Recurse ' + root }, 'delete project root');
  deny('pwsh', { command: 'Rename-Item ' + root + ' ' + root + '-old' }, 'rename project root');
  deny('pwsh', { command: 'Remove-Item -Recurse ..' }, 'delete parent');
  allow('pwsh', { command: 'Remove-Item -Recurse -Force .\\build' }, 'unrelated subdir');
  allow('pwsh', { command: 'Remove-Item -Recurse -Force .model-desk-backup' }, 'similar name');
  for(const command of [
    'Remove-Item -Recurse -Force .model*',
    'Remove-Item -Recurse .?odel-desk',
    'Remove-Item -Recurse [.]model-desk',
    'Get-ChildItem -Force | Remove-Item -Recurse -Force',
    'git clean -fdx',
    'git -C . clean -fdx',
    'find . -delete',
    'python -c "import shutil; shutil.rmtree(\'.\')"',
  ])deny('pwsh',{command},command);
  allow('pwsh',{command:'Remove-Item -Recurse build/*'},'unrelated subtree wildcard');
  allow('pwsh',{command:'git clean -fd -- build'},'explicit unrelated git path');
});

test('provably read-only terminal commands still work', () => {
  allow('pwsh', { command: 'Get-Content .model-desk\\records.db' });
  allow('pwsh', { command: 'Get-ChildItem -Recurse .model-desk' });
  allow('pwsh', { command: 'type .model-desk/records.db' });
  allow('pwsh', { command: 'Select-String -Pattern task .model-desk\\records.db' });
  allow('pwsh', { command: 'Get-ChildItem .model-desk | Select-Object Name' });
  allow('pwsh', { command: 'rg --files .model-desk' });
  allow('pwsh', { command: 'echo .model-desk is managed by the workbench' });
});

test('read-oriented programs with write or execute options are not exempt', () => {
  for (const command of [
    'find .model-desk -delete',
    'sort -o .model-desk/out.txt .model-desk/in.txt',
    'rg --pre=evil .model-desk',
    'rg --pre evil .model-desk',
    'tree -o .model-desk/out.txt .',
    'diff --output=.model-desk/out.txt a b',
    'less -o .model-desk/out.txt src/ok.py',
  ]) deny('bash', {command}, command);
});

test('unrelated terminal commands are untouched', () => {
  allow('pwsh', { command: 'npm test' });
  allow('pwsh', { command: 'python -m unittest discover -s tests' });
  allow('pwsh', { command: 'git status --short' });
  allow('bash', { command: 'python -m pytest -q' });
  allow('pwsh', { command: 'Remove-Item -Recurse -Force .\\tmp\\cache' });
});

test('formatting and separate cache cleanup are not root deletion', () => {
  for (const tool of ['pwsh', 'bash']) {
    for (const command of [
      'Get-ChildItem src | Format-List',
      'Get-ChildItem .model-desk | Format-Table',
      'Remove-Item -LiteralPath "src/__pycache__" -Recurse -Force; Get-ChildItem src | Select-Object Name,Length',
      'rm -rf src/__pycache__ && ls src | head -n 5',
      'Get-ChildItem src | Format-List; Remove-Item -Recurse src/__pycache__',
      'Remove-Item -LiteralPath "src/cache;old" -Recurse; Get-ChildItem src | Format-List',
    ]) allow(tool, { command }, command);
  }
});

test('compound commands do not exempt actual destructive targets or unknown shells', () => {
  for (const command of [
    'Remove-Item -Recurse .; Get-ChildItem src | Format-List',
    'Get-ChildItem src | Format-List; Remove-Item -Recurse .model*',
    'Get-ChildItem | Remove-Item -Recurse; Get-ChildItem src | Format-List',
    'git clean -fdx; Get-ChildItem src | Format-List',
    'cd ..; rm -rf .; Get-ChildItem src | Format-List',
    'Set-Location ..; Remove-Item -Recurse .; Get-ChildItem src | Format-List',
    'Remove-Item -Recurse "src; Get-ChildItem | Remove-Item .model-desk"',
    'format.exe .; Get-ChildItem src | Format-List',
  ]) deny('pwsh', { command }, command);
});

test('a record refusal pauses cross-tool mutations only for the current guard', () => {
  const guard = policy.createGuard(config);
  assert.strictEqual(guard('write', { file_path: 'src/ok.py' }), undefined);
  assert.ok(guard('pwsh', { command: 'Remove-Item -Recurse .model-desk' }));
  for (const [tool, args] of [
    ['bash', { command: 'rm -rf src/cache' }],
    ['pwsh', { command: 'Remove-Item src/cache' }],
    ['write', { file_path: 'src/ok.py' }],
    ['edit', { file_path: 'src/ok.py' }],
    ['str_replace_editor', { command: 'create', path: 'src/new.py' }],
  ]) assert.match(guard(tool, args), /已暂停/);
  assert.strictEqual(guard('read', { file_path: 'src/ok.py' }), undefined);
  assert.strictEqual(guard('str_replace_editor', { command: 'view', path: 'src/ok.py' }), undefined);
  assert.strictEqual(guard(policy.PROBE_TOOL, {}), policy.PROBE_REASON);
  assert.strictEqual(policy.createGuard(config)('write', { file_path: 'src/ok.py' }), undefined);
});

test('unexpected policy errors fail closed and pause without exposing error data', () => {
  const configError = { get root() { throw new Error('PRIVATE_TEST_ERROR'); } };
  const guard = policy.createGuard(configError);
  assert.match(guard('write', { file_path: 'src/ok.py' }), /判断失败/);
  assert.doesNotMatch(guard('bash', { command: 'echo ok' }), /PRIVATE_TEST_ERROR/);
  assert.strictEqual(guard('read', { file_path: 'src/ok.py' }), undefined);
});

test('the declared workdir shifts relative-path resolution', () => {
  const sub = path.join(root, 'src');
  deny('write', { file_path: '../.model-desk/records.db', workdir: sub, content: 'x' });
  allow('write', { file_path: '../.model-desk-backup/records.db', workdir: sub, content: 'x' });
  deny('pwsh', { command: 'Set-Content ..\\.model-desk\\records.db x', workdir: sub });
  allow('pwsh', { command: 'Get-Content ..\\.model-desk\\records.db', workdir: sub });
});

test('relative read-only classification never claims OS isolation', () => {
  assert.strictEqual(policy.isProvablyReadOnly('Get-Content .model-desk\\records.db'), true);
  assert.strictEqual(policy.isProvablyReadOnly('Get-Content .model-desk\\records.db > out.txt'), false);
  assert.strictEqual(policy.isProvablyReadOnly('Get-Content $env:USERPROFILE'), false);
  assert.strictEqual(policy.isProvablyReadOnly('python -c "print(1)"'), false);
});

// ------------------------------------------------------ Cordis plugin mount
function fakeTools(answerProbe) {
  const guards = [];
  return {
    guard(fn) { guards.push(fn); return () => { const at = guards.indexOf(fn); if (at >= 0) guards.splice(at, 1); }; },
    guardReason(exec) {
      if (answerProbe === false) return undefined; // a service without the guard hook
      for (const guard of guards) { const reason = guard(exec); if (reason !== undefined) return reason; }
      return undefined;
    },
    guards,
  };
}
function fakeCtx(tools) {
  const listeners = {};
  return {
    tools,
    on(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
    emit(name, payload) { for (const fn of listeners[name] || []) fn(payload); },
  };
}

test('the plugin mounts a global guard, self-tests it and writes its proof', () => {
  delete process.env.MODEL_DESK_RECORD_PROOF;
  const proof = path.join(temp, 'plugin-proof.json');
  const tools = fakeTools(true);
  const ctx = fakeCtx(tools);
  plugin(ctx, { root, revision: plugin.selfRevision(), proof });
  assert.strictEqual(tools.guards.length, 1);
  const data = JSON.parse(fs.readFileSync(proof, 'utf8'));
  assert.strictEqual(data.source, 'model-desk-record-protection');
  assert.strictEqual(data.scope, 'global');
  assert.strictEqual(data.probe, 'ok');
  assert.strictEqual(data.revision, plugin.selfRevision());
  assert.strictEqual(data.session_id, null);

  assert.strictEqual(tools.guardReason({ name: 'write', arguments: { file_path: 'src/ok.py' } }), undefined);
  const denied = tools.guardReason({ name: 'write', arguments: { file_path: '.model-desk/records.db' } });
  assert.ok(denied && denied.includes(policy.DENY_MARKER), 'the mounted guard must deny record writes');
  assert.match(tools.guardReason({ name: 'write', arguments: { file_path: 'src/ok.py' } }), /已暂停/);
  assert.ok(tools.guardReason({ name: 'pwsh', arguments: { command: 'Set-Content .model-desk/x y' } }));
  assert.strictEqual(tools.guardReason({ name: 'read', arguments: { file_path: '.model-desk/records.db' } }), undefined);

  ctx.emit('agent/created', { agent: { id: 'parent-session' } });
  assert.strictEqual(JSON.parse(fs.readFileSync(proof, 'utf8')).session_id, 'parent-session');
  ctx.emit('agent/created', { agent: { id: 'child-session' } });
  assert.strictEqual(JSON.parse(fs.readFileSync(proof, 'utf8')).session_id, 'parent-session');
});

test('the plugin refuses to mount with a stale revision or without the guard hook', () => {
  delete process.env.MODEL_DESK_RECORD_PROOF;
  const proof = path.join(temp, 'plugin-proof-bad.json');
  assert.throws(() => plugin(fakeCtx(fakeTools(true)), { root, revision: 'not-the-real-revision', proof }), /指纹/);
  assert.throws(() => plugin(fakeCtx(fakeTools(true)), { root, revision: '', proof }), /指纹/);
  // A tools service that cannot answer a registered guard is not protection.
  assert.throws(() => plugin(fakeCtx(fakeTools(false)), { root, revision: plugin.selfRevision(), proof }), /全局工具层/);
  assert.throws(() => plugin(fakeCtx({}), { root, revision: plugin.selfRevision(), proof }), /guard/);
  // No proof destination: fail closed instead of reporting unverifiable protection.
  assert.throws(() => plugin(fakeCtx(fakeTools(true)), { root, revision: plugin.selfRevision(), proof: '' }), /证据路径/);
  assert.ok(!fs.existsSync(proof));
});

console.log('\n' + checks + '/' + (checks + failures.length) + ' checks passed');
if (failures.length) {
  for (const failure of failures) console.error('\n' + failure.name + '\n' + (failure.error && failure.error.stack));
  process.exitCode = 1;
}
fs.rmSync(temp, { recursive: true, force: true });
