'use strict';
// Native hooks run before the tool body, including read tools without approvals.
// This is an argument guard, not an OS sandbox for arbitrary programs/scripts.
const fs=require('fs'),path=require('path'),crypto=require('crypto');
const policy=require('./record-policy.cjs');
const access=require('./endpoint-access.cjs');
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const stable=value=>Array.isArray(value)?value.map(stable):value&&typeof value==='object'?Object.fromEntries(Object.keys(value).sort().map(k=>[k,stable(value[k])])):value;
const reads=new Set(['Read','Glob','Grep','NotebookRead']);
const writes=new Set(['Write','Edit','MultiEdit','NotebookEdit']);
const shells=new Set(['Bash','PowerShell']);
const web=new Set(['WebFetch','WebSearch']);
// Shared directory-search exclusion glob for native Grep rewrites: the
// superset of both adapters' private-path lists, so every native directory
// search hides the same records, credentials and host configuration. This is
// read-only result narrowing; it grants no write or terminal authority.
const SEARCH_EXCLUSION_GLOB='!{**/.model-desk/**,**/.git/**,**/.zcode/**,**/.env,**/.env.*,**/.codebuddy/**,**/.workbuddy/**,**/.codex/**,**/.dsh/**,**/{auth,credentials,tokens,secrets}.json,**/.local-worker/{tasks,logs}/**,**/.local-worker/{models,settings,workflow,execution-endpoints,panel-runtime,workbuddy,zcode,zcode-catalog,zcode-usage,project-index,budget-ledger}.json}';
function searchExclusionGlob(job,base){
  const contract=policy.taskPolicy(job);
  if(!contract||contract.schema<2)return SEARCH_EXCLUSION_GLOB;
  const patterns=['**/.model-desk/**','**/.git/**','**/.env','**/.env.*'];
  const roots=[...contract.deny_paths,...(job.protected_roots||[]),
    ...['.codebuddy','.workbuddy','.codex','.dsh','.zcode'].map(n=>path.resolve(job.project,n))];
  for(const root of roots){
    const relative=path.relative(base,root);
    if(!relative||relative==='..'||relative.startsWith('..'+path.sep)||path.isAbsolute(relative))continue;
    // Native tools normally search from the session project. Cover both that
    // relative form and absolute search arguments without a basename wildcard.
    for(const value of [relative,path.relative(job.project,root),path.resolve(root)]){
      const glob=value.replace(/\\/g,'/').replace(/([*?\[\]{}()!])/g,'[$1]');
      patterns.push(glob,glob+'/**');
    }
  }
  return '!{'+[...new Set(patterns)].join(',')+'}';
}
function inside(root,p){const r=path.relative(policy.realpathDeep(root),policy.realpathDeep(p));return r!== '..'&&!r.startsWith('..'+path.sep)&&!path.isAbsolute(r);}
// Fixed denial codes for the native event streams, shared by both adapters:
// the ZCode adapter records its event rows through this function instead of
// keeping its own copy. The decisions themselves stay inside the shared guard.
const {denialCode,denialRule}=policy;
function unverifiedCleanup(command){
  return (policy.commandGroups(command)||[command]).some(group=>{
    const cleanup=/\b(?:rm|rmdir|remove-item|rd)\b/i.test(group);
    const recursive=group.split(/\s+/).some(token=>{
      const v=token.toLowerCase();
      return ['-recurse','--recursive','/s'].includes(v)||v.startsWith('-recurse:')
        || (/^-[a-z]{1,3}$/.test(v)&&v.includes('r'));
    });
    return cleanup&&recursive&&/[\$%*?`]/.test(group);
  });
}
// A task copy is a sibling named <project>-task-workspaces, not a child of
// the protected original project. Match complete path boundaries rather than
// treating every occurrence of the original name as access to that directory.
function referencesProtectedRoot(command,root){
  const text=command.replace(/\\/g,'/').toLowerCase();
  const value=root.replace(/\\/g,'/').replace(/\/+$/,'').toLowerCase();
  if(!value)return true;
  let start=text.indexOf(value);
  while(start!==-1){
    const suffix=text.slice(start+value.length);
    if(!suffix||/^[\/\s"'`;|&<>():]/.test(suffix)||/^\.+(?=$|[\/\s"'`;|&<>():])/.test(suffix))return true;
    start=text.indexOf(value,start+1);
  }
  return false;
}
function commandWithoutSearchExclusions(command){
  // Only bare ripgrep groups, with quoted literal negative globs.
  // This is analysis only: the native terminal still receives the original
  // command, including its exclusions. Other commands/expansions stay opaque.
  const groups=policy.commandGroups(command);
  if(!groups)return command;
  let result=command;
  for(const group of groups){
    if(!/^\s*(?:rg|ripgrep)(?:\.exe)?\s/i.test(group))continue;
    const filtered=group.replace(/(^|\s)(?:--glob|-g)(?:\s+|=)(['"])(![A-Za-z0-9_./\\!*?{},\[\]-]+)\2(?=\s|$)/g,'$1');
    // Keep the original separators/conditional directory semantics. Unmatched
    // groups remain opaque rather than reconstructing a shell command.
    if(filtered!==group)result=result.replace(group,filtered);
  }
  return result;
}
// A quoted literal query is data, not a filesystem target. Only recognize
// bounded searches over existing ordinary files in this task's own project.
// Unknown flags, expansions, redirects, location changes and other verbs stay
// opaque. The original native command is never rewritten or executed here.
function literalSourceSearch(job,args){
  const command=args.command;
  if(typeof command!=='string'||command.length>16384||/[`$<>\r\n]/.test(command.trim()))return null;
  const groups=policy.commandGroups(command.trim());
  if(!groups||!groups.length||groups.length>8)return null;
  const safeFlags=new Set(['-n','--line-number','-F','--fixed-strings','-i','--ignore-case',
    '-H','--with-filename','-l','--files-with-matches','--no-heading','--no-messages','--color=never']);
  const rewrites=[];
  for(const group of groups){
    const tokens=[];let i=0;
    while(i<group.length){
      if(/\s/.test(group[i])){i++;continue;}
      const start=i,quote=group[i]==="'"||group[i]==='"'?group[i++]:null;
      if(quote){while(i<group.length&&group[i]!==quote)i++;if(i===group.length)return null;i++;
        if(i<group.length&&!/\s/.test(group[i]))return null;
      }else{while(i<group.length&&!/\s/.test(group[i])){if(/["']/.test(group[i]))return null;i++;}}
      const raw=group.slice(start,i);tokens.push({raw,value:quote?raw.slice(1,-1):raw,quoted:!!quote});
      if(tokens.length>64)return null;
    }
    if(!tokens.length||tokens[0].quoted||!/^(?:rg|ripgrep|grep)(?:\.exe)?$/i.test(tokens[0].value))return null;
    let pattern=null,ended=false;const files=[];
    for(let j=1;j<tokens.length;j++){
      const token=tokens[j];
      if(!ended&&token.value==='--'){ended=true;continue;}
      if(!ended&&safeFlags.has(token.value))continue;
      if(!ended&&['-e','--regexp'].includes(token.value)){
        if(pattern||!tokens[j+1]?.quoted)return null;pattern=tokens[++j];continue;
      }
      if(!ended&&token.value.startsWith('-'))return null;
      if(!pattern){if(!token.quoted)return null;pattern=token;}
      else files.push(token);
    }
    if(!pattern||!files.length)return null;
    const base=path.resolve(job.project,args.workdir||args.cwd||'.');
    for(const file of files){
      if(!file.quoted&&!/^[A-Za-z0-9_./\\:-]+$/.test(file.value))return null;
      const p=path.resolve(base,file.value);
      try{if(!inside(job.project,p)||target(job,p)||!fs.statSync(p).isFile())return null;}
      catch(_){return null;}
    }
    rewrites.push([group,group.replace(pattern.raw,"'MODEL_DESK_LITERAL_QUERY'")]);
  }
  let result=command;
  for(const [before,after] of rewrites)result=result.replace(before,after);
  return result;
}
// The declared task scratch directory: a workbench constant that prepare()
// writes into the job (isolated edit tasks only) and the Python loader pins
// to the exact same fixed name, so this allowance cannot be steered to a
// protected path.  Anything but the exact convention declares nothing.
const TASK_TEMP_DIR='.modeldesk-task-tmp';
function declaredTempRoot(job){
  if(!job||job.temp_dir!==TASK_TEMP_DIR)return null;
  return policy.normalizePath(path.resolve(job.project,TASK_TEMP_DIR));
}
function lexicallyInside(root,p){
  const r=path.relative(root,p);
  return r!=='..'&&!r.startsWith('..'+path.sep)&&!path.isAbsolute(r);
}
function target(job,value,write=false,browse=false){
  if(typeof value!=='string'||!value.trim())return '文件路径无效';
  const declared=(policy.taskPolicy(job)?.schema||0)>=2;
  const protectedReason=policy.protectedArgumentsReason({...job,root:job.project},write?'Write':'Read',{file_path:value});
  if(protectedReason)return protectedReason;
  const p=path.resolve(job.project,value),real=policy.realpathDeep(p),root=policy.realpathDeep(job.project);
  const relative=path.relative(root,real);
  if(policy.hasProtectedComponent(p)||policy.hasProtectedComponent(real)||[p,real].some(n=>/(^|[\\/])(?:\.git|\.env(?:\.[^\\/]+)?)(?:[\\/]|$)/i.test(n)))return '工作台记录、Git 元数据或认证配置不作为执行目标';
  if(fs.existsSync(p)&&fs.statSync(p).isFile()&&fs.statSync(p).nlink>1)return '文件存在硬链接别名';
  if(!declared&&[p,real].some(n=>/(?:^|[\\/])(?:\.codebuddy|\.workbuddy|\.codex|\.dsh|\.zcode)(?:[\\/]|$)|(?:^|[\\/])(?:auth|credentials|tokens?|secrets?)\.json$/i.test(n)))return '认证存储不作为执行目标';
  if((job.protected_roots||[]).some(n=>inside(n,p)))return '原生配置目录不作为执行目标';
  if(!declared&&[p,real].some(n=>/[\\/]\.local-worker[\\/](?:models|settings|workflow|execution-endpoints|panel-runtime|workbuddy(?:-access|-catalog)?|project-index|budget-ledger)\.json$/i.test(n)))return '运行账户配置不作为执行目标';
  if(job.access_mode!=='full'&&(relative.startsWith('..'+path.sep)||relative==='..'||path.isAbsolute(relative)))return '文件不属于本任务项目';
  const own=[...job.files,...job.readonly_files];
  const ownsFile=own.some(n=>policy.normalizePath(path.resolve(job.project,n))===policy.normalizePath(p));
  if(write&&job.readonly_files.some(n=>policy.normalizePath(path.resolve(job.project,n))===policy.normalizePath(p))&&!job.files.some(n=>policy.normalizePath(path.resolve(job.project,n))===policy.normalizePath(p)))return '任务只读上下文不可修改';
  const ownsDirectory=browse&&own.some(n=>policy.normalizePath(path.dirname(path.resolve(job.project,n)))===policy.normalizePath(p));
  if(!declared&&/[\\/]\.local-worker[\\/](?:tasks|logs)[\\/]/i.test(p)&&!ownsFile&&!ownsDirectory)return '不读取其他任务或会话材料';
  if(write&&policy.taskPolicy(job))return policy.taskWriteReason({...job,root:job.project},value)||null;
  // Declared scratch material: reasonable test and temp files inside the task
  // copy are task conventions, not undeclared source edits.  Every protected
  // check above still ran first; only the deliverable-declaration rule yields.
  const temp=declaredTempRoot(job);
  if(write&&temp&&lexicallyInside(temp,policy.normalizePath(p))){
    // Both the lexical location and the resolved target must be scratch.
    // Resolve on every call: a link swapped in after prepare must not turn
    // ordinary source files into undeclared temporary write targets.
    if(policy.normalizePath(policy.realpathDeep(temp))!==temp||!lexicallyInside(temp,policy.normalizePath(real)))
      return '任务临时目录存在别名或越界';
    return null;
  }
  if(write&&job.access_mode!=='full'&&!job.files.some(name=>policy.normalizePath(path.resolve(job.project,name))===policy.normalizePath(p)))return '文件未列入本任务可写范围';
  return null;
}
function decide(job,input){
  const name=input.tool_name,args=input.tool_input;
  policy.taskPolicy(job);
  if(!job.tools.includes(name)||!args||typeof args!=='object'||Array.isArray(args))return '本任务未授权此原生工具';
  const direct=policy.directTerminalPolicy(job);
  const searchAnalysis=!direct&&shells.has(name)?literalSourceSearch(job,args):null;
  const checkedArgs=shells.has(name)?{...args,command:policy.terminalAnalysis(searchAnalysis??args.command)}:args;
  const isolated=access.isolationReason({...job,root:job.project},name,direct?args:checkedArgs);
  if(isolated)return isolated;
  if(reads.has(name)){
    const p=args.file_path??args.notebook_path??args.path??'.';
    const reason=target(job,p,false,name==='Glob'||name==='Grep');if(reason)return reason;
    if(name==='Grep'&&(job.protected_roots||[]).some(root=>inside(path.resolve(job.project,p),root)))return '目录检索范围包含原生配置；请选择更具体的文件';
    // Search defaults exclude records even when the root itself is the target.
    if(name==='Glob'&&/\.model-desk|\.git(?:[\\/]|$)|\.env/i.test(args.pattern||''))return '搜索不包含记录或认证目录';
    return null;
  }
  if(writes.has(name))return target(job,args.file_path??args.notebook_path,true);
  if(shells.has(name)){
    if(job.kind!=='edit')return '只读任务没有终端权限';
    if(typeof args.command!=='string'||!args.command.trim())return '命令无效';
    if(direct){
      // Structured options are authority, command text is not. No whole-text
      // regex, cleanup guess, location-overflow refusal or payload rewrite.
      if(args.run_in_background)return '本任务终端须前台完成并返回退出结果';
      if(args.dangerouslyDisableSandbox||args.disableSandbox)return '不能批准原生沙箱绕过';
      const reason=policy.protectedArgumentsReason({...job,root:job.project},name,args);
      if(reason)return reason;
      if(args.cwd||args.workdir){const r=target(job,args.cwd||args.workdir,false,true);if(r)return r;}
      for(const row of policy.directTerminalTargets(args.command,path.resolve(job.project,args.workdir||args.cwd||'.'))){
        if(row.gitMutation)return '执行任务不修改 Git 历史或向远端发布';
        const value=/^~(?:[\\/]|$)/.test(row.value)?job.task_policy.home+row.value.slice(1):row.value;
        const r=target(job,policy.resolveAgainst(row.base,value),row.write);if(r)return r;
      }
      return null;
    }
    const analysisCommand=commandWithoutSearchExclusions(checkedArgs.command);
    const declared=(policy.taskPolicy(job)?.schema||0)>=2;
    const protectedReason=policy.protectedArgumentsReason({...job,root:job.project},name,{...args,command:analysisCommand});
    if(protectedReason)return protectedReason;
    if(!declared&&(job.protected_roots||[]).some(root=>referencesProtectedRoot(analysisCommand,root)))return '终端不能访问原生配置目录';
    if(/\bgit\s+(?:[^;\r\n]*?\s)?(?:push|commit|merge|worktree|switch|checkout|reset|clean)\b/i.test(analysisCommand))return '执行任务不修改 Git 历史或向远端发布';
    if(args.run_in_background)return '本任务终端须前台完成并返回退出结果';
    if(args.dangerouslyDisableSandbox||args.disableSandbox)return '不能批准原生沙箱绕过';
    if((declared?/\.model-desk(?![-\w.])|(?:^|[\\/\s"'])\.env(?:[.\s"']|$)/i
      :/\.(?:model-desk|zcode)(?![-\w.])|(?:^|[\\/\s"'])\.env(?:[.\s"']|$)/i).test(analysisCommand)
      ||(!declared&&/(?:auth|credential)[-_]?(?:token|store|file)/i.test(analysisCommand)))return '终端不能访问工作台记录或认证数据';
    if(unverifiedCleanup(analysisCommand))return '递归清理目标包含未核实的变量或通配符，请保留临时文件并报告主脑';
    if(args.cwd||args.workdir){const r=target(job,args.cwd||args.workdir,false,true);if(r)return r;}
    // The native adapter has stricter read boundaries than DSH/Pi. Validate
    // literal cd targets too, before shared record-write analysis consumes them.
    const facts=policy.terminalFacts(analysisCommand,path.resolve(job.project,args.workdir||args.cwd||'.'));
    for(const base of facts.locations){const r=target(job,base,false,true);if(r)return r;}
    if(facts.overflow)return '复合命令过于复杂，无法核实工作目录；请报告主脑。';
    return policy.decide(name==='PowerShell'?'pwsh':'bash',{...args,command:analysisCommand},{root:job.project})||null;
  }
  if(web.has(name)){
    if(name==='WebFetch'){try{const u=new URL(args.url);if(!['http:','https:'].includes(u.protocol)||/^(?:localhost|127\.|0\.|10\.|192\.168\.|169\.254\.|\[|.*\.local$)/i.test(u.hostname)||/^172\.(?:1[6-9]|2[0-9]|3[01])\./.test(u.hostname))return '联网读取不能访问本机控制器或内网账户接口';}catch(_){return '网址无效';}}
    return null;
  }
  return '未经适配的原生工具';
}
function validateBinding(run,job){
  const record=path.resolve(job.record_project||job.project),execution=path.resolve(job.project);
  if(path.resolve(run)!==path.resolve(record,'.model-desk','runs',job.task_id))throw Error('工具任务记录身份校验失败');
  if(record!==execution){
    const state=JSON.parse(fs.readFileSync(path.join(run,'task.json'),'utf8')),plan=state.workspace;
    const expected=path.resolve(record,'..',path.basename(record)+'-task-workspaces',job.task_id);
    if(!fs.existsSync(execution)||!fs.statSync(execution).isDirectory()||
       !(job.protected_roots||[]).some(root=>path.resolve(root)===record)||
       state.id!==job.task_id||path.resolve(state.project)!==record||!plan||plan.created!==true||
       path.resolve(plan.path)!==execution||execution!==expected||plan.branch!=='modeldesk-task/'+job.task_id||
       policy.normalizePath(policy.realpathDeep(execution))!==policy.normalizePath(execution))throw Error('工具执行工作区绑定校验失败');
    const metadata=root=>{
      const dot=path.join(root,'.git');
      const gitdir=fs.statSync(dot).isDirectory()?dot:path.resolve(root,fs.readFileSync(dot,'utf8').trim().replace(/^gitdir:\s*/,''));
      const commonFile=path.join(gitdir,'commondir');
      const common=fs.existsSync(commonFile)?path.resolve(gitdir,fs.readFileSync(commonFile,'utf8').trim()):gitdir;
      return {gitdir,common:policy.normalizePath(fs.realpathSync(common))};
    };
    const owner=metadata(record),copy=metadata(execution);
    if(owner.common!==copy.common||fs.readFileSync(path.join(copy.gitdir,'HEAD'),'utf8').trim()!=='ref: refs/heads/'+plan.branch)throw Error('工具执行工作区 Git 身份校验失败');
  }
}
function main(file,input){
  const jobBytes=fs.readFileSync(file),job=JSON.parse(jobBytes),run=path.dirname(file);
  const revision=hash(Buffer.concat([fs.readFileSync(__filename),fs.readFileSync(path.join(__dirname,'record-policy.cjs')),fs.readFileSync(path.join(__dirname,'endpoint-access.cjs'))]));
  // Same split as the ZCode adapter: job.guard_revision pins these guard
  // modules, while job.revision is the controller's full protection version
  // (guard files plus the task-visible plan, including the scratch-declaration
  // contract) and is verified by the Python loader.
  if(job.schema!==1||job.guard_revision!==revision||typeof job.revision!=='string'||job.mode!=='native-development-v1'||!['project','full'].includes(job.access_mode)||!Array.isArray(job.protected_roots)||job.protected_roots.some(p=>typeof p!=='string'||!path.isAbsolute(p)))throw Error('工具任务身份校验失败');
  policy.taskPolicy(job);
  validateBinding(run,job);
  if(!input||typeof input.session_id!=='string'||!input.session_id)throw Error('缺少父会话身份');
  const proofFile=path.join(run,'workbuddy-native-proof.json'),flag=path.join(run,'workbuddy-native-denied.flag');
  const proof={...Object.fromEntries(['task_id','project','nonce','revision','mode','access_mode'].map(k=>[k,job[k]])),job_hash:hash(jobBytes),session_id:input.session_id,probe:'ok'};
  if(job.record_project)proof.record_project=job.record_project;
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
    input_hash:hash(JSON.stringify(stable(input.tool_input||{}))),allowed:!reason,reason_code:reason?denialCode(reason):null,
    denial_rule:denialRule(reason),
    command_hash:commandHash,analysis_context:shells.has(input.tool_name)?{cwd_source:input.tool_input?.workdir?'workdir':input.tool_input?.cwd?'cwd':'project',leading_location:!!policy.leadingLocation(input.tool_input?.command||'',job.project)}:null,exit_code:Number.isInteger(raw?.exitCode)?raw.exitCode:null,
    ok:input.hook_event_name==='PostToolUse'&&!(raw?.is_error||raw?.isError||raw?.interrupted||raw?.sandboxDenied||response?.error)
      &&!(Number.isInteger(raw?.exitCode)&&raw.exitCode!==0),at:new Date().toISOString()};
  fs.appendFileSync(path.join(run,'workbuddy-native-events.jsonl'),JSON.stringify(row)+'\n');
  if(reason){fs.writeFileSync(flag,'denied');return {hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',permissionDecisionReason:reason}};}
  let searchDirectory=false;
  if(input.hook_event_name==='PreToolUse'&&input.tool_name==='Grep'){
    try{searchDirectory=fs.statSync(path.resolve(job.project,input.tool_input.path||'.')).isDirectory();}
    catch(error){
      // A missing ordinary target belongs to the native tool, which can report
      // it and let the session correct its input. Other IO errors stay unknown.
      // decide() above has already checked protected and resolved targets.
      if(!['ENOENT','ENOTDIR'].includes(error.code))throw error;
    }
  }
  if(searchDirectory){
    // Restrict project-wide search to non-record/non-auth files. This read-only
    // rewrite grants no terminal/write authority. Use a file path for narrower
    // searches; the negative glob intentionally takes precedence over globs.
    return {hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'allow',updatedInput:{...input.tool_input,
      glob:searchExclusionGlob(job,path.resolve(job.project,input.tool_input.path||'.'))}}};
  }
  if(input.hook_event_name==='PostToolUse'&&input.tool_name==='Glob'&&Array.isArray(response?.content)){
    return {hookSpecificOutput:{hookEventName:'PostToolUse',updatedToolOutput:{...response,content:response.content.filter(p=>typeof p==='string'&&!target(job,p))}}};
  }
  return {}; // Native permission/sandbox rules still run for other tools.

}
module.exports={decide,target,main,denialRule,denialCode,SEARCH_EXCLUSION_GLOB,searchExclusionGlob,validateBinding};
if(require.main===module){
  try{const input=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(main(process.argv[2],input)));}
  catch(_){process.stdout.write(JSON.stringify({hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',permissionDecisionReason:'原生工具保护校验失败，请报告主脑'}}));process.exitCode=2;}
}
