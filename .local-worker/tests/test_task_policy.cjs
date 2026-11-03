'use strict';
// No terminal commands executed; only native/shared decision functions.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const policy=require('../agent-runtime/record-policy.cjs');
const access=require('../agent-runtime/endpoint-access.cjs');
const native=require('../agent-runtime/workbuddy-native-guard.cjs');

function fixture(t,scope='workspace'){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'md-task-authority-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  fs.writeFileSync(path.join(root,'readonly.txt'),'READONLY');
  const config={root,project:root,kind:'edit',access_mode:'project',files:['main.js','readonly.txt'],
    readonly_files:['readonly.txt'],tools:['Write','Edit','Bash','Read'],protected_roots:[],
    task_policy:{schema:1,write_scope:scope}};
  const decide=(name,args)=>native.decide(config,{tool_name:name,tool_input:args});
  return {root,config,decide};
}

test('four adapters share workspace authority distinct from expected files',t=>{
  const {config,decide}=fixture(t);
  assert.equal(decide('Write',{file_path:'tests/new-test.js'}),null);
  for(const name of ['write','edit']){
    const guard=access.createGuard(config); // DSH and Pi both mount this.
    assert.equal(guard(name,{file_path:'tests/new-test.js'}),undefined);
  }
  assert.equal(decide('Write',{file_path:'readonly.txt'}),'任务只读上下文不可修改');
  assert.equal(access.createGuard(config)('write',{file_path:'readonly.txt'}),'任务只读上下文不可修改');
});

test('autonomous schema3 allows unlisted source, tests and reports while protecting sensitive targets',t=>{
  const {root,config}=fixture(t);
  config.task_policy={schema:3,write_scope:'workspace',home:root,deny_paths:[path.join(root,'private-profile')]};
  for(const file of ['src/helper.js','tests/regression.cjs','reports/delivery.md','.modeldesk-task-tmp/report.md']){
    assert.equal(native.decide(config,{tool_name:'Write',tool_input:{file_path:file}}),null,file);
    assert.equal(access.createGuard(config)('write',{file_path:file}),undefined,file);
  }
  for(const file of ['.model-desk/runs/other/task.json','.env','private-profile/auth.json','readonly.txt']){
    assert.ok(native.decide(config,{tool_name:'Write',tool_input:{file_path:file}}),file);
    assert.ok(access.createGuard(config)('write',{file_path:file}),file);
  }
});

test('explicit files, saved project/full access and records remain independent',t=>{
  const {config,decide}=fixture(t,'declared_files');
  assert.equal(decide('Write',{file_path:'new.js'}),'文件未列入本任务可写范围');
  assert.equal(decide('Write',{file_path:'main.js'}),null);
  const full={...config,access_mode:'full'};
  assert.equal(native.decide(full,{tool_name:'Write',tool_input:{file_path:'new.js'}}),'文件未列入本任务可写范围');
  for(const mode of ['project','full']){
    const cfg={...config,access_mode:mode,task_policy:{schema:1,write_scope:'workspace'}};
    assert.ok(native.decide(cfg,{tool_name:'Write',tool_input:{file_path:'.model-desk/x'}}));
    assert.ok(access.createGuard(cfg)('write',{file_path:'.model-desk/x'}));
  }
  const cfg={...config,task_policy:{schema:1,write_scope:'workspace'}};
  assert.ok(native.decide(cfg,{tool_name:'Write',tool_input:{file_path:'../outside.js'}}));
  assert.equal(native.decide({...cfg,access_mode:'full'},{tool_name:'Write',tool_input:{file_path:'../outside.js'}}),null);
});

test('bad contracts refuse and absent contracts keep old native whitelist',t=>{
  const {config}=fixture(t);
  for(const p of [null,{},[],{schema:true,write_scope:'workspace'},{schema:1,write_scope:'full'},
    {schema:1,write_scope:'read_only'},{schema:1,write_scope:'workspace',extra:true}]){
    assert.throws(()=>access.createGuard({...config,task_policy:p}));
    assert.throws(()=>native.decide({...config,task_policy:p},{tool_name:'Write',tool_input:{file_path:'new.js'}}));
  }
  const legacy={...config};delete legacy.task_policy;
  assert.equal(native.decide(legacy,{tool_name:'Write',tool_input:{file_path:'new.js'}}),'文件未列入本任务可写范围');
});

test('readonly targets include junction aliases and denials stay stopped',t=>{
  const {root,config,decide}=fixture(t,'declared_files');
  fs.mkdirSync(path.join(root,'docs'));fs.writeFileSync(path.join(root,'docs','f.txt'),'fixture');
  fs.symlinkSync(path.join(root,'docs'),path.join(root,'alias'),'junction');
  config.readonly_files.push('docs/f.txt');config.files.push('alias/f.txt');
  assert.equal(decide('Write',{file_path:'alias/f.txt'}),'任务只读上下文不可修改');
  const guard=access.createGuard({...config,access_mode:'full'});
  assert.ok(guard('write',{file_path:'new.js'}));
  assert.ok(guard('write',{file_path:'main.js'}));
  assert.ok(guard('bash',{command:'echo OK'}));
  const read={...config,kind:'chat',task_policy:{schema:1,write_scope:'read_only'}};
  assert.ok(access.createGuard(read)('write',{file_path:'main.js'}));
  assert.ok(access.createGuard(read)('bash',{command:'echo OK'}));
  const corrected=access.createGuard(config);
  assert.equal(corrected('write',{}),undefined); // malformed native args do not poison the round
  assert.equal(corrected('write',{file_path:'main.js'}),undefined);
  assert.equal(access.createGuard(config)('write',{cwd:'docs',file_path:'f.txt'}),'任务只读上下文不可修改');
});
