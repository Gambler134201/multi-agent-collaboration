'use strict';
// ZCode native hook adapter: a thin binding layer that implements no
// Read/Write/Edit/Bash tool logic. Native sessions whose session_id starts
// with `modeldesk-zcode-` are bound to a controller task via the project
// index, and every permission decision for the native Read/Glob/Grep/Write/
// Edit/Bash tools is delegated to the shared workbuddy native guard and the
// record policy. Every other session returns immediately without reading any
// index or record.
//
// Native shapes are version-bound and checked against the installed runtime:
//   - job.revision is the controller's full protection version (guard files
//     plus the task-visible tool/hook plan); this adapter verifies only its
//     own guard_revision digest of the three guard modules.
//   - PostToolUse results are recorded only from fields actually present in
//     tool_response (exitCode and error markers); nothing is inferred.
//   - Glob lists paths, not content. Grep directory searches receive the
//     native PreToolUse.updatedInput exclusion glob; outputs are not rewritten.
//   - Unknown hook event names carry no permission decision; they pass
//     through without event rows once the session binding is verified.
//   - Missing ordinary tool paths stay with the native tool: no fs.statSync
//     is allowed to turn a missing search target into a policy denial.
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const policy=require('./record-policy.cjs');
const workbuddy=require('./workbuddy-native-guard.cjs');
const SESSION_PREFIX='modeldesk-zcode-';
const JOB_MODE='zcode-native-v1';
const STICKY_REASON='本轮工具已暂停，请报告主脑，不要换工具重试';
const READS=new Set(['Read','Glob','Grep']);
const WRITES=new Set(['Write','Edit','MultiEdit','NotebookEdit']);
const SHELLS=new Set(['Bash','PowerShell']);
const SEARCHES=new Set(['Glob','Grep']);
const PROOF_KEYS=['source','task_id','project','nonce','revision','mode','access_mode','job_hash','session_id','probe'];
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const stable=value=>Array.isArray(value)?value.map(stable):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(k=>[k,stable(value[k])])):value;
const deny=reason=>({hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',permissionDecisionReason:reason}});

// Opt-in selftest diagnostic (job.selftest_diagnostic, written by prepare()
// only when the task declares a test command): per Bash call, hash-only shape
// evidence so the worker can classify a missed exact-hash match as a format
// difference, a compound-command group, or a genuinely different command -
// without ever recording command text.  The field is absent from event rows
// entirely when not opted in, so existing rows keep their fixed shape.
function commandDiag(command){
  const text=String(command);
  const groups=policy.commandGroups(text);
  return {len:text.length,trimmed:hash(text.trim()),
          line_endings_normalized:hash(text.replace(/\r\n?/g,'\n')),
          groups:Array.isArray(groups)?groups.map(g=>hash(g.trim())):null,
          group_count:Array.isArray(groups)?groups.length:null};
}

/** Concatenated SHA256 of this adapter, the shared guard and the record policy. */
function revision(){
  return hash(Buffer.concat([fs.readFileSync(__filename),
    fs.readFileSync(path.join(__dirname,'workbuddy-native-guard.cjs')),
    fs.readFileSync(path.join(__dirname,'record-policy.cjs')),
    fs.readFileSync(path.join(__dirname,'endpoint-access.cjs'))]));
}

// Event denial codes are the shared workbuddy mapping (denialCode): this
// adapter keeps no copy of its own. The decisions themselves stay inside the
// shared guard.

function loadJob(project,input){
  const index=JSON.parse(fs.readFileSync(path.join(project,'.model-desk','zcode-native-sessions.json'),'utf8'));
  if(!index||index.schema!==1||!index.sessions||typeof index.sessions!=='object'||Array.isArray(index.sessions))throw Error('控制器索引校验失败');
  const entry=index.sessions[input.session_id];
  if(!entry||typeof entry.task_id!=='string'||typeof entry.job_hash!=='string'
    ||!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(entry.task_id))throw Error('原生会话未绑定任务');
  const run=path.resolve(project,'.model-desk','runs',entry.task_id);
  const jobBytes=fs.readFileSync(path.join(run,'zcode-native-job.json'));
  if(hash(jobBytes)!==entry.job_hash)throw Error('任务工作描述与索引不一致');
  const job=JSON.parse(jobBytes);
  const valid=!!job&&job.schema===1&&job.mode===JOB_MODE&&typeof job.revision==='string'
    &&job.guard_revision===revision()
    &&(job.kind==='chat'||job.kind==='edit')
    &&(job.access_mode==='project'||job.access_mode==='full')
    &&typeof job.nonce==='string'&&!!job.nonce
    &&typeof job.project==='string'&&!!job.project
    &&Array.isArray(job.files)&&job.files.every(f=>typeof f==='string')
    &&Array.isArray(job.readonly_files)&&job.readonly_files.every(f=>typeof f==='string')
    &&Array.isArray(job.tools)&&job.tools.every(t=>typeof t==='string')
    &&Array.isArray(job.protected_roots)&&job.protected_roots.every(p=>typeof p==='string'&&path.isAbsolute(p))
    &&job.native_session_id===input.session_id
    &&path.resolve(job.record_project||job.project)===path.resolve(project)
    &&path.resolve(run)===path.resolve(job.record_project||job.project,'.model-desk','runs',job.task_id);
  if(!valid)throw Error('工具任务身份校验失败');
  policy.taskPolicy(job);
  workbuddy.validateBinding(run,job);
  return {job,jobBytes,run};
}

// Binding evidence: written only on a legal SessionStart or the first legal
// PreToolUse, and never re-bound to another native session afterwards.
function ensureProof(run,job,jobBytes,input,sessionStart){
  const proofFile=path.join(run,'zcode-native-proof.json');
  const proof={source:'zcode-native-hooks',task_id:job.task_id,project:job.project,nonce:job.nonce,
    revision:job.revision,mode:job.mode,access_mode:job.access_mode,job_hash:hash(jobBytes),
    session_id:input.session_id,probe:'ok'};
  if(job.record_project)proof.record_project=job.record_project;
  if(fs.existsSync(proofFile)){
    const old=JSON.parse(fs.readFileSync(proofFile,'utf8'));
    if([...PROOF_KEYS,'record_project'].some(k=>proof[k]!==old[k]))throw Error(sessionStart?'不能换绑原会话':'原生工具父会话不匹配');
    return;
  }
  if(!sessionStart&&input.hook_event_name!=='PreToolUse')throw Error('原生会话缺少绑定证据');
  if(policy.decide(policy.PROBE_TOOL,{},{root:job.project})!==policy.PROBE_REASON)throw Error('记录保护探针失败');
  fs.writeFileSync(proofFile,JSON.stringify(proof));
}

function searchInput(job,input){
  if(input.tool_name!=='Grep')return null;
  const value=input.tool_input&&input.tool_input.path;
  const base=typeof value==='string'&&value.trim()?path.resolve(job.project,value):path.resolve(job.project);
  if(fs.existsSync(base)&&fs.statSync(base).isDirectory())return {...input.tool_input,glob:workbuddy.searchExclusionGlob(job,base)};
  return null;
}

function decideCall(job,input){
  const name=input.tool_name;
  // Adapter-level access rule: read-only jobs never gain write authority even
  // if the job description lists those tools; terminals are refused by the
  // shared guard's own read-only rule.
  if(job.kind!=='edit'&&typeof name==='string'&&WRITES.has(name))return '只读任务没有写入权限';
  return workbuddy.decide(job,input);
}

function main(project,input){
  // Ordinary native sessions bypass before any index or record is read.
  if(!input||typeof input.session_id!=='string'||!input.session_id.startsWith(SESSION_PREFIX))return {};
  const {job,jobBytes,run}=loadJob(project,input);
  const flag=path.join(run,'zcode-native-denied.flag');
  if(input.hook_event_name==='SessionStart'){ensureProof(run,job,jobBytes,input,true);return {};}
  ensureProof(run,job,jobBytes,input,false);
  if(!['PreToolUse','PostToolUse','PostToolUseFailure'].includes(input.hook_event_name))return {};
  let reason=null;
  if(input.hook_event_name==='PreToolUse')reason=fs.existsSync(flag)?STICKY_REASON:decideCall(job,input);
  const response=input.tool_response;
  const raw=response&&typeof response==='object'&&response.rawResponse&&typeof response.rawResponse==='object'?response.rawResponse:response;
  const exitCode=Number.isInteger(raw&&raw.exitCode)?raw.exitCode:null;
  const failed=Boolean((raw&&(raw.is_error||raw.isError||raw.interrupted||raw.sandboxDenied))||(response&&response.error));
  const row={event:input.hook_event_name,session_id:input.session_id,nonce:job.nonce,
    call_id:input.tool_use_id||input.call_id||null,tool:input.tool_name,
    input_hash:hash(JSON.stringify(stable(input.tool_input||{}))),
    allowed:!reason,reason_code:reason?workbuddy.denialCode(reason):null,denial_rule:reason?workbuddy.denialRule(reason):null,
    command_hash:input.tool_input&&typeof input.tool_input.command==='string'?hash(input.tool_input.command):null,
    exit_code:exitCode,
    ok:input.hook_event_name==='PostToolUse'&&!failed&&!(Number.isInteger(exitCode)&&exitCode!==0),
    at:new Date().toISOString(),
    ...(job.selftest_diagnostic===true&&input.tool_input&&typeof input.tool_input.command==='string'
        ?{command_diag:commandDiag(input.tool_input.command)}:{})};
  fs.appendFileSync(path.join(run,'zcode-native-events.jsonl'),JSON.stringify(row)+'\n');
  if(reason){fs.writeFileSync(flag,'denied');return deny(reason);}
  if(input.hook_event_name==='PreToolUse'){
    const updated=searchInput(job,input);
    if(updated)return {hookSpecificOutput:{hookEventName:'PreToolUse',updatedInput:updated,
      additionalContext:'目录检索已加私有目录排除。原 glob 过滤如需进一步收窄，请指定更具体路径并复核文件名。'}};
  }
  // No explicit allow decision: the native permission rules keep running.
  return {};
}

module.exports={main,revision};
if(require.main===module){
  try{
    const input=JSON.parse(fs.readFileSync(0,'utf8'));
    process.stdout.write(JSON.stringify(main(process.argv[2],input)));
  }catch(_){
    // Controlled deny: never echo the input or the underlying error.
    process.stdout.write(JSON.stringify(deny('原生工具保护校验失败，请报告主脑')));
    process.exitCode=2;
  }
}
