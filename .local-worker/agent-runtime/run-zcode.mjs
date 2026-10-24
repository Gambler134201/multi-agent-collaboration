// Drive only task-owned native sessions. Account auth stays inside ZCode.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const job=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
const pages=await(await fetch(`http://127.0.0.1:${job.port}/json/list`,{signal:AbortSignal.timeout(5000)})).json();
const expected=pathToFileURL(path.join(job.home,'resources/app.asar/out/renderer/index.html')).href;
const pagesForApp=pages.filter(p=>p.type==='page'&&p.url.split('?')[0].toLowerCase()===expected.toLowerCase());
if(pagesForApp.length!==1)throw Error('ZCode 主窗口未唯一就绪；未请求模型');
const endpoint=new URL(pagesForApp[0].webSocketDebuggerUrl);
if(!['127.0.0.1','localhost'].includes(endpoint.hostname)||Number(endpoint.port)!==job.port)throw Error('非本机 ZCode 桥接地址');
const ws=new WebSocket(endpoint.href),pending=new Map();let seq=0;
await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
ws.onmessage=e=>{const m=JSON.parse(e.data),entry=pending.get(m.id);if(!entry)return;pending.delete(m.id);clearTimeout(entry.timer);m.error?entry.reject(Error('ZCode 调试传输错误')):entry.resolve(m.result);};
ws.onclose=()=>{for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('ZCode 原生通道断开；结果未知，未重试'));}pending.clear();};
async function evaluate(expression){
  const id=++seq;
  const result=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>{pending.delete(id);reject(Error('ZCode 原生操作回执未知，未重试'));},60000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method:'Runtime.evaluate',params:{expression,awaitPromise:true,returnByValue:true}}));});
  if(result.exceptionDetails)throw Error('ZCode 原生操作失败：'+String(result.exceptionDetails.exception?.description||result.exceptionDetails.text).slice(0,600));
  return result.result?.value;
}
const bootstrap=`(()=>{
  const root=document.getElementById('root');if(!root)throw Error('ZCode renderer missing');
  let first=root[Object.keys(root).find(k=>k.startsWith('__reactContainer$'))];while(first?.return)first=first.return;
  const queue=[first],seen=new Set();let services;
  while(queue.length&&seen.size<20000){const f=queue.pop();if(!f||seen.has(f))continue;seen.add(f);const s=f.memoizedProps?.services;
    if(s?.zcodeAgentService&&s?.modelSelectionService){services=s;break;}queue.push(f.child,f.sibling);}
  if(!services)throw Error('ZCode native services not ready');
  window.__modelDeskZCode={agent:services.zcodeAgentService,models:services.modelSelectionService,usage:services.usageStatsService};
  window.__modelDeskZCodeJobs??=new Map();return true;})()`;
let scope=null,created=false,key=null,unsubscribed=false;
try{
  await evaluate(bootstrap);
  if(job.action==='quota'){
    if(!['account:zai-start-plan','account:bigmodel-start-plan'].includes(job.provider))throw Error('此渠道尚未适配原生权益查询');
    const result=await evaluate(`(async()=>{const p=${JSON.stringify(job.provider)},s=window.__modelDeskZCode.usage;if(!s?.getEntitlementSnapshot)throw Error('Native entitlement service unavailable');
      const v=await s.getEntitlementSnapshot({includeSubscription:true,preferredProviderId:p,accountAccess:{type:'zhipu-account',family:p==='account:zai-start-plan'?'zai':'bigmodel',planKind:'start-plan'},allowDisabledPreferredProvider:true,requirePreferredProvider:true,allowEnvApiKey:false});
      if(v.provider?.id!==p||v.authenticated!==true)throw Error('Native entitlement identity unavailable');
      const number=x=>typeof x==='number'&&Number.isFinite(x)?x:null;
      return {provider:p,generated_at:number(v.generatedAt),unavailable_reason:typeof v.unavailableReason==='string'?v.unavailableReason.slice(0,80):null,
        limits:(v.quota?.limits||[]).slice(0,40).map(l=>({label:String(l.usageDetails?.[0]?.displayName||l.type||'账户额度').slice(0,120),unit:String(l.unitType||l.meter||'units').slice(0,40),remaining:number(l.remaining),total:number(l.number),used:number(l.usage),expires_at:number(l.nextResetTime),models:(l.usageDetails||[]).map(d=>String(d.modelCode).slice(0,100)).slice(0,30)}))};})()`);
    emit({type:'quota',...result});
  }else if(job.action==='catalog'){
    const result=await evaluate(`(async()=>{const v=await window.__modelDeskZCode.models.getView();return {preferred:v.preferredSelection,
      models:v.providers.flatMap(p=>p.models.map(m=>({providerId:p.providerId,modelId:m.modelId,
        reasoning:m.config?.optionSpecs?.reasoningLevel?.values||[],contextWindow:m.config?.properties?.contextWindow||0})))}})()`);
    emit({type:'catalog',...result});
  }else if(job.action==='task'){
    if(job.cancel_file&&fs.existsSync(job.cancel_file))throw Error('ZCode 任务已取消；未请求模型');
    scope={workspacePath:job.project,sessionId:job.session_id};key=job.session_id;
    const literal=JSON.stringify(scope),id=JSON.stringify(key);
    const names=['read_file','list_directory','search_files','read_image','fetch_url',...(job.kind==='edit'?['write_file','edit_file','run_command']:[])];
    const params={...scope,mode:'yolo',model:job.selection,thoughtLevel:job.selection.options?.reasoningLevel,
      persistence:'deferred',titleGenerationEnabled:false,dynamicWorkflowEnabled:false,
      toolAllowlist:names.map(n=>'mcp__modeldesk_zcode__'+n),
      mcpServers:[{name:'modeldesk_zcode',command:job.python,args:[job.tools_script,'--job',job.tool_job],env:[]}]};
    emit({type:'session_creating',session_id:key});
    const bound=await evaluate(`(async()=>{const md=window.__modelDeskZCode,s=await md.agent.createSession(${JSON.stringify(params)});
      const current=s.settings.model.current;if(current.providerId!==${JSON.stringify(job.selection.providerId)}||current.modelId!==${JSON.stringify(job.selection.modelId)})throw Error('Native model identity mismatch');
      const mode=await md.agent.setMode({...${literal},mode:'yolo'});
      if(mode.settings.mode.current!=='yolo')throw Error('Native tool mode not confirmed');
      const state={events:[],done:false,dispose:null};window.__modelDeskZCodeJobs.set(${id},state);
      state.dispose=md.agent.onDynamicSessionEvent({...${literal},deliveryKind:'desktop-continuous',includeSnapshot:false})(e=>{
        state.events.push(e);if(state.events.length>10000)state.events.shift();
      });return {session_id:s.session.sessionId,model:current,mode:mode.settings.mode.current};})()`);
    created=true;emit({type:'connected',...bound});
    if(job.cancel_file&&fs.existsSync(job.cancel_file))throw Error('ZCode 任务已取消；未请求模型');
    emit({type:'prompt_attempted'});
    await evaluate(`window.__modelDeskZCode.agent.sendPrompt({...${literal},content:${JSON.stringify(job.prompt)},modelSelection:${JSON.stringify(job.selection)}})`);
    const started=Date.now();let done=false,terminal=null,lastHeartbeat=0;
    while(!done){
      if(job.cancel_file&&fs.existsSync(job.cancel_file)){await evaluate(`window.__modelDeskZCode.agent.stopSession(${literal})`);throw Error('ZCode 任务已取消');}
      if(job.timeout>0&&Date.now()-started>job.timeout*1000){await evaluate(`window.__modelDeskZCode.agent.stopSession(${literal})`);throw Error('ZCode 任务达到显式时限');}
      const events=await evaluate(`(()=>{const s=window.__modelDeskZCodeJobs.get(${id});if(!s)throw Error('owned event subscription lost');return s.events.splice(0)})()`);
      for(const e of events||[]){
        const ev=e.event||e,payload=ev.payload||ev.data||{};
        emit({type:'native_event',event_type:ev.type||ev.kind,keys:Object.keys(ev).slice(0,15)});
        if(['turn.completed','turn.failed','turn.cancelled'].includes(ev.type||ev.kind)){done=true;terminal=ev.type||ev.kind;}
      }
      if(Date.now()-lastHeartbeat>=1000){emit({type:'heartbeat'});lastHeartbeat=Date.now();}
      if(!done)await new Promise(r=>setTimeout(r,350));
    }
    const result=await evaluate(`(async()=>{const s=await window.__modelDeskZCode.agent.readSession(${literal});
      return {session:s.session,projection:s.projection,messages:s.messages,settings:{model:s.settings.model.current}}})()`);
    emit({type:'result',value:result,terminal_event:terminal});
  }else throw Error('Unknown ZCode operation');
}catch(error){emit({type:'error',message:String(error.message).slice(0,1800)});process.exitCode=1;}
finally{
  if(scope){
    try{
      await evaluate(`(async()=>{const s=window.__modelDeskZCodeJobs.get(${JSON.stringify(key)});s?.dispose?.dispose();window.__modelDeskZCodeJobs.delete(${JSON.stringify(key)});try{await window.__modelDeskZCode.agent.closeSession(${JSON.stringify(scope)})}catch(e){if(!String(e.message).includes('Session is not active'))throw e;}return true})()`);
      emit({type:'closed',session_id:key});unsubscribed=true;
    }catch{emit({type:'cleanup_unknown',session_id:key});process.exitCode=1;}
  }
  ws.close();
}
