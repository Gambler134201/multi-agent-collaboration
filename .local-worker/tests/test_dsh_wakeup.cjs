'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const {createBridge}=require('../../integrations/dsh-brain/wakeup.cjs');

function fixture(t){
  const project=fs.mkdtempSync(path.join(os.tmpdir(),'md-dsh-wakeup-'));
  const identity={id:'session-fixture',owner:'dsh-fixture',project};
  const calls=[],timers=[];
  const flags={current:true,enabled:true};
  const agent={status:'running',session:{seq:1},inbox:{hasPending:false},
    followup(message){calls.push(message);agent.status='running';}};
  const bridge=createBridge({agent,identity,isCurrent:()=>flags.current,enabled:()=>flags.enabled,
    setTimer:(fn,ms)=>{const timer={fn,ms,unref(){}};timers.push(timer);return timer;},
    clearTimer:timer=>{const index=timers.indexOf(timer);if(index>=0)timers.splice(index,1);}});
  const env=bridge.environment();
  const call=(operation,body={})=>bridge.handle({operation,token:env.MODEL_DESK_DSH_WAKE_TOKEN,
    session:identity.id,owner:identity.owner,...body});
  t.after(async()=>{await bridge.dispose();fs.rmSync(project,{recursive:true,force:true});});
  return {project,identity,agent,calls,timers,flags,bridge,env,call,tick(){timers.shift().fn();}};
}

test('fixed probe waits for the original turn to end and wakes once',async t=>{
  const f=fixture(t),probe=f.call('probe',{action:'start'});
  assert.equal(probe.waiting,true);assert.equal(f.timers[0].ms,30000);
  assert.equal(f.call('probe',{action:'start'}).probe_id,probe.probe_id);
  assert.equal(f.timers.length,1);assert.equal(f.calls.length,0);
  f.tick();assert.equal(f.calls.length,0);assert.equal(f.timers[0].ms,1000);
  f.agent.status='idle';f.tick();assert.equal(f.calls.length,1);
  assert.ok(f.calls[0].content[0].text.startsWith('[Model Desk wakeup probe]'));
  assert.equal(f.calls[0].role,'user');assert.equal(f.calls[0].source.kind,'user');
  assert.equal(f.call('probe',{action:'status',probe_id:probe.probe_id}).listener_status,'delivered');
  assert.equal(f.call('probe',{action:'received',probe_id:probe.probe_id}).received,true);
  assert.equal(f.call('probe',{action:'received',probe_id:probe.probe_id}).received,true);
  f.call('probe',{action:'start'});assert.equal(f.timers.length,0);assert.equal(f.calls.length,1);
  assert.ok(!JSON.stringify(probe).includes(f.env.MODEL_DESK_DSH_WAKE_TOKEN));
});

test('explicit stop clears pending probe without sending or restarting',t=>{
  const f=fixture(t),probe=f.call('probe',{action:'start'});
  assert.throws(()=>f.call('probe',{action:'received',probe_id:probe.probe_id}));
  assert.equal(f.call('probe',{action:'stop',probe_id:probe.probe_id}).listener_status,'stopped');
  assert.equal(f.timers.length,0);assert.equal(f.calls.length,0);
  assert.equal(f.call('probe',{action:'start'}).waiting,false);
});

test('pending native inbox is busy even if agent reports idle',t=>{
  const f=fixture(t);f.call('probe',{action:'start'});
  f.agent.status='idle';f.agent.inbox.hasPending=true;f.tick();assert.equal(f.calls.length,0);
  f.agent.inbox.hasPending=false;f.tick();assert.equal(f.calls.length,1);
});

test('identity drift, disabled policy and disposal do not wake another chat',async t=>{
  for(const flag of ['current','enabled']){
    const f=fixture(t),probe=f.call('probe',{action:'start'});
    f.agent.status='idle';f.flags[flag]=false;f.tick();assert.equal(f.calls.length,0);
    if(flag==='enabled')assert.equal(f.call('probe',{action:'status',probe_id:probe.probe_id}).listener_status,'stopped');
    else assert.throws(()=>f.call('read'));
  }
  const f=fixture(t);f.call('probe',{action:'start'});await f.bridge.dispose();
  assert.equal(f.timers.length,0);assert.throws(()=>f.call('read'));assert.equal(f.calls.length,0);
});

test('unknown native admission is claimed once and never retried',t=>{
  const f=fixture(t);f.agent.followup=()=>{f.calls.push('attempt');throw Error('PRIVATE_NATIVE_ERROR');};
  const probe=f.call('probe',{action:'start'});f.agent.status='idle';f.tick();
  const status=f.call('probe',{action:'status',probe_id:probe.probe_id});
  assert.equal(status.listener_status,'delivery_unknown');assert.equal(status.send_attempts,1);
  assert.ok(!JSON.stringify(status).includes('PRIVATE_NATIVE_ERROR'));
  assert.throws(()=>f.call('probe',{action:'received',probe_id:probe.probe_id}));
  f.call('probe',{action:'start'});assert.equal(f.calls.length,1);assert.equal(f.timers.length,0);
});

test('pipe operations cannot select another owner/session or carry arbitrary prompts',t=>{
  const f=fixture(t);
  for(const body of [{token:'0'.repeat(64)},{session:'session-other'},{owner:'other'},{prompt:'execute arbitrary text'}])
    assert.throws(()=>f.call('read',body));
  assert.throws(()=>f.call('arbitrary-command'));
  assert.throws(()=>f.call('probe',{action:'received',probe_id:'probe-'+'0'.repeat(32)}));
  assert.equal(f.calls.length,0);
});

test('fixed task references are project-bound, busy-safe and not replayed',t=>{
  const f=fixture(t),reference={project:f.project,wait_id:'wait-'+'a'.repeat(24),task_ids:['task-one']};
  assert.equal(f.call('send',{reference}).busy,true);assert.equal(f.calls.length,0);
  assert.throws(()=>f.call('send',{reference:{...reference,project:path.dirname(f.project)}}));
  assert.throws(()=>f.call('send',{reference:{...reference,prompt:'unsafe'}}));
  f.flags.enabled=false;assert.throws(()=>f.call('send',{reference}));f.flags.enabled=true;
  f.agent.status='idle';assert.equal(f.call('send',{reference}).host_accepted,true);
  assert.equal(f.call('send',{reference}).already_attempted,true);assert.equal(f.calls.length,1);
});

test('real Windows pipe and detached shared waiter deliver once with no model',
 {skip:process.platform!=='win32'||!process.env.MODEL_DESK_TEST_PYTHON},async t=>{
  const project=fs.mkdtempSync(path.join(os.tmpdir(),'md-dsh-wait-process-'));
  const calls=[],identity={id:'session-process-fixture',owner:'dsh-process-fixture',project};
  const binding='b'.repeat(64);
  const agent={status:'idle',session:{seq:1},inbox:{hasPending:false},followup:msg=>calls.push(msg)};
  const bridge=createBridge({agent,identity,isCurrent:()=>true});
  await bridge.start();
  const root=path.resolve(__dirname,'..');
  const script=[
    'import sys,json,time', 'from pathlib import Path', 'sys.path.insert(0,sys.argv[1])',
    'import completion_wait as wait', 'project=Path.cwd()',
    "run=project/'.model-desk/runs/task-fixture'; run.mkdir(parents=True)",
    "wait.registry.write_json(run/'task.json',{'id':'task-fixture','project':str(project),'status':'running','timeout':0,'brain':{'owner':'dsh-process-fixture','binding_key':'"+binding+"'}})",
    "subscription=wait.arm(str(project),['task-fixture'])",
    "print(json.dumps({'armed':subscription['listener_status']}),flush=True)",
    'time.sleep(1.2)',
    "task=wait.registry.read_json(run/'task.json');task.update(status='completed',ended_at=wait.now());wait.registry.write_json(run/'task.json',task)",
    "deadline=time.monotonic()+8",
    "while time.monotonic()<deadline:",
    " data=wait.load(str(project),subscription['id'])",
    " if data['listener_status']=='delivered':break",
    ' time.sleep(0.1)',
    "assert data['listener_status']=='delivered',data['listener_status']",
    "assert data['send_attempts']==1",
    "wait.received(str(project),subscription['id'])",
    "print(json.dumps({'delivered':True,'attempts':data['send_attempts']}),flush=True)",
  ].join('\n');
  const env={SYSTEMROOT:process.env.SYSTEMROOT||process.env.SystemRoot,WINDIR:process.env.WINDIR,
    TEMP:process.env.TEMP,TMP:process.env.TMP,PYTHONIOENCODING:'utf-8',
    ...bridge.environment(),MODEL_DESK_DSH_WAKE_BINDING:binding};
  try{
    const result=await new Promise((resolve,reject)=>{
      const child=spawn(process.env.MODEL_DESK_TEST_PYTHON,['-c',script,root],{cwd:project,env,windowsHide:true});
      let stdout='',stderr='';
      child.stdout.on('data',chunk=>{stdout+=chunk;});child.stderr.on('data',chunk=>{stderr+=chunk;});
      const timer=setTimeout(()=>{child.kill();reject(Error('isolated waiter timeout'));},20000);
      child.on('error',reject);child.on('close',code=>{clearTimeout(timer);resolve({code,stdout,stderr});});
    });
    assert.equal(result.code,0,result.stderr);assert.ok(result.stdout.includes('"delivered": true'));
    assert.equal(calls.length,1);assert.ok(calls[0].content[0].text.startsWith('[Model Desk completion event]'));
  }finally{
    await bridge.dispose();
    // Only the test's own temp tree is removed after the waiter has exited.
    fs.rmSync(project,{recursive:true,force:true});
  }
});
