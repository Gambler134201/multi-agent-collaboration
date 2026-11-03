'use strict';
/**
 * Offline regression tests for the NOT-WIRED candidate module
 * agent-runtime/candidates/protected-root-resolution.cjs.
 *
 * Every fixture lives in an isolated temporary directory with real Windows
 * junctions (fs.symlinkSync(target, link, 'junction') needs no elevation).
 * No service is started and no live model is called. The workbuddy guard
 * module is required here only to call its exported pure decide() function
 * for the compatibility matrix; the candidate itself never imports the guard.
 *
 * Run with: node --test .local-worker/tests/test_protected_root_resolution_candidate.cjs
 */
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const candidate=require('../agent-runtime/candidates/protected-root-resolution.cjs');
const guard=require('../agent-runtime/workbuddy-native-guard.cjs');
const policy=require('../agent-runtime/record-policy.cjs');

const LEXICAL_DENIAL='终端不能访问原生配置目录';

function fixture(t){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'md-protected-root-candidate-'));
  const original=path.join(root,'project');
  const copy=path.join(root,'project-task-workspaces','task');
  const backup=path.join(root,'project-backup');
  fs.mkdirSync(original);
  fs.mkdirSync(path.join(copy,'src'),{recursive:true});
  fs.mkdirSync(backup);
  fs.writeFileSync(path.join(original,'secret.txt'),'fixture');
  fs.writeFileSync(path.join(copy,'src','main.js'),'fixture');
  // Real junction aliases, siblings of the protected root like the recorded
  // boundary fixture builds them.
  const alias=path.join(root,'original-alias');
  const copyAlias=path.join(root,'copy-alias');
  fs.symlinkSync(original,alias,'junction');
  fs.symlinkSync(copy,copyAlias,'junction');
  // Removing a junction unlinks the reparse point; the target tree survives.
  t.after(()=>{fs.rmSync(alias,{recursive:true,force:true});fs.rmSync(copyAlias,{recursive:true,force:true});
    fs.rmSync(root,{recursive:true,force:true});});
  // Native job shape (record_project omitted) so the endpoint-access isolation
  // layer is out of the way and guard.decide exercises the lexical matcher.
  const native={project:copy,kind:'edit',access_mode:'full',files:['src/main.js'],
    readonly_files:[],tools:['Bash'],protected_roots:[original]};
  return {root,original,copy,backup,alias,copyAlias,native,
    lexical(command){return guard.decide(native,{tool_name:'Bash',tool_input:{command}})===LEXICAL_DENIAL;}};
}

test('junction alias into the protected root is caught on resolved command tokens',t=>{
  const {original,alias}=fixture(t);
  // Each command only spells the alias, never the protected root; the
  // resolution layer follows the junction into the original tree.
  for(const command of [
    `type "${path.join(alias,'secret.txt')}"`,
    `Remove-Item "${alias}" -Recurse`,
    `rm -rf ${alias}`,
    `cd "${alias}" && rm -rf sub`,
    `Remove-Item "${path.join(alias,'*')}" -Recurse`,
    `type "${path.join(alias,'new.txt')}"`]){
    assert.equal(candidate.referencesProtectedRootResolved(command,original),true,command);
  }
});

test('literal paths of the protected root itself stay matches',t=>{
  const {original}=fixture(t);
  for(const command of [`type "${path.join(original,'secret.txt')}"`,`ls "${original}"`,
    `rm -rf ${original}/sub`]){
    assert.equal(candidate.referencesProtectedRootResolved(command,original),true,command);
  }
});

test('sibling copies and same-prefix directories are not the protected root',t=>{
  const {original,copy,backup}=fixture(t);
  // Boundary matching uses normalizePath + separator prefix, so
  // <root>-task-workspaces and <root>-backup never count as the root.
  for(const command of [`ls "${path.join(copy,'src')}"`,`ls "${backup}"`,
    `ls ../project-task-workspaces`,`node build.js`,`echo done`]){
    assert.equal(candidate.referencesProtectedRootResolved(command,original),false,command);
  }
});

test('an alias to a non-protected tree does not become a protected reference',t=>{
  const {original,copyAlias}=fixture(t);
  // The junction resolves into the task copy, which is not the protected root.
  assert.equal(candidate.referencesProtectedRootResolved(`type "${path.join(copyAlias,'src','main.js')}"`,original),false);
});

test('variable-bearing commands without protected references stay allowed',t=>{
  const {original}=fixture(t);
  assert.equal(candidate.referencesProtectedRootResolved('Write-Output "$env:USERNAME"',original),false);
});

test('unresolvable tokens are skipped rather than conservatively refused (documented trade-off)',t=>{
  const {original}=fixture(t);
  // Chosen candidate semantics: a token containing $/%/backtick cannot be
  // proven from text and is skipped, not judged. A per-token conservative
  // true here would deny every command containing any variable — including
  // ordinary output commands — and contradict the recorded expectation that
  // `Write-Output "$env:USERNAME"` stays allowed. Recursive cleanup with
  // variables remains owned by the guard's unverified_recursive_cleanup rule;
  // the wiring decision may revisit this split deliberately.
  const command='Remove-Item "$env:MODEL_DESK_UNDEFINED\\sub" -Recurse';
  assert.equal(candidate.referencesProtectedRootResolved(command,original),false,command);
});

test('candidate agrees with the lexical matcher on literal paths',t=>{
  // Compatibility matrix over commands that contain no alias or link: the
  // candidate verdict must equal the guard's referencesProtectedRoot verdict
  // (observed through the exported decide() of a native job) case by case.
  const {original,copy,backup,alias,copyAlias,lexical}=fixture(t);
  assert.notEqual(alias,path.join(original,'original-alias'));
  const literalTrue=[
    `ls "${original}"`,
    `ls "${path.join(original,'secret.txt')}"`,
    `ls "${original.toUpperCase()}"`,
    `ls "${original.replace(/\\/g,'/')}/file"`];
  const literalFalse=[
    `ls "${path.join(copy,'src')}"`,
    `ls "${backup}"`,
    `ls ../project-task-workspaces`,
    `node build.js`,
    `ls src`,
    'Write-Output "$env:USERNAME"'];
  for(const command of literalTrue){
    assert.equal(lexical(command),true,`lexical must match: ${command}`);
    assert.equal(candidate.referencesProtectedRootResolved(command,original),lexical(command),command);
  }
  for(const command of literalFalse){
    assert.equal(lexical(command),false,`lexical must not match: ${command}`);
    assert.equal(candidate.referencesProtectedRootResolved(command,original),lexical(command),command);
  }
  // Aliased shapes are outside the literal matrix; they only show the
  // candidate's added coverage over the lexical matcher.
  assert.equal(lexical(`type "${path.join(alias,'secret.txt')}"`),false);
  assert.equal(candidate.referencesProtectedRootResolved(`type "${path.join(alias,'secret.txt')}"`,original),true);
  assert.equal(lexical(`type "${path.join(copyAlias,'src','main.js')}"`),false);
  assert.equal(candidate.referencesProtectedRootResolved(`type "${path.join(copyAlias,'src','main.js')}"`,original),false);
});

test('unwired path-only candidate must not undo literal-output analysis if connected later',t=>{
  const {original,lexical}=fixture(t);
  for(const command of [`echo "${original}"`,`echo "see ${original} for details"`]){
    assert.equal(lexical(command),false);
    assert.equal(candidate.referencesProtectedRootResolved(command,original),true);
    // Future callers must provide analysis text, not feed literal payloads
    // back into a second path scan and reintroduce the same false refusal.
    assert.equal(candidate.referencesProtectedRootResolved(policy.terminalAnalysis(command),original),false);
  }
});

test('documented divergences from the lexical matcher require additive wiring, not replacement',t=>{
  // Recorded divergences, not defects to fix silently: the lexical matcher
  // sees raw text, the candidate only judges tokens that resolve. Win32
  // realpath rejects these literal forms, so the candidate alone is weaker.
  // If wired, the resolved check must be added BESIDE referencesProtectedRoot.
  const {original,lexical}=fixture(t);
  const divergences=[
    // Alternate data stream suffix on the root.
    `ls "${original}:stream"`,
    // Trailing dot: the lexical dot rule matches, realpath returns ENOENT.
    `ls "${original}."`,
    // Path inside a switch token: looksLikeTarget skips leading-dash tokens.
    `rg --files --path=${original} x`];
  for(const command of divergences){
    assert.equal(lexical(command),true,`lexical must match: ${command}`);
    assert.equal(candidate.referencesProtectedRootResolved(command,original),false,command);
  }
});

test('relative alias tokens resolve against the caller-provided working directory',t=>{
  const {root,original,copy,alias}=fixture(t);
  // Documented caller contract: pass the same base the guard's leading-location
  // analysis uses (args.workdir || args.cwd || job.project). With the shell cwd
  // beside the root, a bare alias name resolves into the protected tree.
  assert.equal(candidate.referencesProtectedRootResolved('rm -rf original-alias',original,root),true);
  // With a different cwd the same text names a different location and is not a
  // protected reference; this is why omitting base must stay a deliberate default.
  assert.equal(candidate.referencesProtectedRootResolved('rm -rf original-alias',original,copy),false);
  // Default base (omitted) is the protected root's parent directory.
  assert.equal(candidate.referencesProtectedRootResolved('rm -rf original-alias',original),true);
});

test('cannot-prove inputs fail closed with true and never throw',t=>{
  const {original}=fixture(t);
  // Failure stance recorded for the wiring decision: an empty root, a missing
  // root or a non-string command cannot be proven safe and returns true —
  // a resolver defect can only over-deny terminal commands, never open access.
  for(const result of [candidate.referencesProtectedRootResolved('ls x',''),
    candidate.referencesProtectedRootResolved('ls x',undefined),
    candidate.referencesProtectedRootResolved(undefined,original),
    candidate.referencesProtectedRootResolved(42,original)]){
    assert.equal(typeof result,'boolean');
    assert.equal(result,true);
  }
  // An empty-string base is only a fallback trigger, not a failure: the token
  // resolves against the documented default (the root's parent) and is judged
  // normally instead of throwing.
  assert.equal(candidate.referencesProtectedRootResolved('ls x',original,''),false);
});
