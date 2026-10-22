'use strict';
// Native hooks run before the tool body, including read tools without approvals.
// This is an argument guard, not an OS sandbox for arbitrary programs/scripts.
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const policy=require('./record-policy.cjs');
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const stable=value=>Array.isArray(value)?value.map(stable):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(k=>[k,stable(value[k])])):value;
const reads=new Set(['Read','Glob','Grep','NotebookRead']);
const writes=new Set(['Write','Edit','MultiEdit','NotebookEdit']);
const shells=new Set(['Bash','PowerShell']);
const web=new Set(['WebFetch','WebSearch']);
function target(job,value,write=false,browse=false){
  if(typeof value!=='string'||!value.trim())return '文件路径无效';
  const p=path.resolve(job.project,value),real=policy.realpathDeep(p),root=policy.realpathDeep(job.project);
  const relative=path.relative(root,real);
  if(relative.startsWith('..'+path.sep)||relative==='..'||path.isAbsolute(relative))return '文件不属于本任务项目';
  if(policy.hasProtectedComponent(p)||policy.hasProtectedComponent(real)||/(^|[\\/])(?:\.git|\.env(?:\.[^\\/]+)?)(?:[\\/]|$)/i.test(p))return '工作台记录、Git 元数据或认证配置不作为执行目标';
  if(fs.existsSync(p)&&fs.statSync(p).isFile()&&fs.statSync(p).nlink>1)return '文件存在硬链接别名';
  if(/(?:^|[\\/])(?:\.codebuddy|\.workbuddy|\.codex)(?:[\\/]|$)|(?:^|[\\/])(?:auth|credentials|tokens?|secrets?)\.json$/i.test(p))return '认证存储不作为执行目标';
  if(/[\\/]\.local-worker[\\/](?:models|settings|panel-runtime|workbuddy)\.json$/i.test(p))return '运行账户配置不作为执行目标';
  const own=[...job.files,...job.readonly_files];
  const ownsFile=own.some(n=>policy.normalizePath(path.resolve(job.project,n))===policy.normalizePath(p));
  const ownsDirectory=browse&&own.some(n=>policy.normalizePath(path.dirname(path.resolve(job.project,n)))===policy.normalizePath(p));
  if(/[\\/]\.local-worker[\\/](?:tasks|logs)[\\/]/i.test(p)&&!ownsFile&&!ownsDirectory)return '不读取其他任务或会话材料';
  if(write&&!job.files.some(name=>policy.normalizePath(path.resolve(job.project,name))===policy.normalizePath(p)))return '文件未列入本任务可写范围';
  return null;
}
function decide(job,input){
  const name=input.tool_name,args=input.tool_input;
  if(!job.tools.includes(name)||!args||typeof args!=='object'||Array.isArray(args))return '本任务未授权此原生工具';
  if(reads.has(name)){
    const p=args.file_path??args.notebook_path??args.path??'.';
    const reason=target(job,p,false,name==='Glob'||name==='Grep');if(reason)return reason;
    // Search defaults exclude records even when the root itself is the target.
    if(name==='Glob'&&/\.model-desk|\.git(?:[\\/]|$)|\.env/i.test(args.pattern||''))return '搜索不包含记录或认证目录';
    return null;
  }
  if(writes.has(name))return target(job,args.file_path??args.notebook_path,true);
  if(shells.has(name)){
    if(job.kind!=='edit')return '只读任务没有终端权限';
    if(typeof args.command!=='string'||!args.command.trim())return '命令无效';
    if(/\bgit\s+(?:[^;\r\n]*?\s)?(?:push|commit|merge|worktree|switch|checkout|reset|clean)\b/i.test(args.command))return '执行任务不修改 Git 历史或向远端发布';
    if(args.run_in_background)return '本任务终端须前台完成并返回退出结果';
    if(args.dangerouslyDisableSandbox||args.disableSandbox)return '不能批准原生沙箱绕过';
    if(/\.model-desk(?![-\w.])|(?:^|[\\/\s"'])\.env(?:[.\s"']|$)|(?:auth|credential)[-_]?(?:token|store|file)/i.test(args.command))return '终端不能访问工作台记录或认证数据';
    if(args.cwd||args.workdir){const r=target(job,args.cwd||args.workdir,false,true);if(r)return r;}
    return policy.decide(name==='PowerShell'?'pwsh':'bash',args,{root:job.project})||null;
  }
  if(web.has(name)){
    if(name==='WebFetch'){try{const u=new URL(args.url);if(!['http:','https:'].includes(u.protocol)||/^(?:localhost|127\.|0\.|10\.|192\.168\.|169\.254\.|\[|.*\.local$)/i.test(u.hostname)||/^172\.(?:1[6-9]|2[0-9]|3[01])\./.test(u.hostname))return '联网读取不能访问本机控制器或内网账户接口';}catch(_){return '网址无效';}}
    return null;
  }
  return '未经适配的原生工具';
}
function main(file,input){
  const job=JSON.parse(fs.readFileSync(file,'utf8')),run=path.dirname(file);
  const revision=hash(Buffer.concat([fs.readFileSync(__filename),fs.readFileSync(path.join(__dirname,'record-policy.cjs'))]));
  if(job.schema!==1||job.revision!==revision||job.mode!=='native-development-v1'||path.resolve(run)!==path.resolve(job.project,'.model-desk','runs',job.task_id))throw Error('工具任务身份校验失败');
  if(!input||typeof input.session_id!=='string'||!input.session_id)throw Error('缺少父会话身份');
  const proofFile=path.join(run,'workbuddy-native-proof.json'),flag=path.join(run,'workbuddy-native-denied.flag');
  const proof={...Object.fromEntries(['task_id','project','nonce','revision','mode'].map(k=>[k,job[k]])),session_id:input.session_id,probe:'ok'};
  if(input.hook_event_name==='SessionStart'){
    if(fs.existsSync(proofFile)&&JSON.parse(fs.readFileSync(proofFile,'utf8')).session_id!==input.session_id)throw Error('不能换绑原会话');
    if(policy.decide(policy.PROBE_TOOL,{}, {root:job.project})!==policy.PROBE_REASON)throw Error('记录保护探针失败');
    fs.writeFileSync(proofFile,JSON.stringify(proof));return {};
  }
  const old=JSON.parse(fs.readFileSync(proofFile,'utf8'));
  if(Object.keys(proof).some(k=>proof[k]!==old[k]))throw Error('原生工具父会话不匹配');
  let reason=null;
  if(input.hook_event_name==='PreToolUse')reason=fs.existsSync(flag)?'本轮工具已暂停，请报告主脑，不要换工具重试':decide(job,input);
  const response=input.tool_response;
  const raw=response?.rawResponse||response;
  const commandHash=typeof input.tool_input?.command==='string'?hash(input.tool_input.command):null;
  const row={event:input.hook_event_name,session_id:input.session_id,nonce:job.nonce,call_id:input.tool_use_id||input.call_id||null,tool:input.tool_name,
    input_hash:hash(JSON.stringify(stable(input.tool_input||{}))),allowed:!reason,
    command_hash:commandHash,exit_code:Number.isInteger(raw?.exitCode)?raw.exitCode:null,
    ok:input.hook_event_name==='PostToolUse'&&!(raw?.is_error||raw?.isError||raw?.interrupted||raw?.sandboxDenied||response?.error),at:new Date().toISOString()};
  fs.appendFileSync(path.join(run,'workbuddy-native-events.jsonl'),JSON.stringify(row)+'\n');
  if(reason){fs.writeFileSync(flag,'denied');return {hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',permissionDecisionReason:reason}};}
  if(input.hook_event_name==='PreToolUse'&&input.tool_name==='Grep'&&!fs.statSync(path.resolve(job.project,input.tool_input.path||'.')).isFile()){
    // Restrict project-wide search to non-record/non-auth files. This read-only
    // rewrite grants no terminal/write authority. Use a file path for narrower
    // searches; the negative glob intentionally takes precedence over globs.
    return {hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'allow',updatedInput:{...input.tool_input,glob:'!{**/.model-desk/**,**/.git/**,**/.env,**/.env.*,**/.codebuddy/**,**/.workbuddy/**,**/.codex/**,**/.local-worker/{tasks,logs}/**,**/.local-worker/{models,settings,panel-runtime,workbuddy}.json}'}}};
  }
  if(input.hook_event_name==='PostToolUse'&&input.tool_name==='Glob'&&Array.isArray(response?.content)){
    return {hookSpecificOutput:{hookEventName:'PostToolUse',updatedToolOutput:{...response,content:response.content.filter(p=>typeof p==='string'&&!target(job,p))}}};
  }
  return {}; // Native permission/sandbox rules still run for other tools.

}
module.exports={decide,target,main};
if(require.main===module){
  try{const input=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(main(process.argv[2],input)));}
  catch(_){process.stdout.write(JSON.stringify({hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',permissionDecisionReason:'原生工具保护校验失败，请报告主脑'}}));process.exitCode=2;}
}
