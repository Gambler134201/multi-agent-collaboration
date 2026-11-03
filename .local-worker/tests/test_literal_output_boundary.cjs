'use strict';
// Pure guard calls only: none of these command strings is executed.
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const policy=require('../agent-runtime/record-policy.cjs');
const access=require('../agent-runtime/endpoint-access.cjs');
const native=require('../agent-runtime/workbuddy-native-guard.cjs');

function fixture(t){
  const box=fs.mkdtempSync(path.join(os.tmpdir(),'md-output-boundary-'));
  t.after(()=>fs.rmSync(box,{recursive:true,force:true}));
  const original=path.join(box,'project'),copy=path.join(box,'project-task-workspaces','task');
  fs.mkdirSync(path.join(original,'.model-desk'),{recursive:true});
  fs.mkdirSync(path.join(copy,'.model-desk'),{recursive:true});
  const job={project:copy,record_project:original,kind:'edit',access_mode:'project',
    tools:['Bash','PowerShell'],files:['main.js'],readonly_files:[],protected_roots:[original]};
  return {original,copy,job};
}

test('literal output payloads are data across record, project and native guards',t=>{
  const {original,copy,job}=fixture(t);
  for(const command of ['echo "rm -rf ."','Write-Output "git commit and .model-desk"',
    'echo "rm -rf *"','echo "C:\\project\\.model-desk"',
    'Write-Host "auth-token .env .zcode"',`echo "${original}"`,
    'echo "a; rm -rf . | cat .model-desk/records.db"',
    'echo "Remove-Item . -Recurse"']){
    assert.equal(policy.decide('bash',{command},{root:copy}),undefined,command);
    for(const name of ['bash','pwsh','powershell'])
      assert.equal(access.createGuard({root:copy,record_project:original,access_mode:'project'})(name,{command}),undefined,command);
    for(const name of job.tools)
      assert.equal(native.decide(job,{tool_name:name,tool_input:{command}}),null,command);
  }
});

test('output does not hide actual commands, expansion, redirection or changed cwd',t=>{
  const {original,copy,job}=fixture(t);
  for(const command of ['echo OK && rm -rf .','echo OK; Remove-Item . -Recurse',
    'echo OK | Set-Content .model-desk/x','echo OK > .model-desk/x',
    'echo "$(cat .model-desk/records.db)"','echo "$env:MODEL_DESK" .model-desk',
    'echo "%MODEL_DESK%" .model-desk','echo "!MODEL_DESK!" .model-desk',
    'echo `cat .model-desk/records.db`','echo "unterminated .model-desk',
    'echo "x"rm .model-desk','echo OK\nrm -rf .',
    `echo OK && cat "${original}/secret.txt"`]){
    assert.ok(native.decide(job,{tool_name:'Bash',tool_input:{command}}),command);
  }
  for(const tool_name of job.tools){
    assert.ok(native.decide(job,{tool_name,tool_input:{command:'echo OK',cwd:original}}));
    assert.ok(native.decide(job,{tool_name,tool_input:{command:'echo OK',run_in_background:true}}));
    assert.ok(native.decide(job,{tool_name,tool_input:{command:'echo OK',disableSandbox:true}}));
  }
  assert.ok(policy.decide('bash',{command:'echo OK',cwd:copy,workdir:original},{root:copy}));
});

test('literal output never unlocks a stopped guard or adds terminal authority',t=>{
  const {copy,job}=fixture(t);
  const guard=access.createGuard({root:copy,access_mode:'project'});
  assert.ok(guard('bash',{command:'rm -rf .'}));
  assert.ok(guard('bash',{command:'echo OK'}));
  assert.equal(native.decide({...job,kind:'chat'},{tool_name:'Bash',tool_input:{command:'echo OK'}}),'只读任务没有终端权限');
  assert.equal(native.decide({...job,tools:[]},{tool_name:'Bash',tool_input:{command:'echo OK'}}),'本任务未授权此原生工具');
});

test('unrecognized syntax is returned byte-for-byte rather than guessed safe',()=>{
  for(const command of [String.raw`echo "quoted\"; rm -rf ."`,
    'echo "a""b"','echo "a"suffix','echo $VALUE','echo %VALUE%',
    'echo !VALUE!','echo ^& del .','echo a\\; rm -rf .',
    'echo *','echo ~','echo @(Remove-Item .)','echo { Remove-Item . }',
    'echo OK && echo DONE','echo OK | cat','echo OK > output.txt',
    "echo 'single quoted'",'echo OK\r\nrm -rf .','printf ".model-desk"',
    'echo '+ 'a'.repeat(16384),'echo `whoami`'])
    assert.equal(policy.terminalAnalysis(command),command,command.slice(0,100));
});
