'use strict';
// Pure decisions against disposable paths. Never execute the shell samples.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const policy=require('../agent-runtime/record-policy.cjs');
const access=require('../agent-runtime/endpoint-access.cjs');
const native=require('../agent-runtime/workbuddy-native-guard.cjs');
function fixture(t){
  const box=fs.mkdtempSync(path.join(os.tmpdir(),'md-direct-terminal-'));
  t.after(()=>fs.rmSync(box,{recursive:true,force:true}));
  const root=path.join(box,'copy'),original=path.join(box,'original'),home=path.join(box,'home');
  fs.mkdirSync(root);fs.mkdirSync(original);fs.mkdirSync(home);
  const config={root,project:root,record_project:original,kind:'edit',access_mode:'project',
    files:['main.js'],readonly_files:['readonly.txt'],protected_roots:[],
    tools:['Read','Write','Edit','Bash','PowerShell'],
    task_policy:{schema:3,write_scope:'workspace',home,deny_paths:[path.join(home,'private')]}};
  const calls=(name,args,cfg=config)=>[
    // Same mount for Pi and DSH; WorkBuddy shared hook also backs ZCode.
    access.createGuard(cfg)(name==='Bash'?'bash':name==='PowerShell'?'pwsh':name.toLowerCase(),args),
    native.decide(cfg,{tool_name:name,tool_input:args})];
  return {box,root,home,original,config,calls};
}
test('v3 no whole-command guesses for filters, payloads, scripts or dynamic cleanup',t=>{
  const {root,original,calls}=fixture(t);
  const commands=[
    `$d='${root}'; Get-ChildItem -Force -Recurse -Exclude '.git' $d | Where-Object { $_.FullName -notlike "$d\\.git*" }`,
    "rg -n '.model-desk|.env|git commit|rm -rf' src --glob '!.git'",
    `Write-Output '${original}'`,
    'node -e "console.log(\'.model-desk\')"',
    'rm -rf "$TEMP"/edge-prof-*',
    'cd "$DEST" && node --test',
    'cd "$TEMP"; rm -rf .',
    'cd scratch; rm -rf .',
    'cd scratch && rm -rf .',
    "Remove-Item -Recurse -Exclude '.model-desk' .",
    "Set-Content note.txt '.model-desk'",
    'python test_auth.py; node --test',
  ];
  fs.mkdirSync(path.join(root,'scratch'));
  for(const command of commands)for(const name of ['Bash','PowerShell']){
    const input={command},before=JSON.stringify(input);
    for(const result of calls(name,input))assert.ok(!result,`${name}: ${command}: ${result}`);
    assert.equal(JSON.stringify(input),before);
  }
});
test('v3 direct protected operands, ancestors and aliases still refuse in both mounts',t=>{
  const {root,home,calls}=fixture(t);
  fs.mkdirSync(path.join(home,'private'));
  fs.symlinkSync(path.join(home,'private'),path.join(root,'alias'),'junction');
  for(const command of ['cat .env','cat .git/config','rm -rf .model-desk',
    'Remove-Item -LiteralPath .git -Recurse -Force', 'rm -rf .',
    'cat alias/credentials.json',`rm -rf "${home}"`,
    'cat ~/private/auth.json','cd .git && cat config',
    'echo DONE; rm -rf .model-desk']){
    for(const result of calls('Bash',{command}))assert.ok(result,command);
  }
});
test('v3 explicit scope, readonly, project mode, original and other copy checks persist',t=>{
  const {box,root,original,config,calls}=fixture(t);
  for(const [name,args] of [['Write',{file_path:'readonly.txt'}],['Read',{file_path:'.env'}],
    ['Bash',{command:'rm readonly.txt'}],['Bash',{command:'cat ../outside.txt'}],
    ['Bash',{command:`cat "${original}/a.js"`}],['Bash',{command:'node check.js',cwd:original}]]){
    for(const result of calls(name,args))assert.ok(result,JSON.stringify(args));
  }
  const strict={...config,task_policy:{...config.task_policy,write_scope:'declared_files'}};
  for(const result of calls('Bash',{command:'Set-Content extra.js DATA'},strict))assert.ok(result);
  for(const result of calls('Bash',{command:'Set-Content main.js DATA'},strict))assert.ok(!result,result);
  const full={...config,access_mode:'full'};
  for(const result of calls('Bash',{command:'cat ../outside.txt'},full))assert.ok(!result,result);
  for(const result of calls('Bash',{command:`cat "${original}/a.js"`},full))assert.ok(result);
  const context=path.join(box,'other-task-workspaces','b','x');
  for(const result of calls('Read',{file_path:context},full))assert.ok(result);
});
test('unknown syntax does not pause, actual denial pauses and readonly terminal stays denied',t=>{
  const {config}=fixture(t),guard=access.createGuard(config);
  assert.equal(guard('pwsh',{command:'cd "$DEST"; node script.js'}),undefined);
  assert.equal(guard('write',{file_path:'helper.js'}),undefined);
  assert.ok(guard('bash',{command:'rm .model-desk/x'}));
  assert.ok(guard('bash',{command:'node --test'}));
  const filesFirst=access.createGuard(config);
  assert.ok(filesFirst('write',{file_path:'.model-desk/x'}));
  const paused=filesFirst('bash',{command:'node --test'});
  assert.equal(policy.denialRule(paused),'prior_denial');
  const readonly={...config,kind:'chat',task_policy:{...config.task_policy,write_scope:'read_only'}};
  assert.ok(access.createGuard(readonly)('bash',{command:'echo OK'}));
  assert.ok(native.decide(readonly,{tool_name:'Bash',tool_input:{command:'echo OK'}}));
});
test('v2 and legacy retain old command refusals; corrupt v3 never selects relaxed rules',t=>{
  const {config,calls}=fixture(t);
  const old={...config,task_policy:{...config.task_policy,schema:2}};
  for(const result of calls('PowerShell',{command:"Get-ChildItem -Exclude '.git' ."},old))assert.ok(result);
  const legacy={...config};delete legacy.task_policy;
  assert.ok(native.decide(legacy,{tool_name:'Bash',tool_input:{command:'cd "$DEST" && rm x'}}));
  for(const p of [{...config.task_policy,home:'relative'},{...config.task_policy,extra:true},
    {...config.task_policy,schema:4},{...config.task_policy,deny_paths:['relative']}]){
    assert.throws(()=>access.createGuard({...config,task_policy:p}));
  }
});
