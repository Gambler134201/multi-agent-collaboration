'use strict';
/**
 * Offline regression tests for the NOT-WIRED candidate module
 * agent-runtime/candidates/shell-dialect.cjs.
 *
 * The candidate is a pure evidence table: every fixture exercises its
 * exported dialectFor() function only. Nothing here starts a service, reads
 * the user's environment or calls a model. The version strings stand for the
 * versions the caller has already verified through its own existing checks
 * (dsh_worker/executor_adapter, pi_worker, workbuddy_executor,
 * zcode_worker); the candidate itself performs no I/O.
 *
 * Run with: node --test .local-worker/tests/test_shell_dialect_candidate.cjs
 */
const test=require('node:test'),assert=require('node:assert/strict');
const candidate=require('../agent-runtime/candidates/shell-dialect.cjs');

test('DSH tool names carry construction-level dialects under the researched version',t=>{
  const bash=candidate.dialectFor('dsh','bash','0.2.1.0');
  assert.equal(bash.dialect,'bash');
  assert.equal(bash.level,candidate.LEVELS.CONSTRUCTION);
  assert.ok(bash.basis.includes('"bash","-c"'),bash.basis);
  const pwsh=candidate.dialectFor('dsh','pwsh','0.2.1.0');
  assert.equal(pwsh.dialect,'powershell');
  assert.equal(pwsh.level,candidate.LEVELS.CONSTRUCTION);
  assert.ok(pwsh.basis.includes('-Command'),pwsh.basis);
});

test('Pi tool names carry construction-level dialects under the pinned version',t=>{
  const bash=candidate.dialectFor('pi','bash','1.3.0');
  assert.equal(bash.dialect,'bash');
  assert.equal(bash.level,candidate.LEVELS.CONSTRUCTION);
  const powershell=candidate.dialectFor('pi','powershell','1.3.0');
  assert.equal(powershell.dialect,'powershell');
  assert.equal(powershell.level,candidate.LEVELS.CONSTRUCTION);
});

test('the WorkBuddy PowerShell tool is provable while the Bash tool stays ambiguous',t=>{
  const powershell=candidate.dialectFor('workbuddy','PowerShell','5.8.3');
  assert.equal(powershell.dialect,'powershell');
  assert.equal(powershell.level,candidate.LEVELS.CONSTRUCTION);
  // The 5.8.3 bundle falls back from Git Bash to the PowerShell
  // configuration when findBashPath fails, and the hook payload cannot show
  // which branch served a call — so the Bash tool name alone proves nothing.
  const bash=candidate.dialectFor('workbuddy','Bash','5.8.3');
  assert.equal(bash.dialect,null);
  assert.equal(bash.level,candidate.LEVELS.AMBIGUOUS);
  assert.ok(bash.basis.includes('falls back'),bash.basis);
});

test('a tool name is never guessed into a dialect without encoded evidence',t=>{
  // ZCode 3.16.3 has dialect-selection branches that were not bound to a
  // tool name in this batch: every name stays unknown, including 'Bash'.
  for(const tool of ['Bash','PowerShell','bash','pwsh']){
    const result=candidate.dialectFor('zcode',tool,'3.16.3');
    assert.equal(result.dialect,null,tool);
    assert.equal(result.level,candidate.LEVELS.UNKNOWN,tool);
    assert.equal(result.basis,null,tool);
  }
  // Cross-executor name confusion is not papered over: DSH names are
  // lowercase, WorkBuddy names capitalized, and each table answers only its
  // own exact names.
  assert.equal(candidate.dialectFor('dsh','Bash','0.2.1.0').dialect,null);
  assert.equal(candidate.dialectFor('dsh','PowerShell','0.2.1.0').dialect,null);
  assert.equal(candidate.dialectFor('workbuddy','bash','5.8.3').dialect,null);
  assert.equal(candidate.dialectFor('workbuddy','pwsh','5.8.3').dialect,null);
  assert.equal(candidate.dialectFor('pi','Bash','1.3.0').dialect,null);
  // Non-terminal tools never resolve to a shell dialect.
  assert.equal(candidate.dialectFor('dsh','read','0.2.1.0').dialect,null);
  assert.equal(candidate.dialectFor('pi','write','1.3.0').dialect,null);
  assert.equal(candidate.dialectFor('workbuddy','Edit','5.8.3').dialect,null);
});

test('an unverified version degrades to unknown even for a known executor',t=>{
  // The evidence is only as good as the install it was inspected in; a
  // newer DSH or WorkBuddy must be re-inspected, not assumed.
  for(const [executor,tool] of [['dsh','bash'],['pi','bash'],['workbuddy','PowerShell']]){
    for(const version of [undefined,null,'','0.2.2.0','1.4.0','5.8.2','5.9.0']){
      const result=candidate.dialectFor(executor,tool,version);
      assert.equal(result.dialect,null,`${executor}/${tool}@${version}`);
      assert.equal(result.level,candidate.LEVELS.UNKNOWN,`${executor}/${tool}@${version}`);
    }
  }
});

test('unknown executors and malformed inputs never throw or fabricate evidence',t=>{
  for(const result of [candidate.dialectFor('aider','bash','1.0.0'),
    candidate.dialectFor(undefined,'bash','0.2.1.0'),
    candidate.dialectFor('dsh',undefined,'0.2.1.0'),
    candidate.dialectFor('dsh',42,'0.2.1.0'),
    candidate.dialectFor('dsh','bash',42)]){
    assert.deepEqual({...result},{dialect:null,level:candidate.LEVELS.UNKNOWN,basis:null});
  }
});

test('proven dialects carry their residual machine-state limits',t=>{
  // Construction evidence fixes the grammar class, not the concrete
  // executable: the limits string travels with the result so a wiring keeps
  // the boundary honest instead of silently overclaiming.
  const bash=candidate.dialectFor('dsh','bash','0.2.1.0');
  assert.ok(bash.limits.includes('machine state'),bash.limits);
  const powershell=candidate.dialectFor('workbuddy','PowerShell','5.8.3');
  assert.ok(powershell.limits.includes('per-call'),powershell.limits);
});

test('the candidate module changes no shipped behaviour anywhere',t=>{
  // Nothing inside the pinned decision files may require this module: the
  // guards and the policy run without it, and their source bytes (and hence
  // the revision digests old sessions are bound to) are untouched by the
  // candidate's existence.
  const fs=require('node:fs'),path=require('node:path');
  const runtime=path.join(__dirname,'..','agent-runtime');
  const pinned=['workbuddy-native-guard.cjs','zcode-native-guard.cjs',
    'record-policy.cjs','endpoint-access.cjs'];
  for(const file of pinned){
    const source=fs.readFileSync(path.join(runtime,file),'utf8');
    assert.ok(!source.includes('shell-dialect'),file+' must not reference the candidate');
  }
});
