'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const guard=require('../agent-runtime/workbuddy-native-guard.cjs');

function fixture(t){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'md-profile-boundary-'));
  const original=path.join(root,'project'),copy=path.join(root,'project-task-workspaces','task');
  fs.mkdirSync(path.join(copy,'src'),{recursive:true});fs.mkdirSync(original);
  fs.writeFileSync(path.join(copy,'src','main.js'),'fixture');
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  return {root,original,copy,job:{project:copy,record_project:original,kind:'edit',access_mode:'full',
    files:['src/main.js'],readonly_files:[],tools:['Bash','Read'],protected_roots:[original]}};
}
test('absolute command in sibling task copy is not the original protected project',t=>{
  const {job,copy}=fixture(t);
  for(const target of [path.join(copy,'src'),path.join(copy,'src','main.js')]){
    assert.equal(guard.decide(job,{tool_name:'Bash',tool_input:{command:`ls "${target}"`}}),null);
  }
});
test('protected original roots and descendants remain blocked',t=>{
  const {job,original}=fixture(t);
  // Omit the separate original-tree isolation check so these directly prove
  // the shared protected-root matcher still refuses actual protected access.
  const native={...job,record_project:undefined};
  for(const command of [`ls "${original}"`,`ls "${original}/secret.txt"`,
    `python -c "open('${original}/secret.txt').read()"`,`ls "${original}:stream"`,
    `ls "${original}."`]){
    assert.equal(guard.decide(native,{tool_name:'Bash',tool_input:{command}}),'终端不能访问原生配置目录');
  }
});
test('mixed slash and case spellings do not open a protected path',t=>{
  const {job,original}=fixture(t);
  const native={...job,record_project:undefined};
  for(const target of [original.toUpperCase(),original.replace(/\\/g,'/')]){
    assert.equal(guard.decide(native,{tool_name:'Bash',tool_input:{command:`ls "${target}/file"`}}),'终端不能访问原生配置目录');
  }
});
test('whole literal output has no protected file target, while actual reads do',t=>{
  const {job,original}=fixture(t);
  assert.equal(guard.decide({...job,record_project:undefined},{tool_name:'Bash',tool_input:{command:`echo "${original}"`}}),null);
  assert.equal(guard.decide({...job,record_project:undefined},{tool_name:'Bash',tool_input:{command:`cat "${original}"`}}),'终端不能访问原生配置目录');
});
test('junction alias into the protected original tree is refused on resolved targets',t=>{
  const {root,original,job}=fixture(t);
  fs.writeFileSync(path.join(original,'secret.txt'),'fixture');
  const alias=path.join(root,'original-alias');
  fs.symlinkSync(original,alias,'junction');
  t.after(()=>fs.rmSync(alias,{recursive:true,force:true}));
  // The alias never spells the protected root, so the lexical terminal matcher
  // cannot see it. Workspace isolation still resolves the alias into the
  // original tree (realpathDeep) for terminal and read tool calls.
  assert.equal(guard.denialRule(guard.decide(job,{tool_name:'Bash',tool_input:{command:`type "${path.join(alias,'secret.txt')}"`}})),'workspace_isolation');
  assert.equal(guard.denialRule(guard.decide(job,{tool_name:'Read',tool_input:{file_path:path.join(alias,'secret.txt')}})),'workspace_isolation');
  // With the separate record-project isolation out of the way, the resolved
  // write target still reaches the protected-root matcher (both an existing
  // file and a not-yet-existing leaf inside the alias).
  const native={...job,record_project:undefined,tools:[...job.tools,'Write']};
  for(const file of [path.join(alias,'secret.txt'),path.join(alias,'new.txt')]){
    assert.equal(guard.denialRule(guard.decide(native,{tool_name:'Write',tool_input:{file_path:file}})),'native_profile');
  }
});
test('documents current lexical boundary: junction alias to the original tree is only caught on resolved file targets',t=>{
  const {root,original,job}=fixture(t);
  fs.writeFileSync(path.join(original,'secret.txt'),'fixture');
  const alias=path.join(root,'original-alias');
  fs.symlinkSync(original,alias,'junction');
  t.after(()=>fs.rmSync(alias,{recursive:true,force:true}));
  // Recorded behaviour, not a desired one: with the separate record-project
  // isolation out of the way, the terminal path is matched lexically, the
  // alias spelling never matches the protected root, and no later terminal
  // layer resolves the alias against protected_roots. Do not adjust this test
  // to keep it passing: change the guard, then update this record.
  const native={...job,record_project:undefined};
  for(const command of [`type "${path.join(alias,'secret.txt')}"`,`Remove-Item "${alias}" -Recurse`]){
    assert.equal(guard.denialRule(guard.decide(native,{tool_name:'Bash',tool_input:{command}})),null);
  }
});
test('recursively cleaning variable-built or substitution-fed targets stays conservatively denied',t=>{
  const {job}=fixture(t);
  for(const command of ['Remove-Item "$env:MODEL_DESK_FAKE\\sub" -Recurse','rm -r $(cat list.txt)']){
    assert.equal(guard.denialRule(guard.decide(job,{tool_name:'Bash',tool_input:{command}})),'unverified_recursive_cleanup');
  }
});
test('a variable-bearing command without protected references stays allowed',t=>{
  const {job}=fixture(t);
  assert.equal(guard.denialRule(guard.decide(job,{tool_name:'Bash',tool_input:{command:'Write-Output "$env:USERNAME"'}})),null);
});
test('literal negative rg globs exclude records rather than target them',t=>{
  const {job}=fixture(t);
  for(const command of ["rg --files --glob '!.model-desk' src",'rg --files --glob="!**/.model-desk/**" src',
    "rg --files -g '!**/.zcode/**' -g '!**/.env' src",
    "rg --files --glob '!.model-desk' src; echo DONE",
    "rg --files --glob '!.model-desk' src && echo DONE"]){
    assert.equal(guard.decide(job,{tool_name:'Bash',tool_input:{command}}),null);
  }
});
test('positive, unquoted, expanded and compound protected rg references stay denied',t=>{
  const {job}=fixture(t);
  for(const command of ["rg --files --glob '.model-desk/**' .","rg --files --glob '!.model-desk' .model-desk",
    'rg --files --glob !.model-desk .',"rg --files --glob '!$HOME/.model-desk' .",
    "rg --files --glob '!.model-desk' . && cat .model-desk/records.db",
    "echo --glob '!.model-desk'"]){
    assert.ok(guard.decide(job,{tool_name:'Bash',tool_input:{command}}),command);
  }
});

test('bounded literal source searches treat the quoted query as text, not a protected target',t=>{
  const {job,original}=fixture(t);
  for(const command of ["rg -n '.zcode/config' src/main.js","grep -n -e '.model-desk' src/main.js",
    "rg -F '.env' src/main.js","rg -n 'auth-token' src/main.js",
    `rg -n '${original}' src/main.js`,
    "rg -n '.zcode' src/main.js && grep -n '.model-desk' src/main.js"]){
    assert.equal(guard.decide(job,{tool_name:'Bash',tool_input:{command}}),null,command);
  }
  assert.equal(guard.decide(job,{tool_name:'Bash',tool_input:{command:"rg -n '.zcode' main.js",cwd:'src'}}),null);
});

test('bounded literal search support never masks actual private paths or opaque shell syntax',t=>{
  const {job,copy,original}=fixture(t);
  fs.mkdirSync(path.join(copy,'.zcode'));fs.writeFileSync(path.join(copy,'.zcode','config.json'),'private fixture');
  fs.mkdirSync(path.join(copy,'.model-desk'));fs.writeFileSync(path.join(copy,'.model-desk','records.db'),'private fixture');
  fs.writeFileSync(path.join(original,'secret.txt'),'private fixture');
  const alias=path.join(copy,'src','linked');fs.symlinkSync(original,alias,'junction');
  for(const command of ["rg -n '.zcode' .zcode/config.json","grep -n '.model-desk' .model-desk/records.db",
    "rg -n '.zcode' src/linked/secret.txt","rg -n '.zcode' src",
    "rg --pre python -n '.zcode' src/main.js","rg -n '.zcode' src/main.js | cat .model-desk/records.db",
    "rg -n '.zcode' src/main.js > result.txt","rg -n '.zcode' $SOURCE",
    "rg -n \"$env:VALUE.zcode\" src/main.js","echo '.zcode/config'",
    "cd src && rg -n '.zcode' main.js","rg -n '.zcode' src/main.js"+' '.repeat(16384)]){
    assert.ok(guard.decide(job,{tool_name:'Bash',tool_input:{command}}),command);
  }
});
