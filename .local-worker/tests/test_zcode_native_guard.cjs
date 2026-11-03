'use strict';
/**
 * Offline regression tests for the ZCode native hook guard.
 *
 * Every fixture is generated in an isolated temporary directory: a fake
 * project with source files, a controller index, a task job description and a
 * run record tree. No real session, account, record or model work is touched
 * and no service is started; the guard module and the CLI entrypoint are
 * driven directly.
 *
 * Run with: node .local-worker/tests/test_zcode_native_guard.cjs
 */
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
const {spawnSync}=require('node:child_process');
const AGENT=path.join(__dirname,'..','agent-runtime');
const GUARD_FILE=path.join(AGENT,'zcode-native-guard.cjs');
const guard=require(GUARD_FILE);
const SESSION='modeldesk-zcode-test-session';
const NATIVE_TOOLS=['Read','Glob','Grep','Write','Edit','Bash'];
const sha256=value=>crypto.createHash('sha256').update(value).digest('hex');
const stable=value=>Array.isArray(value)?value.map(stable):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(k=>[k,stable(value[k])])):value;

test('v3 native hook keeps exact input hash, permits opaque filters and pauses only on real direct denial',t=>{
  const home=path.join(os.tmpdir(),'md-synthetic-home');
  const f=fixture(t,{job:{task_policy:{schema:3,write_scope:'workspace',home,deny_paths:[]}}});
  const command="$d='.'; Get-ChildItem -Exclude '.git' $d | Where-Object { $_.Name -ne '.model-desk' }";
  const input={tool_name:'Bash',tool_input:{command}},before=JSON.stringify(input);
  assert.deepEqual(f.call(input),{});
  assert.equal(JSON.stringify(input),before);
  assert.equal(f.events().at(-1).command_hash,sha256(command));
  assert.equal(fs.existsSync(path.join(f.run,'zcode-native-denied.flag')),false);
  assert.deepEqual(f.call({tool_name:'Write',tool_input:{file_path:'helper.js',content:'fixture'}}),{});
  assert.equal(f.call({tool_name:'Bash',tool_input:{command:'cat .git/config'}}).hookSpecificOutput.permissionDecision,'deny');
  assert.equal(f.events().at(-1).denial_rule,'protected_file');
  assert.equal(f.call({tool_name:'Write',tool_input:{file_path:'helper.js',content:'no retry'}}).hookSpecificOutput.permissionDecision,'deny');
});

test('v2 path policy passes business credential names unchanged and refuses actual home roots',t=>{
  const home=path.join(os.tmpdir(),'md-synthetic-home'),contract={schema:2,write_scope:'workspace',home,
    deny_paths:[path.join(home,'.codex')]};
  const f=fixture(t,{job:{task_policy:contract}});
  const command='node --test auth-token.test.js';
  assert.deepEqual(f.call({tool_name:'Bash',tool_input:{command}}),{});
  assert.equal(f.events().at(-1).command_hash,sha256(command));
  assert.deepEqual(f.call({tool_name:'Read',tool_input:{file_path:'tokens.json'}}),{});
  assert.equal(f.call({tool_name:'Read',tool_input:{file_path:'~/.codex/auth.json'}}).hookSpecificOutput.permissionDecision,'deny');
  assert.equal(f.events().at(-1).denial_rule,'protected_path');
  assert.equal(f.call({tool_name:'Read',tool_input:{file_path:'tokens.json'}}).hookSpecificOutput.permissionDecision,'deny');
});

test('literal output avoids false denial without rewriting native input or command evidence',t=>{
  const f=fixture(t),command='echo "git commit; rm -rf .model-desk"';
  const input={tool_name:'Bash',tool_input:{command}},before=JSON.stringify(input);
  const result=f.call(input);
  assert.notEqual(result.hookSpecificOutput?.permissionDecision,'deny');
  assert.equal(result.hookSpecificOutput?.updatedInput,undefined);
  assert.equal(JSON.stringify(input),before);
  assert.equal(f.events()[0].command_hash,sha256(command));
  assert.equal(f.proof().session_id,SESSION);
  assert.equal(fs.existsSync(path.join(f.run,'zcode-native-denied.flag')),false);
  assert.notEqual(f.call({}).hookSpecificOutput?.permissionDecision,'deny');
});

test('native hook permits literal negative rg exclusions with an exit-code suffix but denies a real record target',t=>{
  const f=fixture(t);
  const ok=f.call({tool_name:'Bash',tool_input:{command:"rg --files --glob '!.model-desk' src; echo DONE"}});
  assert.notEqual(ok.hookSpecificOutput?.permissionDecision,'deny');
  const blocked=f.call({tool_name:'Bash',tool_input:{command:"rg --files --glob '!.model-desk' .model-desk"}});
  assert.equal(blocked.hookSpecificOutput.permissionDecision,'deny');
});

function makeJob(project,taskId,session,overrides){
  return {schema:1,mode:'zcode-native-v1',task_id:taskId,project,kind:'edit',
    files:['src/main.js'],readonly_files:[],access_mode:'project',
    protected_roots:[path.join(project,'.zcode')],native_session_id:session,
    nonce:crypto.randomBytes(8).toString('hex'),revision:'fixture-plan-revision',guard_revision:guard.revision(),
    tools:[...NATIVE_TOOLS],...(overrides||{})};
}

function fixture(t,{session=SESSION,job:jobOverrides,index:rawIndex}={}){
  const project=fs.mkdtempSync(path.join(os.tmpdir(),'md-zcode-guard-'));
  const taskId='task-'+crypto.randomBytes(6).toString('hex');
  const run=path.join(project,'.model-desk','runs',taskId);
  fs.mkdirSync(path.join(project,'src'),{recursive:true});
  fs.mkdirSync(path.join(project,'.zcode'),{recursive:true});
  fs.mkdirSync(run,{recursive:true});
  fs.writeFileSync(path.join(project,'src','main.js'),'console.log("fixture")\n');
  fs.writeFileSync(path.join(project,'.zcode','settings.json'),'{}\n');
  fs.writeFileSync(path.join(project,'.model-desk','records.db'),'RECORD-BYTES');
  const job=makeJob(project,taskId,session,jobOverrides);
  const jobBytes=Buffer.from(JSON.stringify(job));
  fs.writeFileSync(path.join(run,'zcode-native-job.json'),jobBytes);
  const index=rawIndex||{schema:1,sessions:{[session]:{task_id:taskId,job_hash:sha256(jobBytes)}}};
  fs.writeFileSync(path.join(project,'.model-desk','zcode-native-sessions.json'),JSON.stringify(index));
  t.after(()=>fs.rmSync(project,{recursive:true,force:true}));
  return {project,taskId,run,job,jobBytes,
    call(input){return guard.main(project,{session_id:session,hook_event_name:'PreToolUse',
      tool_name:'Read',tool_input:{file_path:path.join(project,'src','main.js')},tool_use_id:'call-1',...input});},
    proof(){return JSON.parse(fs.readFileSync(path.join(run,'zcode-native-proof.json'),'utf8'));},
    events(){const file=path.join(run,'zcode-native-events.jsonl');
      return fs.existsSync(file)?fs.readFileSync(file,'utf8').trim().split('\n').map(JSON.parse):[];}};
}

const runCli=(box,input)=>spawnSync(process.execPath,[GUARD_FILE,box.project],
  {input:JSON.stringify(input),encoding:'utf8'});

test('revision() binds all shared guard modules',()=>{
  const expected=sha256(Buffer.concat([fs.readFileSync(GUARD_FILE),
    fs.readFileSync(path.join(AGENT,'workbuddy-native-guard.cjs')),
    fs.readFileSync(path.join(AGENT,'record-policy.cjs')),
    fs.readFileSync(path.join(AGENT,'endpoint-access.cjs'))]));
  assert.equal(guard.revision(),expected);
  assert.equal(guard.revision(),guard.revision());
});

test('native bounded source search passes unchanged and still protects real private targets',t=>{
  const box=fixture(t),command="rg -n '.zcode/config' src/main.js";
  assert.deepEqual(box.call({tool_name:'Bash',tool_input:{command},tool_use_id:'source-query'}),{});
  assert.equal(box.events().at(-1).command_hash,sha256(command));
  assert.equal(box.events().at(-1).allowed,true);
  assert.equal(JSON.stringify(box.events()).includes(command),false);
  const refused=box.call({tool_name:'Bash',tool_input:{command:"rg -n '.zcode' .zcode/settings.json"},tool_use_id:'private-target'});
  assert.equal(refused.hookSpecificOutput.permissionDecision,'deny');
});

test('ordinary native sessions bypass completely without reading the project',t=>{
  const project=fs.mkdtempSync(path.join(os.tmpdir(),'md-zcode-plain-'));
  t.after(()=>fs.rmSync(project,{recursive:true,force:true}));
  assert.deepEqual(guard.main(project,{session_id:'native-plain-session',hook_event_name:'PreToolUse',
    tool_name:'Write',tool_input:{file_path:path.join(project,'src','x.js'),content:'x'},tool_use_id:'c'}),{});
  assert.deepEqual(guard.main(project,{hook_event_name:'PreToolUse',tool_name:'Read',tool_input:{}}),{});
  assert.deepEqual(fs.readdirSync(project),[]);
});

test('unbound controlled-prefix sessions are denied, not silently passed',t=>{
  const box=fixture(t,{index:{schema:1,sessions:{}}});
  assert.throws(()=>box.call({}),/未绑定|校验失败/);
  assert.equal(fs.existsSync(path.join(box.run,'zcode-native-proof.json')),false);
  assert.equal(box.events().length,0);
});

test('SessionStart binds the proof once and records no tool rows',t=>{
  const box=fixture(t);
  assert.deepEqual(box.call({hook_event_name:'SessionStart'}),{});
  const proof=box.proof();
  assert.equal(proof.source,'zcode-native-hooks');
  assert.equal(proof.probe,'ok');
  assert.equal(proof.session_id,SESSION);
  assert.equal(proof.task_id,box.taskId);
  assert.equal(proof.nonce,box.job.nonce);
  assert.equal(proof.revision,'fixture-plan-revision');
  assert.equal(proof.mode,'zcode-native-v1');
  assert.equal(proof.access_mode,'project');
  assert.equal(proof.job_hash,sha256(box.jobBytes));
  assert.equal(fs.existsSync(path.join(box.run,'zcode-native-events.jsonl')),false);
  // The same session may re-run SessionStart; another native session cannot
  // take over the bound task even if job and index are rewritten for it.
  assert.deepEqual(box.call({hook_event_name:'SessionStart'}),{});
  const second='modeldesk-zcode-second-session';
  const job2=makeJob(box.project,box.taskId,second,{nonce:box.job.nonce});
  const job2Bytes=Buffer.from(JSON.stringify(job2));
  fs.writeFileSync(path.join(box.run,'zcode-native-job.json'),job2Bytes);
  fs.writeFileSync(path.join(box.project,'.model-desk','zcode-native-sessions.json'),
    JSON.stringify({schema:1,sessions:{[second]:{task_id:box.taskId,job_hash:sha256(job2Bytes)}}}));
  assert.throws(()=>guard.main(box.project,{session_id:second,hook_event_name:'SessionStart'}),/换绑/);
  assert.equal(box.proof().session_id,SESSION);
});

test('the first legal PreToolUse binds the proof when SessionStart never ran',t=>{
  const box=fixture(t);
  assert.deepEqual(box.call({}),{});
  assert.equal(box.proof().session_id,SESSION);
  assert.equal(box.events().length,1);
  // A PostToolUse before any binding cannot create the proof itself.
  const fresh=fixture(t);
  assert.throws(()=>fresh.call({hook_event_name:'PostToolUse',tool_name:'Read',
    tool_input:{file_path:path.join(fresh.project,'src','main.js')},tool_response:{exitCode:0},tool_use_id:'c'}),/绑定|校验/);
  assert.equal(fs.existsSync(path.join(fresh.run,'zcode-native-proof.json')),false);
});

test('an existing proof cannot be rebound through PreToolUse',t=>{
  const box=fixture(t);
  assert.deepEqual(box.call({hook_event_name:'SessionStart'}),{});
  const second='modeldesk-zcode-second';
  const job2=makeJob(box.project,box.taskId,second,{nonce:box.job.nonce});
  const job2Bytes=Buffer.from(JSON.stringify(job2));
  fs.writeFileSync(path.join(box.run,'zcode-native-job.json'),job2Bytes);
  fs.writeFileSync(path.join(box.project,'.model-desk','zcode-native-sessions.json'),
    JSON.stringify({schema:1,sessions:{[second]:{task_id:box.taskId,job_hash:sha256(job2Bytes)}}}));
  assert.throws(()=>guard.main(box.project,{session_id:second,hook_event_name:'PreToolUse',tool_name:'Read',
    tool_input:{file_path:path.join(box.project,'src','main.js')},tool_use_id:'c'}),/不匹配/);
  assert.equal(box.proof().session_id,SESSION);
  assert.equal(box.events().length,0);
});

test('pre/post events carry fixed fields only and never raw parameters',t=>{
  const box=fixture(t);
  box.call({tool_use_id:'call-read'});
  box.call({tool_name:'Bash',tool_input:{command:'node build.js --token=MARKER-CMD'},tool_use_id:'call-bash'});
  box.call({hook_event_name:'PostToolUse',tool_name:'Bash',tool_input:{command:'node build.js --token=MARKER-CMD'},
    tool_use_id:'call-bash',tool_response:{stdout:'done',exitCode:0}});
  box.call({hook_event_name:'PostToolUse',tool_name:'Read',
    tool_input:{file_path:path.join(box.project,'src','main.js')},tool_use_id:'call-read',
    tool_response:{content:'text without exit basis'}});
  const rows=box.events();
  assert.equal(rows.length,4);
  for(const row of rows){
    assert.deepEqual(Object.keys(row).sort(),
      ['allowed','at','call_id','command_hash','denial_rule','event','exit_code','input_hash','nonce','ok','reason_code','session_id','tool']);
    assert.equal(row.nonce,box.job.nonce);
    assert.equal(row.session_id,SESSION);
  }
  assert.equal(rows[0].event,'PreToolUse');
  assert.equal(rows[0].allowed,true);
  assert.equal(rows[0].reason_code,null);
  assert.equal(rows[0].input_hash,sha256(JSON.stringify(stable({file_path:path.join(box.project,'src','main.js')}))));
  assert.equal(rows[1].tool,'Bash');
  assert.equal(rows[1].command_hash,sha256('node build.js --token=MARKER-CMD'));
  assert.equal(rows[2].exit_code,0);
  assert.equal(rows[2].ok,true);
  // exit_code is only recorded on a real native field, never inferred.
  assert.equal(rows[3].exit_code,null);
  assert.equal(rows[3].ok,true);
  const evidence=fs.readFileSync(path.join(box.run,'zcode-native-events.jsonl'),'utf8')
    +fs.readFileSync(path.join(box.run,'zcode-native-proof.json'),'utf8');
  assert.ok(!evidence.includes('MARKER-CMD'));
  assert.ok(!evidence.includes('main.js'));
  assert.ok(!evidence.includes('stdout'));
});

test('nonzero exits and PostToolUseFailure only log and never pause the task',t=>{
  const box=fixture(t);
  assert.deepEqual(box.call({tool_use_id:'call-bind'}),{});
  box.call({hook_event_name:'PostToolUse',tool_name:'Bash',tool_input:{command:'node build.js'},
    tool_response:{exitCode:1},tool_use_id:'call-fail'});
  box.call({hook_event_name:'PostToolUseFailure',tool_name:'Edit',
    tool_input:{file_path:path.join(box.project,'src','main.js')},tool_response:{exitCode:2},tool_use_id:'call-f2'});
  box.call({hook_event_name:'PostToolUse',tool_name:'Bash',tool_input:{command:'node build.js'},
    tool_response:{is_error:true},tool_use_id:'call-err'});
  const rows=box.events();
  assert.equal(rows.length,4);
  assert.equal(rows[0].event,'PreToolUse');
  assert.equal(rows[1].exit_code,1);
  assert.equal(rows[1].ok,false);
  assert.equal(rows[2].event,'PostToolUseFailure');
  assert.equal(rows[2].allowed,true);
  assert.equal(rows[2].exit_code,2);
  assert.equal(rows[3].ok,false);
  assert.equal(fs.existsSync(path.join(box.run,'zcode-native-denied.flag')),false);
  assert.deepEqual(box.call({tool_use_id:'call-after'}),{});
});

test('policy denials stop the task across tools until reported',t=>{
  const box=fixture(t);
  const record=path.join(box.project,'.model-desk','records.db');
  const denied=box.call({tool_name:'Write',tool_input:{file_path:record,content:'HACKED'},tool_use_id:'call-w'});
  assert.equal(denied.hookSpecificOutput.hookEventName,'PreToolUse');
  assert.equal(denied.hookSpecificOutput.permissionDecision,'deny');
  assert.ok(denied.hookSpecificOutput.permissionDecisionReason.includes('工作台记录'));
  assert.equal(fs.readFileSync(path.join(box.run,'zcode-native-denied.flag'),'utf8'),'denied');
  const rows=box.events();
  assert.equal(rows[0].allowed,false);
  assert.equal(rows[0].reason_code,'protected_data');
  assert.equal(rows[0].denial_rule,'protected_file');
  // Even normally legitimate calls cannot retry through another tool.
  for(const attempt of [
    {tool_name:'Read',tool_input:{file_path:path.join(box.project,'src','main.js')}},
    {tool_name:'Write',tool_input:{file_path:path.join(box.project,'src','main.js'),content:'ok'}},
    {tool_name:'Bash',tool_input:{command:'node build.js'}}]){
    const result=box.call({...attempt,tool_use_id:'call-retry'});
    assert.equal(result.hookSpecificOutput.permissionDecision,'deny');
    assert.ok(result.hookSpecificOutput.permissionDecisionReason.includes('已暂停'));
  }
  assert.equal(box.events().length,4);
  for(const row of box.events().slice(1)){
    assert.equal(row.reason_code,'prior_denial');
    assert.equal(row.denial_rule,'prior_denial');
  }
  assert.equal(fs.readFileSync(record,'utf8'),'RECORD-BYTES');
});

test('native profiles and terminal record references stay protected',t=>{
  const box=fixture(t);
  const profile=box.call({tool_name:'Write',tool_input:{file_path:path.join(box.project,'.zcode','settings.json'),
    content:'{}'},tool_use_id:'call-p'});
  assert.equal(profile.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(profile.hookSpecificOutput.permissionDecisionReason,'认证存储不作为执行目标');
  assert.equal(box.events().at(-1).reason_code,'protected_data');
  assert.equal(box.events().at(-1).denial_rule,'credential_store');
  const shellBox=fixture(t);
  const shell=shellBox.call({tool_name:'Bash',
    tool_input:{command:'type "'+path.join(shellBox.project,'.zcode','settings.json')+'"'},tool_use_id:'call-s'});
  assert.equal(shell.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(shell.hookSpecificOutput.permissionDecisionReason,'终端不能访问原生配置目录');
  const recordBox=fixture(t);
  const record=recordBox.call({tool_name:'Bash',tool_input:{command:'type .model-desk\\records.db'},tool_use_id:'call-r'});
  assert.equal(record.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(record.hookSpecificOutput.permissionDecisionReason,'终端不能访问工作台记录或认证数据');
  const relativeBox=fixture(t,{job:{protected_roots:[]}});
  const relative=relativeBox.call({tool_name:'Bash',tool_input:{command:'type .zcode\\config.json'},tool_use_id:'call-relative'});
  assert.equal(relative.hookSpecificOutput.permissionDecision,'deny');
});

test('read-only jobs cannot write or open a terminal',t=>{
  const box=fixture(t,{job:{kind:'chat'}});
  assert.deepEqual(box.call({}),{});
  const write=box.call({tool_name:'Write',tool_input:{file_path:path.join(box.project,'src','main.js'),
    content:'x'},tool_use_id:'call-w'});
  assert.equal(write.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(write.hookSpecificOutput.permissionDecisionReason,'只读任务没有写入权限');
  assert.equal(box.events().at(-1).reason_code,'tool_not_allowed');
  // The read-only overreach is a real policy denial and pauses the task.
  const next=box.call({tool_use_id:'call-r2'});
  assert.ok(next.hookSpecificOutput.permissionDecisionReason.includes('已暂停'));
  const bashBox=fixture(t,{job:{kind:'chat'}});
  const bash=bashBox.call({tool_name:'Bash',tool_input:{command:'node build.js'},tool_use_id:'call-b'});
  assert.equal(bash.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(bash.hookSpecificOutput.permissionDecisionReason,'只读任务没有终端权限');
  assert.equal(bashBox.events().at(-1).denial_rule,'tool_not_allowed');
});

test('project-scoped jobs refuse undeclared, outside and unauthorized writes',t=>{
  const box=fixture(t);
  assert.deepEqual(box.call({tool_name:'Write',tool_input:{file_path:path.join(box.project,'src','main.js'),
    content:'x'},tool_use_id:'call-ok'}),{});
  const undeclared=box.call({tool_name:'Write',tool_input:{file_path:path.join(box.project,'src','other.js'),
    content:'x'},tool_use_id:'call-u'});
  assert.equal(undeclared.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(undeclared.hookSpecificOutput.permissionDecisionReason,'文件未列入本任务可写范围');
  assert.equal(box.events().at(-1).reason_code,'undeclared_write');
  const outsideBox=fixture(t);
  const outside=outsideBox.call({tool_name:'Write',
    tool_input:{file_path:path.join(outsideBox.project,'..','outside.txt'),content:'x'},tool_use_id:'call-o'});
  assert.equal(outside.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(outside.hookSpecificOutput.permissionDecisionReason,'文件不属于本任务项目');
  const toolsBox=fixture(t,{job:{tools:['Read','Glob','Grep']}});
  const unauthorized=toolsBox.call({tool_name:'Write',
    tool_input:{file_path:path.join(toolsBox.project,'src','main.js'),content:'x'},tool_use_id:'call-t'});
  assert.equal(unauthorized.hookSpecificOutput.permissionDecisionReason,'本任务未授权此原生工具');
  assert.equal(toolsBox.events().at(-1).reason_code,'tool_not_allowed');
});

test('the declared task temp directory accepts scratch writes but nothing else changes',t=>{
  const box=fixture(t,{job:{temp_dir:'.modeldesk-task-tmp'}});
  fs.mkdirSync(path.join(box.project,'.modeldesk-task-tmp'));
  // Scratch material inside the declared directory is writable without a
  // deliverable declaration: test output, nested runs and edits to it.
  for(const file of [path.join(box.project,'.modeldesk-task-tmp','test-output.txt'),
                     path.join(box.project,'.modeldesk-task-tmp','nested','deep.log')]){
    assert.deepEqual(box.call({tool_name:'Write',tool_input:{file_path:file,content:'x'},tool_use_id:'call-t'}),{});
  }
  assert.deepEqual(box.call({tool_name:'Edit',tool_input:{file_path:path.join(box.project,'.modeldesk-task-tmp','test-output.txt'),
    old_string:'x',new_string:'y'},tool_use_id:'call-e'}),{});
  // Deliverable declaration rules stay intact outside the directory.
  const undeclared=box.call({tool_name:'Write',tool_input:{file_path:path.join(box.project,'src','other.js'),
    content:'x'},tool_use_id:'call-u'});
  assert.equal(undeclared.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(undeclared.hookSpecificOutput.permissionDecisionReason,'文件未列入本任务可写范围');
  // A similarly named sibling is not the declared directory (fresh fixture:
  // the refusal above already paused this run's tools).
  const siblingBox=fixture(t,{job:{temp_dir:'.modeldesk-task-tmp'}});
  const sibling=siblingBox.call({tool_name:'Write',
    tool_input:{file_path:path.join(siblingBox.project,'.modeldesk-task-tmp-backup','x.txt'),content:'x'},tool_use_id:'call-s'});
  assert.equal(sibling.hookSpecificOutput.permissionDecisionReason,'文件未列入本任务可写范围');
});

test('scratch root junction cannot authorize source aliases or direct undeclared source writes',t=>{
  const shared=require(path.join(AGENT,'workbuddy-native-guard.cjs'));
  const box=fixture(t,{job:{temp_dir:'.modeldesk-task-tmp'}});
  const scratch=path.join(box.project,'.modeldesk-task-tmp');
  fs.symlinkSync(path.join(box.project,'src'),scratch,'junction');
  assert.equal(shared.target(box.job,path.join(box.project,'src','other.js'),true),'文件未列入本任务可写范围');
  assert.match(shared.target(box.job,path.join(scratch,'other.js'),true),/临时目录.*别名/);
  fs.unlinkSync(scratch);
  fs.mkdirSync(scratch);
  assert.equal(shared.target(box.job,path.join(scratch,'ordinary.txt'),true),null);
  // Swap a child in after preparation: checking only the root is insufficient.
  const child=path.join(scratch,'linked');
  fs.symlinkSync(path.join(box.project,'src'),child,'junction');
  assert.match(shared.target(box.job,path.join(child,'other.js'),true),/临时目录.*越界/);
  fs.unlinkSync(child);
  const protectedChild=path.join(scratch,'records');
  fs.symlinkSync(path.join(box.project,'.model-desk'),protectedChild,'junction');
  assert.match(shared.target(box.job,path.join(protectedChild,'records.db'),true),/记录|认证/);
  fs.unlinkSync(protectedChild);
  // A root swapped in after an earlier successful call is checked again.
  fs.rmdirSync(scratch);
  fs.symlinkSync(path.join(box.project,'src'),scratch,'junction');
  assert.match(shared.target(box.job,path.join(scratch,'ordinary.txt'),true),/临时目录.*别名/);
  fs.unlinkSync(scratch);
});

test('temp directory allowance needs the workbench declaration and never covers read-only or protected targets',t=>{
  // Without temp_dir in the job the same path is just another undeclared write.
  const undeclared=fixture(t);
  const refused=undeclared.call({tool_name:'Write',
    tool_input:{file_path:path.join(undeclared.project,'.modeldesk-task-tmp','x.txt'),content:'x'},tool_use_id:'call-1'});
  assert.equal(refused.hookSpecificOutput.permissionDecisionReason,'文件未列入本任务可写范围');
  // An invalid or hostile shape simply declares nothing; every target below
  // stays denied exactly as before the feature existed.
  for(const steer of ['.model-desk','.zcode','../outside','.modeldesk-task-tmp/../../escape','C:\\elsewhere']){
    const box=fixture(t,{job:{temp_dir:steer}});
    const call=box.call({tool_name:'Write',tool_input:{file_path:path.join(box.project,steer,'x.txt'),content:'x'},tool_use_id:'c'});
    assert.equal(call.hookSpecificOutput.permissionDecision,'deny',steer+' must stay denied');
  }
  // Read-only jobs keep the write refusal even when a declaration exists.
  const chat=fixture(t,{job:{kind:'chat',tools:['Read','Glob','Grep','WebFetch','WebSearch'],temp_dir:'.modeldesk-task-tmp'}});
  const chatWrite=chat.call({tool_name:'Write',
    tool_input:{file_path:path.join(chat.project,'.modeldesk-task-tmp','x.txt'),content:'x'},tool_use_id:'c'});
  assert.equal(chatWrite.hookSpecificOutput.permissionDecisionReason,'只读任务没有写入权限');
});

test('selftest diagnostic records hash-only command shape when opted in',t=>{
  const h=v=>crypto.createHash('sha256').update(v).digest('hex');
  const box=fixture(t,{job:{selftest_diagnostic:true}});
  const command='cd somedir && node check.cjs';
  box.call({tool_name:'Bash',tool_input:{command},tool_use_id:'call-d'});
  const row=box.events().at(-1);
  assert.equal(row.command_diag.len,command.length);
  assert.equal(row.command_diag.group_count,2);
  assert.deepEqual(row.command_diag.groups,[h('cd somedir'),h('node check.cjs')]);
  assert.equal(row.command_diag.trimmed,h(command.trim()));
  assert.equal(row.command_diag.line_endings_normalized,h(command));
  // Hash-only: the raw command text never reaches the event stream.
  assert.ok(!JSON.stringify(row).includes('somedir'));
  // A trailing newline makes the compound unparseable by design: groups stay
  // null and the worker classifies the run as an unparsable compound.
  const wrapped=fixture(t,{job:{selftest_diagnostic:true}});
  wrapped.call({tool_name:'Bash',tool_input:{command:'node check.cjs'+String.fromCharCode(13,10)},tool_use_id:'call-w'});
  const wrappedRow=wrapped.events().at(-1);
  assert.equal(wrappedRow.command_diag.groups,null);
  assert.equal(wrappedRow.command_diag.trimmed,h('node check.cjs'));
  assert.equal(wrappedRow.command_diag.line_endings_normalized,h('node check.cjs'+String.fromCharCode(10)));
  // Not opted in: the field is absent entirely, so event rows keep their
  // fixed shape.
  const plain=fixture(t);
  plain.call({tool_name:'Bash',tool_input:{command},tool_use_id:'call-p'});
  assert.ok(!('command_diag' in plain.events().at(-1)));
});

test('identity, index hash, schema and revision mismatches fail closed',t=>{
  // Job bytes edited after indexing.
  const tampered=fixture(t);
  fs.writeFileSync(path.join(tampered.run,'zcode-native-job.json'),JSON.stringify({...tampered.job,extra:1}));
  assert.throws(()=>tampered.call({}),/索引|一致/);
  // Stale guard digest, or a job missing the controller version binding.
  const stale=fixture(t,{job:{guard_revision:'stale-revision'}});
  assert.throws(()=>stale.call({}),/身份校验/);
  const unbound=fixture(t,{job:{revision:undefined}});
  assert.throws(()=>unbound.call({}),/身份校验/);
  // Wrong job schema, mode, native session, access mode and run identity.
  for(const overrides of [{schema:2},{mode:'native-development-v1'},{kind:'browse'},
    {access_mode:'write-all'},{native_session_id:'modeldesk-zcode-other'},{task_id:'task-elsewhere'},
    {protected_roots:['.zcode']},{project:path.join(os.tmpdir(),'elsewhere-project')}]){
    const box=fixture(t,{job:overrides});
    assert.throws(()=>box.call({}),/身份校验/);
  }
  // Index schema and unreachable or traversal task ids.
  const badIndex=fixture(t,{index:{schema:2,sessions:{[SESSION]:{task_id:'x',job_hash:'y'}}}});
  assert.throws(()=>badIndex.call({}),/索引/);
  const ghost=fixture(t,{index:{schema:1,sessions:{[SESSION]:{task_id:'task-ghost',job_hash:sha256('{}')}}}});
  assert.throws(()=>ghost.call({}));
  const evil=fixture(t,{index:{schema:1,sessions:{[SESSION]:{task_id:'..\\evil',job_hash:'x'}}}});
  assert.throws(()=>evil.call({}),/未绑定/);
  // Missing profile directories in the binding stay fail-closed.
  for(const box of [tampered,stale,unbound]) assert.equal(fs.existsSync(path.join(box.run,'zcode-native-proof.json')),false);
});

test('native directory Grep excludes private content; Glob metadata and missing paths stay native',t=>{
  const box=fixture(t);
  assert.deepEqual(box.call({tool_name:'Glob',tool_input:{path:path.join(box.project,'src'),pattern:'*.js'},
    tool_use_id:'g0'}),{});
  assert.ok(box.call({tool_name:'Grep',tool_input:{path:path.join(box.project,'src'),pattern:'fixture'},
    tool_use_id:'g1'}).hookSpecificOutput.updatedInput.glob.includes('.model-desk'));
  // A missing search path is left to the native tool, not upgraded to a
  // policy denial by a throwing stat.
  assert.deepEqual(box.call({tool_name:'Grep',tool_input:{path:path.join(box.project,'docs','missing'),
    pattern:'x'},tool_use_id:'g2'}),{});
  // With a declared native profile the shared guard refuses the root search.
  const profiled=box.call({tool_name:'Grep',tool_input:{pattern:'x'},tool_use_id:'g3'});
  assert.equal(profiled.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(profiled.hookSpecificOutput.permissionDecisionReason,'目录检索范围包含原生配置；请选择更具体的文件');
  assert.equal(box.events().at(-1).denial_rule,'profile_search');
  // Native PreToolUse supports updatedInput. Directory Grep keeps the query
  // but substitutes a read-only exclusion filter; no permission is granted.
  const bare=fixture(t,{job:{protected_roots:[]}});
  const rootGrep=bare.call({tool_name:'Grep',tool_input:{pattern:'x'},tool_use_id:'g4'});
  assert.equal(rootGrep.hookSpecificOutput.permissionDecision,undefined);
  assert.equal(rootGrep.hookSpecificOutput.updatedInput.pattern,'x');
  assert.ok(rootGrep.hookSpecificOutput.updatedInput.glob.includes('.zcode'));
  assert.equal(bare.events().at(-1).allowed,true);
  assert.equal(fs.existsSync(path.join(bare.run,'zcode-native-denied.flag')),false);
  const bareGlob=fixture(t,{job:{protected_roots:[]}});
  const rootGlob=bareGlob.call({tool_name:'Glob',tool_input:{pattern:'*.js'},tool_use_id:'g5'});
  assert.deepEqual(rootGlob,{});
});

test('CLI passes allowed calls through and returns native deny shapes',t=>{
  const box=fixture(t);
  const allowed=runCli(box,{session_id:SESSION,hook_event_name:'PreToolUse',tool_name:'Read',
    tool_input:{file_path:path.join(box.project,'src','main.js')},tool_use_id:'call-1'});
  assert.equal(allowed.status,0);
  assert.deepEqual(JSON.parse(allowed.stdout),{});
  const denied=runCli(box,{session_id:SESSION,hook_event_name:'PreToolUse',tool_name:'Write',
    tool_input:{file_path:path.join(box.project,'.model-desk','records.db'),content:'x'},tool_use_id:'call-2'});
  assert.equal(denied.status,0);
  const shape=JSON.parse(denied.stdout);
  assert.equal(shape.hookSpecificOutput.hookEventName,'PreToolUse');
  assert.equal(shape.hookSpecificOutput.permissionDecision,'deny');
  // A denied task stays denied through the CLI too.
  const retry=runCli(box,{session_id:SESSION,hook_event_name:'PreToolUse',tool_name:'Bash',
    tool_input:{command:'node build.js'},tool_use_id:'call-3'});
  assert.equal(retry.status,0);
  assert.ok(JSON.parse(retry.stdout).hookSpecificOutput.permissionDecisionReason.includes('已暂停'));
  const bypass=runCli(box,{session_id:'native-plain',hook_event_name:'PreToolUse',
    tool_name:'Bash',tool_input:{command:'x'}});
  assert.equal(bypass.status,0);
  assert.deepEqual(JSON.parse(bypass.stdout),{});
});

test('CLI identity failures exit 2 with a controlled deny and never echo input',t=>{
  const box=fixture(t,{index:{schema:1,sessions:{}}});
  const unbound=runCli(box,{session_id:SESSION,hook_event_name:'PreToolUse',tool_name:'Read',
    tool_input:{file_path:path.join(box.project,'src','main.js')},tool_use_id:'call-1'});
  assert.equal(unbound.status,2);
  const out=JSON.parse(unbound.stdout);
  assert.equal(out.hookSpecificOutput.permissionDecision,'deny');
  assert.ok(!unbound.stdout.includes(SESSION));
  assert.ok(!unbound.stdout.includes('main.js'));
  const garbage=spawnSync(process.execPath,[GUARD_FILE,box.project],{input:'not-json',encoding:'utf8'});
  assert.equal(garbage.status,2);
  assert.equal(JSON.parse(garbage.stdout).hookSpecificOutput.permissionDecision,'deny');
});

test('bypassed sessions leave no binding or event records',t=>{
  const box=fixture(t);
  assert.deepEqual(guard.main(box.project,{session_id:'native-other',hook_event_name:'PreToolUse',
    tool_name:'Write',tool_input:{file_path:path.join(box.project,'src','main.js'),content:'x'},
    tool_use_id:'c'}),{});
  assert.equal(fs.existsSync(path.join(box.run,'zcode-native-proof.json')),false);
  assert.equal(fs.existsSync(path.join(box.run,'zcode-native-events.jsonl')),false);
  assert.equal(fs.existsSync(path.join(box.run,'zcode-native-denied.flag')),false);
});
