// Real component DOM; intercepted in-memory controller, never a workbench/model.
'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {launch}=require('./browser_helpers.cjs');
const web=path.resolve(__dirname,'../panel');
const jointChecks=[['fixture-python','joint_check.py','<probe>'.repeat(100)]];
const stages=Array.from({length:8},(_,i)=>({id:'s'+i,title:'阶段 '+i+' <img src=x onerror="window.pwn=1">',
  objective:'实现并自测当前模块。'.repeat(6),depends_on:i?['s'+(i-1)]:[],acceptance:['正常输入通过','边界输入通过']}));
const document={definition:{goal:{title:'Fixture goal',objective:'Isolated test'},stages},context:{models:Object.fromEntries(stages.map(s=>[s.id,{engine:'pi',model_name:'Fixture model',access_mode:'project'}]))},integration_checks:[]};
const bootstrap=`window.calls=[];window.notices=[];window.currentView={mode:'project',project:'C:/fixture'};let stored=null;
window.ui=ModelDeskApprovedPlan.create({getView:()=>currentView,toast:(...v)=>notices.push(v),api:async(p,b)=>{
 calls.push({path:p,body:b});const action=p.split('/').pop();
 if(action==='preview')return {document:{...${JSON.stringify(document)},integration_checks:b.checks??stored?.document?.integration_checks??[]},digest:'fixed-digest',read_only:true};
 if(action!=='view'){stored={document:{...${JSON.stringify(document)},integration_checks:b.checks||[]},digest:'fixed-digest',revision:1,status:({approve:'approved',pause:'paused',resume:'approved',revoke:'revoked'})[action],observations:{s0:'unknown'}};}
 return {plan:stored,attached:stored?.status==='approved'};
}});ui.open('goal-fixture');`;
(async()=>{
 const browser=await launch();let count=0;
 try{
  for(const width of [390,1100])for(const theme of ['dark','light']){
   const page=await browser.newPage({viewport:{width,height:800},colorScheme:theme}),errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',route=>route.fulfill({status:404,body:''}));
   await page.setContent('<!doctype html><html data-theme="'+theme+'"><head><style>'+fs.readFileSync(path.join(web,'style.css'),'utf8')+'</style></head><body></body></html>');
   await page.addScriptTag({content:fs.readFileSync(path.join(web,'approved-plan.js'),'utf8')});
   await page.addScriptTag({content:bootstrap});
   const dialog=page.locator('#approved-plan-dialog');
   await dialog.locator('[data-plan-consent]').waitFor();
   assert.equal(await dialog.locator('[data-plan-stage]').count(),8);count++;
   assert.equal(await dialog.locator('img').count(),0);count++;
   assert.equal(await page.evaluate(()=>window.pwn),undefined);count++;
   assert.deepEqual(await page.evaluate(()=>calls.map(c=>c.path.split('/').pop())),['view','preview']);count++;
   await dialog.locator('summary').click();
   await dialog.locator('[data-plan-consent]').check();
   await dialog.locator('[data-plan-checks]').fill(JSON.stringify(jointChecks));
   assert.equal(await dialog.locator('[data-plan-action="approve"]').isDisabled(),true);count++;
   assert.equal(await dialog.locator('[data-plan-consent]').isChecked(),false);count++;
   await dialog.locator('[data-plan-action="preview-checks"]').click();
   await dialog.locator('[data-plan-action="approve"]:enabled').waitFor();
   assert.deepEqual(await page.evaluate(()=>calls.filter(c=>c.path.endsWith('/preview')).at(-1).body.checks),jointChecks);count++;
   assert.equal(await dialog.locator('[data-plan-consent]').isChecked(),false);count++;
   await dialog.locator('[data-plan-action="approve"]').click();
   assert.equal(await page.evaluate(()=>calls.filter(c=>c.path.endsWith('/approve')).length),0);count++;
   await dialog.locator('[data-plan-consent]').check();
   await dialog.locator('[data-plan-action="approve"]').click();
   await dialog.locator('[data-plan-action="pause"]').waitFor();
   assert.equal(await page.evaluate(()=>calls.find(c=>c.path.endsWith('/approve')).body.digest),'fixed-digest');count++;
   assert.equal(await page.evaluate(()=>calls.find(c=>c.path.endsWith('/approve')).body.confirm),true);count++;
   assert.deepEqual(await page.evaluate(()=>calls.find(c=>c.path.endsWith('/approve')).body.checks),jointChecks);count++;
   assert.match(await dialog.textContent(),/派发结果未知，禁止重发/);count++;
   assert(await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+2));count++;
   const footer=await dialog.locator('[data-plan-actions]').boundingBox();assert(footer.y+footer.height<=800);count++;
   await dialog.locator('[data-plan-action="pause"]').click();
   await page.waitForFunction(()=>document.querySelector('[data-plan-body]').textContent.includes('已暂停'));
   await dialog.locator('[data-plan-consent]').check();
   await dialog.locator('[data-plan-action="resume"]').click();
   await dialog.locator('[data-plan-action="pause"]').waitFor();
   assert.equal(await page.evaluate(()=>calls.filter(c=>c.path.endsWith('/resume')).length),1);count++;
   await page.evaluate(()=>{currentView={mode:'project',project:'C:/other'};ui.scopeChanged();});
   assert.equal(await dialog.evaluate(e=>e.open),false);count++;
   assert.deepEqual(errors,[]);count++;
   await page.close();
   const brainDoc={...document,definition:{...document.definition,responsibility:{mode:'brain',host:'dsh',owner:'fixture-brain'}}};
   const brainPage=await browser.newPage({viewport:{width,height:800},colorScheme:theme});
   await brainPage.route('**/*',route=>route.fulfill({status:404,body:''}));
   await brainPage.setContent('<!doctype html><html><body></body></html>');
   await brainPage.addScriptTag({content:fs.readFileSync(path.join(web,'approved-plan.js'),'utf8')});
   await brainPage.addScriptTag({content:bootstrap.replaceAll(JSON.stringify(document),JSON.stringify(brainDoc))});
   const brainDialog=brainPage.locator('#approved-plan-dialog');
   await brainDialog.locator('[data-plan-consent]').waitFor();
   assert.match(await brainDialog.textContent(),/责任主脑：dsh \/ fixture-brain/);count++;
   assert.equal(await brainDialog.locator('[data-plan-action="approve"]').textContent(),'批准，等待原主脑续接');count++;
   await brainDialog.locator('[data-plan-consent]').check();
   await brainDialog.locator('[data-plan-action="approve"]').click();
   await brainDialog.locator('[data-plan-action="pause"]').waitFor();
   assert.equal(await brainDialog.locator('[data-plan-action="resume"]').count(),0);count++;
   await brainDialog.locator('[data-plan-action="pause"]').click();
   await brainPage.waitForFunction(()=>document.querySelector('[data-plan-body]').textContent.includes('已暂停'));
   assert.equal(await brainDialog.locator('[data-plan-action="resume"]').textContent(),'解除暂停，等待原主脑续接');count++;
   await brainDialog.locator('[data-plan-consent]').check();
   await brainDialog.locator('[data-plan-action="resume"]').click();
   await brainDialog.locator('[data-plan-action="pause"]').waitFor();
   assert.equal(await brainDialog.locator('[data-plan-action="resume"]').count(),0);count++;
   assert.equal(await brainPage.evaluate(()=>calls.some(c=>c.path.includes('brain_plan'))),false);count++;
   await brainPage.close();
  }
  process.stdout.write(JSON.stringify({checks:count,widths:[390,1100],themes:['dark','light'],real_model_calls:0,real_settings_writes:0})+'\n');
 }finally{await browser.close();}
})().catch(e=>{process.stderr.write(String(e.stack||e)+'\n');process.exitCode=1;});
