'use strict';
const assert=require('node:assert/strict'),fs=require('fs'),os=require('os'),path=require('path');
const {createGuard}=require('../agent-runtime/endpoint-access.cjs');
const box=fs.mkdtempSync(path.join(os.tmpdir(),'endpoint-access-'));
try{
  const root=path.join(box,'project');fs.mkdirSync(root);
  fs.mkdirSync(path.join(root,'.model-desk'));
  for(const name of ['read','write','edit','grep','find','ls']){
    assert.equal(createGuard({root,access_mode:'project'})(name,{path:'src/file.txt'}),undefined);
    assert.match(createGuard({root,access_mode:'project'})(name,{path:'../outside.txt'}),/项目外/);
    assert.equal(createGuard({root,access_mode:'full'})(name,{path:'../outside.txt'}),undefined);
  }
  for(const name of ['bash','pwsh','powershell']){
    assert.match(createGuard({root,access_mode:'project'})(name,{command:'cat ../outside.txt'}),/项目外/);
    assert.match(createGuard({root,access_mode:'project'})(name,{cwd:box,command:'pwd'}),/项目外/);
    assert.equal(createGuard({root,access_mode:'project'})(name,{command:'python -m unittest'}),undefined);
    assert.match(createGuard({root,access_mode:'project'})(name,{command:'cd .. && echo fixture'}),/项目外/);
    assert.equal(createGuard({root,access_mode:'full'})(name,{command:'cd .. && echo fixture'}),undefined);
  }
  for(const mode of ['full','project']){
    assert.match(createGuard({root,access_mode:mode})('write',{file_path:'.model-desk/sentinel'}),/工作台/);
  }
  const guard=createGuard({root,access_mode:'project'});
  guard('read',{path:'../outside'});
  assert.match(guard('bash',{command:'echo retry'}),/不要换工具/);
  const execution=path.join(box,'project-task-workspaces','task-a');fs.mkdirSync(execution,{recursive:true});
  const isolated=()=>createGuard({root:execution,record_project:root,access_mode:'full'});
  const context=path.join(root,'.model-desk','runs','task-a','context');
  for(const mode of ['project','full']){
    const own=()=>createGuard({root:execution,record_project:root,context_root:context,access_mode:mode});
    assert.equal(own()('read',{path:path.join(context,'project-index.json')}),undefined);
    assert.match(own()('read',{path:path.join(context,'..','..','task-b','context','project-index.json')}),/任务隔离/);
    assert.match(own()('write',{path:path.join(context,'project-index.json')}),/工作台|任务隔离/);
    assert.match(own()('bash',{command:'cat "'+path.join(context,'project-index.json')+'"'}),/任务隔离|工作台/);
  }
  for(const name of ['read','write','edit','grep','find','ls']){
    assert.equal(isolated()(name,{path:'own.txt'}),undefined);
    assert.match(isolated()(name,{path:path.join(root,'ordinary.txt')}),/任务隔离/);
    assert.match(isolated()(name,{path:'../task-b/delivery.txt'}),/任务隔离/);
    assert.equal(isolated()(name,{path:path.join(box,'temporary.png')}),undefined);
  }
  for(const name of ['bash','pwsh','powershell']){
    assert.match(isolated()(name,{command:'cat "'+path.join(root,'ordinary.txt')+'"'}),/任务隔离/);
    assert.match(isolated()(name,{command:'cat ../task-b/delivery.txt'}),/任务隔离/);
    assert.match(isolated()(name,{command:'cd ../task-b && cat delivery.txt'}),/任务隔离/);
    assert.equal(isolated()(name,{command:'python -m unittest'}),undefined);
  }
  fs.mkdirSync(path.join(box,'outside'));
  fs.symlinkSync(path.join(box,'outside'),path.join(root,'alias'),'junction');
  assert.match(createGuard({root,access_mode:'project'})('read',{path:'alias/file.txt'}),/项目外/);
  console.log('ok - Pi/DSH access guard: file targets, terminal paths, junctions, full access, records and pause');
}finally{fs.rmSync(box,{recursive:true,force:true});}
