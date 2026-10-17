// Exercise the actual runner wiring against a fake ACP backend, with temporary files.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import assert from 'node:assert/strict';
const root=fs.mkdtempSync(path.join(os.tmpdir(),'desk-timing-'));
try {
  const runner=fs.readFileSync(fileURLToPath(new URL('./run-dsh.mjs',import.meta.url)),'utf8');
  fs.writeFileSync(path.join(root,'runner.mjs'),runner.replace("from 'acpx/runtime'","from './fake-runtime.mjs'"));
  fs.writeFileSync(path.join(root,'fake-runtime.mjs'),`
import fs from 'node:fs';
const job=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const write=(type,options)=>fs.appendFileSync(job.observations,JSON.stringify({type,options})+'\\n');
export const createAgentRegistry=x=>x,createFileSessionStore=x=>x;
export function createAcpRuntime(options){
 write('runtime',options);
 return {
  ensureSession:async()=>{
   fs.writeFileSync(job.record_proof,JSON.stringify({source:'model-desk-record-protection',probe:'ok',scope:'global',revision:job.record_revision,root:job.cwd,session_id:job.proof_session||'fake'}));
   return {backendSessionId:'fake'};
  },
  getStatus:async()=>({models:{availableModelIds:[JSON.stringify(['desk-worker',job.model])],currentModelId:JSON.stringify(['desk-worker',job.model])}}),
  startTurn(input){
   write('turn',input);
   let finish;const result=new Promise(resolve=>{finish=resolve;});
   if(job.doCancel)setTimeout(()=>fs.writeFileSync(job.cancel_file,''),20);
   const timer=setTimeout(()=>finish({status:'completed',stopReason:'end_turn'}),300);
   return {result,events:(async function*(){await result;})(),cancel:async()=>{write('cancel',{});clearTimeout(timer);finish({status:'cancelled'});}};
  },shutdown:async()=>{},
 };
}
`);
  for(const [timeout,doCancel] of [[0,false],[30,false],[0,true]]){
    const prefix=path.join(root,`${timeout}-${doCancel}`);
    const job={timeout,doCancel,model:'fake',kind:'chat',cwd:root,executable:'unused',cli:'unused',patch:'unused',state_dir:root,session_key:'fake',task_id:'fake',prompt_file:prefix+'.txt',cancel_file:prefix+'.cancel',observations:prefix+'.jsonl'};
    job.record_proof=prefix+'.proof';job.record_revision='offline-timing-fixture';
    fs.writeFileSync(job.prompt_file,'offline');fs.writeFileSync(prefix+'.job',JSON.stringify(job));
    const result=spawnSync(process.execPath,[path.join(root,'runner.mjs'),prefix+'.job'],{encoding:'utf8',timeout:5000});
    assert.equal(result.status,0,result.stderr);
    const rows=fs.readFileSync(job.observations,'utf8').trim().split('\n').map(JSON.parse);
    for(const type of ['runtime','turn']){
      const options=rows.find(row=>row.type===type).options;
      if(timeout===0)assert.equal(Object.hasOwn(options,'timeoutMs'),false);
      else assert.equal(options.timeoutMs,30000);
    }
    if(doCancel){assert.ok(rows.some(row=>row.type==='cancel'));assert.match(result.stdout,/"status":"cancelled"/);}
    else assert.match(result.stdout,/"status":"completed"/);
  }
  const badPrefix=path.join(root,'wrong-parent');
  const bad={timeout:0,model:'fake',kind:'edit',cwd:root,executable:'unused',cli:'unused',patch:'unused',state_dir:root,
    session_key:'fake',task_id:'wrong-parent',prompt_file:badPrefix+'.txt',cancel_file:badPrefix+'.cancel',observations:badPrefix+'.jsonl',
    record_proof:badPrefix+'.proof',record_revision:'offline-timing-fixture',proof_session:'child'};
  fs.writeFileSync(bad.prompt_file,'offline');fs.writeFileSync(badPrefix+'.job',JSON.stringify(bad));
  const denied=spawnSync(process.execPath,[path.join(root,'runner.mjs'),badPrefix+'.job'],{encoding:'utf8',timeout:5000});
  assert.equal(denied.status,1,denied.stderr);
  assert.match(denied.stdout,/未绑定当前父会话/);
  const deniedRows=fs.readFileSync(bad.observations,'utf8').trim().split('\n').map(JSON.parse);
  assert.equal(deniedRows.some(row=>row.type==='turn'),false,'unbound protection must refuse before a turn');
  console.log('Actual runner: both timeout options omitted by default; explicit limit forwarded; manual stop preserved.');
  console.log('Actual runner: a child-session protection proof refuses admission before any prompt/tool dispatch.');
}finally{fs.rmSync(root,{recursive:true,force:true});}
