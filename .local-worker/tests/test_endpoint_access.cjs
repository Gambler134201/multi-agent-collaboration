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
  }
  for(const mode of ['full','project']){
    assert.match(createGuard({root,access_mode:mode})('write',{file_path:'.model-desk/sentinel'}),/工作台/);
  }
  const guard=createGuard({root,access_mode:'project'});
  guard('read',{path:'../outside'});
  assert.match(guard('bash',{command:'echo retry'}),/不要换工具/);
  fs.mkdirSync(path.join(box,'outside'));
  fs.symlinkSync(path.join(box,'outside'),path.join(root,'alias'),'junction');
  assert.match(createGuard({root,access_mode:'project'})('read',{path:'alias/file.txt'}),/项目外/);
  console.log('ok - Pi/DSH access guard: file targets, terminal paths, junctions, full access, records and pause');
}finally{fs.rmSync(box,{recursive:true,force:true});}
