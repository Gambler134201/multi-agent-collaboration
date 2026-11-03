'use strict';
// Pure shared/native decisions against synthetic paths, never real credentials.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {spawnSync}=require('node:child_process');
const policy=require('../agent-runtime/record-policy.cjs');
const access=require('../agent-runtime/endpoint-access.cjs');
const native=require('../agent-runtime/workbuddy-native-guard.cjs');
function fixture(t){
  const box=fs.mkdtempSync(path.join(os.tmpdir(),'md-protected-paths-'));
  t.after(()=>fs.rmSync(box,{recursive:true,force:true}));
  const root=path.join(box,'project'),home=path.join(box,'home'),secret=path.join(home,'.codex');
  fs.mkdirSync(root);fs.mkdirSync(secret,{recursive:true});
  fs.writeFileSync(path.join(secret,'auth.json'),'SYNTHETIC');
  const config={root,project:root,kind:'edit',access_mode:'full',files:[],readonly_files:[],
    tools:['Read','Write','Edit','Bash','Grep','Glob'],protected_roots:[],
    task_policy:{schema:2,write_scope:'workspace',home,deny_paths:[secret]}};
  return {box,root,home,secret,config,
    call:(name,args)=>native.decide(config,{tool_name:name,tool_input:args})};
}
test('business filenames and auth words do not declare credential locations',t=>{
  const {config,call}=fixture(t);
  for(const file of ['auth.json','tokens.json','credentials.json','tests/.codex/fixture.json']){
    assert.equal(call('Write',{file_path:file}),null,file);
    assert.equal(call('Read',{file_path:file}),null,file);
    assert.equal(access.createGuard(config)('write',{file_path:file}),undefined,file);
  }
  for(const command of ['node --test auth-token.test.js','python credential-store-test.py',
    'node tests/.zcode/fixture.js']){
    assert.equal(call('Bash',{command}),null,command);
    assert.equal(access.createGuard(config)('bash',{command}),undefined,command);
  }
  const old={...config,task_policy:{schema:1,write_scope:'workspace'}};
  assert.equal(native.decide(old,{tool_name:'Read',tool_input:{file_path:'tokens.json'}}),'认证存储不作为执行目标');
  assert.equal(native.decide(old,{tool_name:'Bash',tool_input:{command:'node auth-token.test.js'}}),'终端不能访问工作台记录或认证数据');
});
test('same path declaration protects file and shell targets across adapters',t=>{
  const {config,call,secret}=fixture(t);
  for(const file of [path.join(secret,'auth.json'),'~/.codex/auth.json','.codex/new.json',
    '.model-desk/records.db','.git/config','.env']){
    assert.ok(call('Read',{file_path:file}),file);
    assert.ok(call('Write',{file_path:file}),file);
    assert.ok(access.createGuard(config)('read',{file_path:file}),file);
    assert.ok(access.createGuard(config)('write',{file_path:file}),file);
    assert.ok(call('Bash',{command:'cat "'+file+'"'}),file);
    assert.ok(access.createGuard(config)('bash',{command:'cat "'+file+'"'}),file);
  }
  assert.equal(native.denialRule(call('Read',{file_path:path.join(secret,'auth.json')})),'protected_path');
});
test('resolved aliases are denied, ordinary siblings and synthetic same prefixes are not',t=>{
  const {root,secret,config,call}=fixture(t);
  fs.symlinkSync(secret,path.join(root,'alias'),'junction');
  assert.ok(call('Read',{file_path:'alias/auth.json'}));
  assert.ok(access.createGuard(config)('bash',{command:'cat alias/auth.json'}));
  assert.equal(call('Read',{file_path:secret+'-fixture/auth.json'}),null);
  assert.equal(access.createGuard(config)('read',{file_path:secret+'-fixture/auth.json'}),undefined);
  assert.ok(call('Bash',{command:'cd alias && cat auth.json'}));
});
test('destructive targets cannot cover a declared private root through its ancestor',t=>{
  const {home,call,config}=fixture(t);
  assert.ok(call('Bash',{command:'rm -rf "'+home+'"'}));
  assert.ok(access.createGuard(config)('bash',{command:'rm -rf "'+home+'"'}));
  assert.equal(call('Bash',{command:'ls "'+home+'"'}),null);
  // Merely working in the project must not become an ancestor refusal.
  assert.equal(call('Bash',{command:'node --test .'}),null);
});
test('controller-owned read context is readable but links out and writes remain protected',t=>{
  const {root,secret,config}=fixture(t),context=path.join(secret,'context');
  fs.mkdirSync(context);fs.writeFileSync(path.join(context,'brief.txt'),'SYNTHETIC');
  fs.symlinkSync(secret,path.join(context,'out'),'junction');
  config.context_root=context;
  assert.equal(access.createGuard(config)('read',{file_path:path.join(context,'brief.txt')}),undefined);
  assert.ok(access.createGuard(config)('read',{file_path:path.join(context,'out/auth.json')}));
  assert.ok(access.createGuard(config)('write',{file_path:path.join(context,'brief.txt')}));
  const isolated={...config,root,record_project:path.join(root,'original')};
  assert.equal(access.createGuard(isolated)('read',{file_path:path.join(context,'brief.txt')}),undefined);
});
test('malformed or extra protected-path fields never degrade to legacy',t=>{
  const {config}=fixture(t),p=config.task_policy;
  for(const bad of [{...p,home:'relative'},{...p,deny_paths:['relative']},{...p,deny_paths:null},
    {...p,deny_paths:new Array(129).fill(p.home)},{...p,extra:true},{...p,schema:true}]){
    assert.throws(()=>access.createGuard({...config,task_policy:bad}));
    assert.throws(()=>native.decide({...config,task_policy:bad},{tool_name:'Write',tool_input:{file_path:'a.py'}}));
  }
});
test('private Codex state beside managed source does not ban the source parent',t=>{
  const {home,secret,config}=fixture(t),root=path.join(secret,'worktrees','source');
  fs.mkdirSync(root,{recursive:true});
  config.root=config.project=root;
  config.task_policy.deny_paths=[path.join(secret,'auth.json'),path.join(secret,'sessions')];
  assert.equal(access.createGuard(config)('write',{file_path:'helper.py'}),undefined);
  assert.equal(native.decide(config,{tool_name:'Bash',tool_input:{command:'node --test .'}}),null);
  assert.ok(access.createGuard(config)('read',{file_path:path.join(secret,'auth.json')}));
  assert.ok(native.decide(config,{tool_name:'Read',tool_input:{file_path:'~/.codex/auth.json'}}));
  assert.equal(config.task_policy.home,home);
});
test('search exclusions derive from paths without hiding ordinary token business files',t=>{
  const {root,config}=fixture(t),privateDir=path.join(root,'private-data');
  config.task_policy.deny_paths.push(privateDir);
  const glob=native.searchExclusionGlob(config,root);
  assert.ok(glob.includes('private-data/**'));
  assert.ok(glob.includes('.codex/**'));
  assert.ok(glob.includes('**/.model-desk/**'));
  assert.ok(!glob.includes('tokens'));
  fs.mkdirSync(privateDir);fs.writeFileSync(path.join(privateDir,'hidden.txt'),'PRIVATE');
  fs.writeFileSync(path.join(root,'tokens.json'),'BUSINESS');
  const result=spawnSync('rg',['--files','--hidden','--glob',glob,root],{encoding:'utf8'});
  if(result.error?.code!=='ENOENT'){
    assert.equal(result.status,0,result.stderr);
    assert.ok(result.stdout.includes('tokens.json'));
    assert.ok(!result.stdout.includes('hidden.txt'));
  }
  assert.equal(native.searchExclusionGlob({...config,task_policy:{schema:1,write_scope:'workspace'}},root),native.SEARCH_EXCLUSION_GLOB);
});
test('one literal directory traversal supplies both branches to callers',t=>{
  const {root}=fixture(t);
  const facts=policy.terminalFacts('cd tests && cd nested; node check.js',root);
  assert.equal(facts.overflow,false);
  assert.ok(facts.locations.includes(policy.normalizePath(path.join(root,'tests'))));
  assert.ok(facts.locations.includes(policy.normalizePath(path.join(root,'tests','nested'))));
  assert.ok(facts.contexts.some(item=>item.base===policy.normalizePath(path.join(root,'tests','nested'))));
  assert.ok(facts.contexts.some(item=>item.base===policy.normalizePath(path.join(root,'tests'))));
  assert.deepEqual(policy.terminalFacts('echo "cd .model-desk"',root).locations,[]);
});
