'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url'),path=require('node:path');
const contract=import(pathToFileURL(path.join(__dirname,'../agent-runtime/zcode-session-contract.mjs')).href);
const selection={providerId:'account:zai-start-plan',modelId:'GLM-5.3-Flash'};
function receipt(mode){return {session:{sessionId:'owned'},settings:{mode:{current:mode},model:{current:selection}}};}
test('actual 3.16.3 build receipts complete readonly bootstrap before any prompt',async()=>{
  const c=await contract,mode=c.requestedMode('chat');let prompts=0;
  c.assertTools('chat',[...c.READ_TOOLS]);
  c.assertReceipt(receipt('build'),{stage:'createSession',mode,sessionId:'owned',selection,transition:true});
  c.assertReceipt(receipt('build'),{stage:'setMode',mode});
  prompts++;
  c.assertReceipt(receipt('build'),{stage:'readSession',mode,sessionId:'owned',selection});
  assert.equal(prompts,1);assert.equal(mode,'build');
});
test('edit explicitly transitions initial build to confirmed yolo',async()=>{
  const c=await contract,mode=c.requestedMode('edit');
  c.assertTools('edit',[...c.EDIT_TOOLS]);
  c.assertReceipt(receipt('build'),{stage:'createSession',mode,sessionId:'owned',selection,transition:true});
  c.assertReceipt(receipt('yolo'),{stage:'setMode',mode});
  c.assertReceipt(receipt('yolo'),{stage:'readSession',mode,sessionId:'owned',selection});
  assert.equal(c.requestedMode('chat',false),'yolo');
});
test('old plan request versus real build receipt is still refused rather than a fallback',async()=>{
  const c=await contract;
  assert.throws(()=>c.assertReceipt(receipt('build'),{stage:'setMode',mode:'plan'}),/requested=plan observed=build/);
});
test('chat cannot acquire write, edit, bash or duplicated tools',async()=>{
  const c=await contract;
  for(const name of ['Write','Edit','Bash','Read'])assert.throws(()=>c.assertTools('chat',[...c.READ_TOOLS,name]));
  assert.throws(()=>c.assertTools('chat',['Read']));assert.throws(()=>c.requestedMode('other'));
});
test('missing and unexpected mode receipts fail before prompt and do not echo unsafe fields',async()=>{
  const c=await contract;
  for(const value of [null,[],{}, {settings:{}},receipt(null),receipt(0),receipt('unknown-secret-value'),receipt('plan'),receipt('yolo')]){
    let prompts=0;
    try{c.assertReceipt(value,{stage:'setMode',mode:'build'});prompts++;}catch(e){assert.doesNotMatch(e.message,/unknown-secret-value/);}
    assert.equal(prompts,0);
  }
});
test('create/read session and model identities remain required',async()=>{
  const c=await contract;
  for(const stage of ['createSession','readSession']){
    const options={stage,mode:'build',sessionId:'owned',selection};
    for(const value of [{...receipt('build'),session:null},
      {...receipt('build'),session:{sessionId:'other'}},
      {...receipt('build'),settings:{mode:{current:'build'}}},
      {...receipt('build'),settings:{mode:{current:'build'},model:{current:{...selection,modelId:'other'}}}}]){
      assert.throws(()=>c.assertReceipt(value,options));
    }
  }
});
test('mode drift in final session never becomes successful completion',async()=>{
  const c=await contract;
  assert.throws(()=>c.assertReceipt(receipt('yolo'),{stage:'readSession',mode:'build',sessionId:'owned',selection}),/mode not confirmed/);
});
test('readonly chat final drift to a higher-authority mode never completes',async()=>{
  const c=await contract,mode=c.requestedMode('chat');
  // Contract: when a read-only chat task's final readSession receipt drifts to
  // a higher-authority mode (yolo or plan), the task must never be reported as
  // completed, and the failure must not echo an unknown observed mode value.
  assert.equal(mode,'build');
  for(const drifted of ['yolo','plan'])
    assert.throws(()=>c.assertReceipt(receipt(drifted),{stage:'readSession',mode,sessionId:'owned',selection}),/mode not confirmed/);
  let prompts=0;
  try{c.assertReceipt(receipt('unknown-secret-value'),{stage:'readSession',mode,sessionId:'owned',selection});prompts++;}
  catch(e){assert.doesNotMatch(e.message,/unknown-secret-value/);}
  assert.equal(prompts,0);
});
