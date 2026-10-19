'use strict';
/** Installed DSH Agent loop, isolated profile, scripted local provider only. */
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {createRequire}=require('node:module');
const {pathToFileURL}=require('node:url');
const {makeSandbox,helper,startProvider}=require('./test_record_protection_runtime.cjs');
const ROOT=path.resolve(__dirname,'..');

test('installed DSH followup opens one new model turn after the first turn ends',
 {skip:!process.env.MODEL_DESK_TEST_PYTHON||!process.env.MODEL_DESK_DSH_EXE,timeout:45000},async()=>{
  const box=makeSandbox('native-wakeup-fixture');
  const provider=await startProvider([]);
  let runtime;
  try{
    const request={mode:'render',root:ROOT,sandbox:box.sandbox,project:box.project,run:box.run,
      task_id:box.taskId,kind:'edit',provider:'local',preset:'native',
      api_base:'http://127.0.0.1:'+provider.port+'/v1',state_dir:path.join(box.sandbox,'state'),
      session_key:'native-wakeup-fixture',patch:path.join(box.sandbox,'patch.yml'),
      job:path.join(box.sandbox,'job.json'),prompt:path.join(box.run,'dsh-prompt.txt'),
      cancel:path.join(box.run,'cancelled.flag'),record_proof:path.join(box.run,'record-proof.json'),
      extra_plugin:path.join(__dirname,'dsh-wakeup-native-fixture.cjs'),
      extra_config:{proof:path.join(box.sandbox,'native-idle-proof.json')}};
    helper(request);
    const job=JSON.parse(fs.readFileSync(request.job,'utf8'));
    const requireFrom=createRequire(path.join(ROOT,'agent-runtime/package.json'));
    const acpx=await import(pathToFileURL(requireFrom.resolve('acpx/runtime')).href);
    runtime=acpx.createAcpRuntime({cwd:box.project,
      agentProcessEnv:{...process.env,ELECTRON_RUN_AS_NODE:'1',DSH_HOME:box.home,
        DSH_WORKER_API_KEY:'fixture-only-key',DSH_TELEMETRY_MODE:'DISABLED',
        MODEL_DESK_PRESET_PROOF:path.join(box.run,'preset-proof.json'),
        MODEL_DESK_RECORD_PROOF:request.record_proof},
      agentRegistry:acpx.createAgentRegistry({overrides:{dsh:[job.executable,'--expose-internals',job.cli,'--profile','acp','--patch',job.patch]}}),
      sessionStore:acpx.createFileSessionStore({stateDir:job.state_dir}),
      permissionMode:'deny-all',nonInteractivePermissions:'deny',fs:false,terminal:false,
      onPermissionRequest:async()=>({outcome:'reject_once'})});
    const handle=await runtime.ensureSession({sessionKey:job.session_key,agent:'dsh',mode:'persistent',cwd:job.cwd});
    const turn=runtime.startTurn({handle,text:'Return a brief fixture response. No tools.',mode:'prompt',requestId:'initial-fixture'});
    for await(const _event of turn.events){}
    const result=await turn.result;
    assert.notEqual(result.status,'failed');
    const deadline=Date.now()+8000;
    while(provider.captured.length<2&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,100));
    assert.equal(provider.captured.length,2,'native followup must cause a second provider request');
    assert.ok(fs.existsSync(request.extra_config.proof),'probe must be armed after an idle transition');
    const texts=provider.captured[1].messages.map(row=>typeof row.content==='string'?row.content:JSON.stringify(row.content));
    assert.ok(texts.some(text=>text.includes('[Model Desk wakeup probe]')));
    await new Promise(resolve=>setTimeout(resolve,350));
    assert.equal(provider.captured.length,2,'no third turn or recurring wakeup');
  }finally{
    if(runtime)await runtime.shutdown();
    provider.server.close();
    fs.rmSync(box.sandbox,{recursive:true,force:true});
  }
});
