'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const plugin=require('../agent-runtime/record-protection.cjs');
const policy=require('../agent-runtime/record-policy.cjs');
const native=require('../agent-runtime/workbuddy-native-guard.cjs');
function fixture(t){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'md-dsh-refusal-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const file=path.join(root,'proof.json'),handlers={};let guard;
  plugin({tools:{guard(fn){guard=fn;return ()=>{};},guardReason(exec){return guard(exec);}},
    on(name,fn){handlers[name]=fn;}},{root,revision:plugin.selfRevision(),proof:file});
  handlers['agent/created']({agent:{id:'synthetic-parent'}});
  return {root,file,guard,proof:()=>JSON.parse(fs.readFileSync(file,'utf8'))};
}
test('DSH records first real refusal without raw target and preserves parent binding',t=>{
  const f=fixture(t);
  assert.equal(f.guard({name:'write',arguments:{file_path:'source.py'}}),undefined);
  assert.equal(f.proof().guard_denial,undefined);
  assert.ok(f.guard({name:'write',arguments:{file_path:'.model-desk/private-marker.json'}}));
  const proof=f.proof();
  assert.equal(proof.session_id,'synthetic-parent');
  assert.equal(proof.guard_denial.code,'TOOL_PERMISSION_DENIED');
  assert.equal(proof.guard_denial.rule_id,'record_target');
  assert.ok(!JSON.stringify(proof).includes('private-marker'));
  assert.ok(f.guard({name:'bash',arguments:{command:'echo retry'}}));
  assert.deepEqual(f.proof().guard_denial,proof.guard_denial);
});
test('ordinary errors returned by a native body cannot manufacture a policy refusal',t=>{
  const f=fixture(t);
  assert.equal(f.guard({name:'read',arguments:{file_path:'missing.txt'}}),undefined);
  const bodyResult={isError:true,message:'permission denied'}; // Native result, not guard evidence.
  assert.ok(bodyResult.isError);
  assert.equal(f.proof().guard_denial,undefined);
  assert.equal(f.guard({name:'write',arguments:{file_path:'corrected.py'}}),undefined);
});
test('four adapters use one fixed denial mapping and never persist unknown tool text',()=>{
  assert.equal(native.denialCode,policy.denialCode);
  assert.equal(native.denialRule,policy.denialRule);
  const data=policy.guardDenial('private-tool-marker','private-reason-marker');
  assert.equal(data.tool,'unknown');assert.equal(data.rule_id,'unclassified');
  assert.ok(!JSON.stringify(data).includes('marker'));
});
