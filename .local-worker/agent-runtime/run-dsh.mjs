import fs from 'node:fs';
import path from 'node:path';
import {createAcpRuntime, createAgentRegistry, createFileSessionStore} from 'acpx/runtime';

const job=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
const env={...process.env,ELECTRON_RUN_AS_NODE:'1'};
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const normPath=value=>String(value||'').replace(/[\\/]+$/,'').replace(/\//g,'\\').toLowerCase();
// Workbench-record write protection: the mounted plugin writes this per-run
// proof at DSH load time.  A turn is never started without it, so an old or
// unknown session can never be resumed silently without the guard in place.
const recordProof=typeof job.record_proof==='string'&&job.record_proof?job.record_proof:null;
const recordRevision=typeof job.record_revision==='string'?job.record_revision:'';
const recordRoot=typeof job.record_root==='string'&&job.record_root?job.record_root:job.cwd;
if(recordProof){try{fs.rmSync(recordProof,{force:true});}catch(error){}}
const readRecordProof=async()=>{
  for(let attempt=0;attempt<60;attempt+=1){
    try{return JSON.parse(fs.readFileSync(recordProof,'utf8'));}catch(error){}
    await sleep(100);
  }
  return null;
};
const verifyRecordProtection=async sessionId=>{
  if(!recordProof)throw new Error('任务缺少工作台记录写入保护配置，已拒绝执行；请创建新任务');
  const data=await readRecordProof();
  if(!data)throw new Error('工作台记录写入保护未在 DSH 中装载，已拒绝执行；请创建新任务');
  if(data.source!=='model-desk-record-protection'||data.probe!=='ok'||data.scope!=='global')throw new Error('工作台记录写入保护未在全局工具层生效，已拒绝执行');
  if(String(data.revision||'')!==recordRevision)throw new Error('工作台记录写入保护内容与派发记录不一致，已拒绝执行；请创建新任务');
  if(normPath(data.root)!==normPath(recordRoot))throw new Error('工作台记录写入保护的项目目录与任务不一致，已拒绝执行');
  if(!sessionId||data.session_id!==sessionId)throw new Error('工作台记录写入保护未绑定当前父会话，已拒绝执行');
  emit({type:'record_protection',scope:'global',revision:recordRevision,root:recordRoot});
};
const runtime=createAcpRuntime({
  cwd:job.cwd,
  agentProcessEnv:env,
  agentRegistry:createAgentRegistry({overrides:{dsh:[job.executable,'--expose-internals',job.cli,'--profile','acp','--patch',job.patch]}}),
  sessionStore:createFileSessionStore({stateDir:job.state_dir}),
  permissionMode:job.kind==='edit'?'approve-all':'approve-reads',
  nonInteractivePermissions:'deny', fs:false, terminal:false,
  ...(job.timeout>0?{timeoutMs:job.timeout*1000}:{}),
  processLifecycle:{onSpawned:process=>emit({type:'process',pid:process.pid})},
  onPermissionRequest:async request=>{
    const tool=request.raw.toolCall;
    emit({type:'permission',kind:request.inferredKind,title:tool?.title});
    const readTool=['read','glob','grep','list'].includes(tool?.title);
    if(job.kind!=='edit' && !readTool && !['read','search','think'].includes(request.inferredKind))return {outcome:'reject_once'};
    // DSH edit jobs use the user's full-access policy; chat jobs remain read-only.
    return {outcome:'allow_once'};
  }
});
// Controlled subagent observation: the DSH-side bridge appends lifecycle edges
// to this ledger.  We only stream what the executor recorded; nothing here
// infers a child from the model's text.
const ledger=typeof job.subagent_ledger==='string'&&job.subagent_ledger?job.subagent_ledger:null;
let ledgerOffset=0,ledgerPartial='';
const drainLedger=()=>{
  if(!ledger||!fs.existsSync(ledger))return;
  let text;
  try{
    const size=fs.statSync(ledger).size;
    if(size<=ledgerOffset)return;
    const fd=fs.openSync(ledger,'r');
    const buffer=Buffer.alloc(size-ledgerOffset);
    fs.readSync(fd,buffer,0,buffer.length,ledgerOffset);
    fs.closeSync(fd);
    ledgerOffset=size;
    text=buffer.toString('utf8');
  }catch(error){return;}
  const parts=(ledgerPartial+text).split('\n');
  ledgerPartial=parts.pop()??'';
  for(const line of parts){
    const trimmed=line.trim();
    if(!trimmed)continue;
    try{emit({type:'subagent',event:JSON.parse(trimmed)});}catch(error){}
  }
};
const writeParent=sessionId=>{
  if(!ledger)return;
  try{
    fs.appendFileSync(ledger,JSON.stringify({schema:1,source:'run-dsh.mjs',type:'parent',
      at:new Date().toISOString(),task_id:job.task_id,parent_session:sessionId??null})+'\n');
    ledgerOffset=fs.statSync(ledger).size;
  }catch(error){}
};
let timer,turn,handle,stopping=false;
try{
  emit({type:'phase',text:'连接 DeepSeek Harness'});
  handle=await runtime.ensureSession({sessionKey:job.session_key,agent:'dsh',mode:'persistent',cwd:job.cwd});
  const status=await runtime.getStatus({handle});
  const sessionId=handle.backendSessionId||handle.agentSessionId||status.backendSessionId;
  emit({type:'session',session_key:job.session_key,session_id:sessionId,
        acpx_record_id:handle.acpxRecordId,models:status.models});
  writeParent(sessionId);
  const modelIds=status.models?.availableModelIds||[];
  const expected=JSON.stringify(['desk-worker',job.model]);
  if(!modelIds.includes(expected)||status.models?.currentModelId!==expected)throw new Error('DSH 未选择指定模型，停止派发');
  await verifyRecordProtection(sessionId);
  if(fs.existsSync(job.cancel_file))throw new Error('任务已取消');
  turn=runtime.startTurn({handle,text:fs.readFileSync(job.prompt_file,'utf8'),mode:'prompt',requestId:job.task_id,...(job.timeout>0?{timeoutMs:job.timeout*1000}:{})});
  timer=setInterval(()=>{
    drainLedger();
    if(!stopping&&fs.existsSync(job.cancel_file)){stopping=true;turn.cancel({reason:'用户停止任务'}).catch(error=>emit({type:'cancel_error',text:error.message}));}
  },200);
  for await(const event of turn.events)emit(event);
  const result=await turn.result;
  drainLedger();
  emit({type:'result',result});
  if(result.status==='failed')process.exitCode=1;
}catch(error){
  drainLedger();
  emit({type:'error',text:error.message,code:error.code});process.exitCode=1;
}finally{
  clearInterval(timer);
  drainLedger();
  await runtime.shutdown();
}
