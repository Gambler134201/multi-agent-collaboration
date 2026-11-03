// Real shared UI and inline RPC bundle. All API data and actions are intercepted.
'use strict';
const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');
const {execFileSync}=require('node:child_process');const {launch}=require('./browser_helpers.cjs');
const root=path.resolve(__dirname,'../..'),panel=path.join(root,'.local-worker/panel');
const inline=execFileSync(process.env.MODEL_DESK_PYTHON||'python',['-c',"import sys;sys.path.insert(0,'.local-worker');import codex_mcp_server;print(codex_mcp_server.build_workbench_html())"],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},maxBuffer:8*1024*1024});
const project='E:\\integration-ui-fixture',other='E:\\empty-integration-fixture';
const states=['queued','checking','awaiting_review','conflict','recovery_required','accepted'];
const records=Object.fromEntries(states.map((state,i)=>['job-'+i,{schema:1,kind:'integration-acceptance',name:'job-'+i,project,
 baseline:'a'.repeat(40),integrated_commit:i>1?'b'.repeat(40):null,task_ids:['task'],commits:{task:'c'.repeat(40)},
 accepted:state==='accepted',verdict:i===2||i===5?'passed':i===3?'merge_conflict':null,
 queue:{schema:1,state,requires_authority:i===4,detail:'集成状态按持久记录显示。',check_commands:[['node','--test']],enqueued_at:'fixture',updated_at:'fixture'},
 checks:i===2||i===5?[{command:['node','--test'],ok:true,exit_code:0,output_tail:'PASS'}]:[],
 integration_copy:{path:'E:\\isolated\\copy'},merges:i===3?[{conflicts:['conflicted.js']}]:[]} ]));
const recoveries={'job-2':{state:'ready',can_settle:true,detail:'完整结果已保存，原执行进程已退出。'}};
const task={id:'task',project,title:'已验收模块',kind:'edit',engine:'pi',model_name:'fixture',status:'completed',created_at:'2026-10-27T00:00:00Z',files:[],changed_files:[],
 review:{state:'accepted',acceptance_valid:true},execution_result:{schema_version:1,stages:[],issues:[],artifacts:{files:[]},integration:{state:'awaiting_review',records:[{name:'job-2',state:'awaiting_review'}]},
 summary:{schema:1,source:'controller_projection',code:'accepted',label:'已独立验收',tone:'success'}}};
const tree={goals:[{id:'g',title:'合成目标',status:'active',project,stages:[{id:'s',project,title:'已验收阶段',status:'accepted',depends_on:[],task_ids:['task'],active_task_id:'task',tasks:[{task_id:'task'}],files:[],delivery:{deliveries:[],warnings:[]},summary:{schema:1,source:'controller_projection',code:'accepted',label:'阶段已验收'}}]}]};
const fixture={models:[],tasks:[task],default_model:null,workspace:project,workflow:{enabled:true,readable:true,model_scope:'all',daily:{}},delegation:{delegated:1,accepted:1},agent:{ready:false},codex_usage:{status:'unknown',buckets:[]},execution_endpoints:[]};
function response(apiPath,view){
 const name=apiPath.split('?')[0],empty=view?.project===other;
 if(name==='/api/session')return {token:'synthetic-only'};
 if(name==='/api/state')return {...fixture,view:view||{mode:'project',project},tasks:empty?[]:[task],integrations:{available:true,records:empty?[]:Object.values(records).map(r=>({name:r.name,state:r.queue.state,task_ids:r.task_ids,updated_at:r.queue.updated_at,check_count:r.checks.length,requires_owner:r.queue.requires_authority,recovery:recoveries[r.name]}))}};
 if(name==='/api/tree')return empty?{goals:[]}:tree;
 if(name==='/api/observability')return {tasks:[],stats:{}};
 const match=name.match(/^\/api\/integrations\/(job-\d)(?:\/(resume|cancel|reconcile))?$/);
 if(match){const row=records[match[1]];if(match[2]==='cancel'){row.queue.state='cancelled';row.queue.updated_at='changed';}if(match[2]==='reconcile'){recoveries[match[1]]={state:'settled',can_settle:false,detail:'本作业收尾已确认。'};}return {integration:row,state:row.queue.state,recovery:recoveries[row.name],read_only:true};}
 if(name.startsWith('/api/tasks/'))return {task,prompt:'合成任务'};
 return {};
}
function hostHtml(){return '<!doctype html><style>body{margin:0}iframe{width:100%;height:100vh;border:0}</style><iframe src="/inline"></iframe><script>'+ 'const project='+JSON.stringify(project)+',other='+JSON.stringify(other)+',fixture='+JSON.stringify(fixture)+',records='+JSON.stringify(records)+',recoveries='+JSON.stringify(recoveries)+',tree='+JSON.stringify(tree)+',task=fixture.tasks[0];'+response.toString()+';window.__calls=[];window.addEventListener("message",e=>{const m=e.data;if(!m||m.id===undefined)return;let result={};if(m.method==="ui/initialize"){e.source.postMessage({jsonrpc:"2.0",method:"ui/notifications/tool-input",params:{arguments:{project}}},"*");}else if(m.method==="tools/call"){const a=m.params.arguments;window.__calls.push({path:a.path,method:a.method});result={structuredContent:{status:200,data:response(a.path,a.view)}};}e.source.postMessage({jsonrpc:"2.0",id:m.id,result},"*");});</script>';}
(async()=>{
 const browser=await launch();let checks=0;
 try{
  for(const host of ['codex','dsh'])for(const width of [320,1100])for(const theme of ['dark','light']){
   recoveries['job-2']={state:'ready',can_settle:true,detail:'完整结果已保存，原执行进程已退出。'};
   const localRecords=structuredClone(records);const page=await browser.newPage({viewport:{width,height:1000},colorScheme:theme}),errors=[],calls=[],missing=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url());
    if(url.pathname==='/host')return route.fulfill({contentType:'text/html; charset=utf-8',body:hostHtml()});
    if(url.pathname==='/inline')return route.fulfill({contentType:'text/html; charset=utf-8',body:inline});
    if(url.pathname.startsWith('/api/')){
     calls.push({path:url.pathname,method:req.method()});
     const encoded=req.headers()['x-model-desk-view'];const view=encoded?JSON.parse(Buffer.from(encoded,'base64url').toString('utf8')):undefined;
     // The HTTP fixture owns its own record copy, independent of other pages.
     const old=Object.fromEntries(Object.entries(records));Object.assign(records,localRecords);
     const result=response(url.pathname,view);Object.assign(records,old);
     return route.fulfill({json:result});
    }
    const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if(/^[a-z-]+\.(html|js|css)$/.test(name)&&fs.existsSync(path.join(panel,name)))return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(panel,name),'utf8')});
    missing.push(url.pathname);return route.fulfill({status:404,body:''});
   });
   await page.goto(host==='codex'?'http://fixture.invalid/host':'http://fixture.invalid/?host=dsh&project='+encodeURIComponent(project));
   const scope=host==='codex'?page.frameLocator('iframe'):page;
   const evaluate=fn=>scope.locator('body').evaluate(fn);
   await scope.locator('#task-list .task-row').first().waitFor();await evaluate(async()=>{await refresh();stopAutoPoll();});
   await scope.locator('#integration-queue-open').click();const dialog=scope.locator('#integration-queue-dialog');
   await dialog.locator('[data-iq-action="cancel"]').waitFor();
   assert.match(await dialog.locator('.iq-detail').innerText(),/集成状态按持久记录显示/);checks++;
   assert.equal(await dialog.locator('.iq-row').count(),6);checks++;
   for(let i=0;i<6;i++){await dialog.locator('[data-iq-name="job-'+i+'"]').click();await dialog.locator('.iq-detail h3').filter({hasText:'job-'+i}).waitFor();assert.match(await dialog.locator('.iq-detail').innerText(),new RegExp(['等待集成','正在联合检查','等待集成验收','集成存在冲突','集成结果待核对','集成已验收'][i]));checks++;}
   await dialog.locator('[data-iq-name="job-2"]').click();await dialog.locator('[data-iq-action="reconcile"]').waitFor();
   assert.match(await dialog.locator('[data-iq-name="job-2"]').innerText(),/收尾待核对/);checks++;
   assert.match(await dialog.locator('.iq-detail').innerText(),/完整结果已保存/);checks++;
   page.once('dialog',d=>d.accept());await dialog.locator('[data-iq-action="reconcile"]').click();
   await dialog.locator('.iq-detail').filter({hasText:'本作业收尾已确认'}).waitFor();
   assert.equal(await dialog.locator('[data-iq-action]').count(),0);checks++;
   assert.match(await dialog.locator('.iq-status').innerText(),/等待集成验收/);checks++;
   assert.equal(records['job-2'].accepted,false);checks++;
   await evaluate(async()=>{await refresh();stopAutoPoll();});
   assert(!((await dialog.locator('[data-iq-name="job-2"]').innerText()).includes('收尾待核对')));checks++;
   await dialog.locator('[data-iq-name="job-4"]').click();await dialog.locator('.iq-detail h3').filter({hasText:'job-4'}).waitFor();
   assert.equal(await dialog.locator('[data-iq-action]').count(),0);checks++;
   await dialog.locator('[data-iq-name="job-0"]').click();await dialog.locator('[data-iq-action="cancel"]').waitFor();
   page.once('dialog',d=>d.accept());await dialog.locator('[data-iq-action="cancel"]').click();
   await dialog.locator('.iq-status').filter({hasText:'集成已取消'}).waitFor();checks++;
   await evaluate(async()=>{await refresh();stopAutoPoll();});assert.match(await dialog.locator('[data-iq-name="job-0"]').innerText(),/集成已取消/);checks++;
   await evaluate(()=>{document.documentElement.dataset.fontScale='xlarge';});
   const b=await dialog.boundingBox();assert(b.x>=0&&b.x+b.width<=width+1);assert(await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+1));checks+=2;
   await evaluate(()=>{document.documentElement.dataset.fontScale='standard';});
   await dialog.locator('.iq-close').click();
   await scope.locator('#task-graph-open').click();await scope.locator('[data-tg-integration="job-2"]').click();
   await dialog.locator('.iq-detail h3').filter({hasText:'job-2'}).waitFor();checks++;
   assert.equal(await scope.locator('#task-graph-dialog').evaluate(e=>e.open),false);checks++;
   if(process.env.MODEL_DESK_QA_DIR&&width===1100&&theme==='dark'){fs.mkdirSync(process.env.MODEL_DESK_QA_DIR,{recursive:true});await dialog.screenshot({path:path.join(process.env.MODEL_DESK_QA_DIR,'integration-queue-'+host+'.png')});}
   await evaluate(()=>{changeProjectView({mode:'project',project:'E:\\empty-integration-fixture'});});assert.equal(await dialog.evaluate(e=>e.open),false);checks++;
   await evaluate(async()=>{await refresh();stopAutoPoll();});await scope.locator('#integration-queue-open').click();
   assert.match(await dialog.locator('.iq-list').innerText(),/没有集成记录/);assert(!((await dialog.innerText()).includes('job-2')));checks+=2;
   const rpc=host==='codex'?await page.evaluate(()=>window.__calls):[];
   const writes=[...calls,...rpc].filter(c=>c.method!=='GET');assert.equal(writes.filter(c=>c.path.endsWith('/cancel')).length,1);assert.equal(writes.filter(c=>c.path.endsWith('/reconcile')).length,1);assert(writes.every(c=>c.path==='/api/projects/remember'||c.path==='/api/integrations/job-0/cancel'||c.path==='/api/integrations/job-2/reconcile'));checks+=3;
   assert.deepEqual(errors,[]);assert.deepEqual(missing,[]);checks+=2;
   await page.close();
  }
  console.log(JSON.stringify({checks,hosts:['codex-inline-rpc-mock','dsh-browser-mock'],widths:[320,1100],themes:['dark','light'],real_model_calls:0,real_mutations:0}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
