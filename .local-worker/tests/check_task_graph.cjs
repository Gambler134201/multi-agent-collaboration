// Actual shared page and Codex inline RPC bundle; synthetic records only.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {launch} = require('./browser_helpers.cjs');
const root = path.resolve(__dirname, '../..'), panel = path.join(root,'.local-worker/panel');
const inline = execFileSync(process.env.MODEL_DESK_PYTHON || 'python',['-c',
  "import sys;sys.path.insert(0,'.local-worker');import codex_mcp_server;print(codex_mcp_server.build_workbench_html())"],
  {cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},maxBuffer:8*1024*1024});
const project = 'E:\\task-graph-fixture', other = 'E:\\empty-graph-fixture';
const summary = (code,label,tone='neutral') => ({schema:1,source:'controller_projection',code,label,tone,
  detail:code==='waiting_dependency'?'页面尚未独立验收，不能进入集成。':'保留真实执行与验收事实。',
  next_step:'由主脑按依赖与授权安排，不自动派发。',facts:[]});
const now = '2026-10-27T00:00:00Z';
const tasks = [
  {id:'api-task',title:'实现收藏接口',project,kind:'edit',engine:'zcode',model_name:'GLM-5.3-Flash',model:'glm-5.3-flash',status:'completed',created_at:now,files:[],changed_files:['api.js'],review:{state:'accepted',acceptance_valid:true},execution_result:{schema_version:1,summary:summary('accepted','已独立验收','success'),stages:[],issues:[],artifacts:{files:[]}}},
  {id:'page-task',title:'实现收藏页面',project,kind:'edit',engine:'workbuddy',model_name:'DeepSeek V4.1 Flash',status:'running',created_at:now,files:[],changed_files:[],execution_result:{schema_version:1,summary:summary('running','正在执行','accent'),stages:[],issues:[],artifacts:{files:[]}}},
  {id:'loose-task',title:'独立调查任务',project,kind:'chat',engine:'pi',status:'completed',files:[],changed_files:[],created_at:now}
];
const mkStage = (id,title,deps,status,s,taskId='') => ({id,title,project,goal_id:'g',kind:'edit',status,
  objective:'完成'+title+'，保留实际测试结果。',depends_on:deps,task_ids:taskId?[taskId]:[],active_task_id:taskId,
  files:['src/'+id+'.js'],acceptance:['行为符合约定且独立检查通过'],readiness:{ready:false,blocked_by:deps,reasons:deps.length?['前置阶段尚未独立验收通过']:[]},
  tasks:taskId?[{task_id:taskId}]:[],delivery:{deliveries:[],warnings:[]},summary:s});
const stages = [
  mkStage('plan','确认需求与接口约定',[],'accepted',summary('accepted','阶段已验收','success')),
  mkStage('api','实现收藏接口',['plan'],'accepted',summary('accepted','阶段已验收','success'),'api-task'),
  mkStage('page','实现收藏页面',['plan'],'running',summary('running','正在执行','accent'),'page-task'),
  mkStage('join','合并接口与页面',['api','page'],'pending',summary('waiting_dependency','等待前置验收')),
  mkStage('review','独立检查整体功能',['join'],'pending',summary('waiting_dependency','等待前置验收')),
  mkStage('delivery','整理交付',['review'],'pending',summary('waiting_dependency','等待前置验收'))
];
stages[1].delivery.deliveries=[{task_id:'api-task',summary:'接口已独立检查',files:[{path:'api.js'}],fresh:{valid:true}}];
const tree = {goals:[{id:'g',project,title:'给网站增加文章收藏功能',status:'active',stages},
  {id:'history',project,title:'历史取消目标',status:'cancelled',stages:[mkStage('cancelled','取消的阶段',[],'cancelled',summary('cancelled','已停止'))]}]};
const fixture = {models:[],tasks,default_model:null,workspace:project,workflow:{enabled:true,readable:true,model_scope:'all',daily:{}},
  delegation:{delegated:2,executor_completed:1,accepted:1,returned:0},agent:{ready:false},codex_usage:{status:'unknown',buckets:[]},execution_endpoints:[]};
function response(apiPath, view) {
  const pathname=apiPath.split('?')[0], empty=view?.project===other;
  if(pathname==='/api/session')return {token:'synthetic-only'};
  if(pathname==='/api/state')return {...fixture,tasks:empty?[]:tasks,view:view||{mode:'project',project}};
  if(pathname==='/api/tree')return empty?{goals:[]}:tree;
  if(pathname==='/api/observability')return {tasks:[],stats:{}};
  if(pathname.startsWith('/api/tasks/'))return {task:tasks.find(t=>pathname.includes(t.id)),prompt:'合成任务'};
  return {};
}
function hostHtml() {
  return '<!doctype html><meta charset="utf-8"><style>body{margin:0}iframe{width:100%;height:100vh;border:0}</style><iframe src="/inline"></iframe><script>' +
    'const project='+JSON.stringify(project)+',other='+JSON.stringify(other)+',fixture='+JSON.stringify(fixture)+',tree='+JSON.stringify(tree)+';const tasks=fixture.tasks;'+response.toString()+
    ';window.__calls=[];window.__failTree=false;window.addEventListener("message",e=>{const m=e.data;if(!m||m.id===undefined)return;let result={};if(m.method==="ui/initialize"){e.source.postMessage({jsonrpc:"2.0",method:"ui/notifications/tool-input",params:{arguments:{project}}},"*");}else if(m.method==="tools/call"){const a=m.params.arguments;window.__calls.push({path:a.path,method:a.method});result={structuredContent:window.__failTree&&a.path==="/api/tree"?{status:503,data:{error:"fixture tree offline"}}:{status:200,data:response(a.path,a.view)}};}e.source.postMessage({jsonrpc:"2.0",id:m.id,result},"*");});</script>';
}
(async()=>{
  const browser=await launch();let checks=0;
  try {
    for(const host of ['dsh','codex'])for(const width of [320,1100])for(const theme of ['dark','light']) {
      const page=await browser.newPage({viewport:{width,height:1000},colorScheme:theme}), errors=[],calls=[],missing=[];
      let failTree=false;
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/*',async route=>{
        const req=route.request(),url=new URL(req.url());
        if(url.pathname==='/host')return route.fulfill({contentType:'text/html',body:hostHtml()});
        if(url.pathname==='/inline')return route.fulfill({contentType:'text/html',body:inline});
        if(url.pathname.startsWith('/api/')){
          calls.push({path:url.pathname,method:req.method()});
          if(failTree&&url.pathname==='/api/tree')return route.fulfill({status:503,json:{error:'fixture tree offline'}});
          const header=req.headers()['x-model-desk-view'];
          const view=header?JSON.parse(Buffer.from(header,'base64url').toString('utf8')):undefined;
          return route.fulfill({json:response(url.pathname,view)});
        }
        const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
        if(/^[a-z-]+\.(html|js|css)$/.test(name)&&fs.existsSync(path.join(panel,name)))return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(panel,name),'utf8')});
        missing.push(url.pathname);return route.fulfill({status:404,body:''});
      });
      await page.goto(host==='codex'?'http://fixture.invalid/host':'http://fixture.invalid/?host=dsh&project='+encodeURIComponent(project));
      const scope=host==='codex'?page.frameLocator('iframe'):page;
      const evaluate=async fn=>scope.locator('body').evaluate(fn);
      await scope.locator('#task-list .task-row').first().waitFor();
      await evaluate(async()=>{await refresh();stopAutoPoll();});
      const initialCalls=host==='codex'?await page.evaluate(()=>window.__calls.length):calls.length;
      await scope.locator('#task-graph-open').click();
      const dialog=scope.locator('#task-graph-dialog');
      assert.equal(await dialog.locator('.tg-node').count(),6);checks++;
      await dialog.locator('.tg-wires g path').first().waitFor({state:'attached'});
      assert.equal(await dialog.locator('.tg-wires g path').count(),6);checks++;
      await dialog.locator('[data-tg-node="join"]').click();
      assert.match(await dialog.locator('.tg-detail').innerText(),/页面尚未独立验收/);checks++;
      await dialog.locator('[data-tg-jump="api"]').click();
      assert.equal(await dialog.locator('.tg-detail h3').innerText(),'实现收藏接口');checks++;
      assert.match(await dialog.locator('.tg-detail').innerText(),/ZCode · GLM-5.3-Flash/);checks++;
      assert.match(await dialog.locator('.tg-detail').innerText(),/交接依据当前有效/);checks++;
      const afterClicks=host==='codex'?await page.evaluate(()=>window.__calls.length):calls.length;
      assert.equal(afterClicks,initialCalls,'graph selection and opening must issue no request');checks++;
      await dialog.locator('.tg-handover [data-tg-task="api-task"]').click();
      assert.equal(await dialog.evaluate(e=>e.open),false);checks++;
      assert.match(await scope.locator('#task-detail').innerText(),/实现收藏接口/);checks++;
      await scope.locator('#task-graph-open').click();
      const originalNode=await dialog.locator('[data-tg-node="plan"]').elementHandle();
      await evaluate(()=>taskGraphUI.update());
      assert(await originalNode.evaluate(e=>e.isConnected));checks++;
      await evaluate(()=>{
        const current=state.tree.goals[0].stages.find(s=>s.id==='page');
        current.status='executor_completed';
        current.summary={schema:1,source:'controller_projection',code:'awaiting_review',label:'等待独立验收',tone:'warning',detail:'执行结束但尚未验收',next_step:'主脑独立检查'};
        taskGraphUI.update();
      });
      assert.match(await dialog.locator('[data-tg-node="page"]').innerText(),/等待独立验收/);checks++;
      assert.match(await dialog.locator('.tg-overview').innerText(),/2 \/ 6 已验收/);checks++;
      if(host==='codex')await page.evaluate(()=>{window.__failTree=true;}); else failTree=true;
      await evaluate(async()=>{await refresh();stopAutoPoll();});
      assert.match(await dialog.locator('.tg-error').innerText(),/最近读取内容/);checks++;
      assert.equal(await dialog.locator('.tg-node').count(),6);checks++;
      if(host==='codex')await page.evaluate(()=>{window.__failTree=false;}); else failTree=false;
      await evaluate(async()=>{await refresh();stopAutoPoll();});
      assert(await dialog.locator('.tg-error').isHidden());checks++;
      await dialog.locator('#tg-goal').selectOption('history');
      assert.match(await dialog.locator('.tg-overview').innerText(),/目标已取消/);checks++;
      await dialog.locator('#tg-goal').selectOption('g');
      await dialog.locator('[data-tg-node="join"]').click();
      // No model is invoked for long titles or narrow/font-scaled layouts.
      await evaluate(()=>{document.documentElement.dataset.fontScale='xlarge';});
      const bounds=await dialog.boundingBox();
      assert(bounds.x>=0&&bounds.x+bounds.width<=width+1,JSON.stringify(bounds));checks++;
      const fits=await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+1);assert(fits);checks++;
      await evaluate(()=>{
        const current=state.tree.goals[0].stages.find(s=>s.id==='join');
        current.title='<img src=x onerror="window.graphXss=1">'+ '很长的阶段标题'.repeat(30);
        current.objective='LongUnbrokenText'.repeat(70);current.files=['longpath'.repeat(120)];
        taskGraphUI.update();
      });
      assert.equal(await dialog.locator('img').count(),0);checks++;
      assert(await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+1));checks++;
      assert.equal(await evaluate(()=>window.graphXss),undefined);checks++;
      await evaluate(async()=>{await refresh();stopAutoPoll();});
      await evaluate(()=>{document.documentElement.dataset.fontScale='standard';});
      if(process.env.MODEL_DESK_QA_DIR&&width===1100&&theme==='dark') {
        fs.mkdirSync(process.env.MODEL_DESK_QA_DIR,{recursive:true});
        await dialog.screenshot({path:path.join(process.env.MODEL_DESK_QA_DIR,'task-graph-'+host+'.png')});
      }
      await evaluate(()=>{changeProjectView({mode:'project',project:'E:\\empty-graph-fixture'});});
      assert.equal(await dialog.evaluate(e=>e.open),false);checks++;
      await evaluate(async()=>{await refresh();stopAutoPoll();});
      await scope.locator('#task-graph-open').click();
      assert.equal(await dialog.locator('.tg-node').count(),0);checks++;
      assert(!((await dialog.innerText()).includes('实现收藏接口')));checks++;
      await dialog.locator('.tg-close').click();
      await evaluate(()=>{changeProjectView({mode:'all',project:null});stopAutoPoll();});
      assert(await scope.locator('#task-graph-open').isDisabled());checks++;
      const rpc=host==='codex'?await page.evaluate(()=>window.__calls):[];
      const writes=[...calls,...rpc].filter(c=>c.method!=='GET');
      assert(writes.every(c=>c.path==='/api/projects/remember'),JSON.stringify(writes));checks++;
      assert.deepEqual(errors,[]);assert.deepEqual(missing,[]);checks+=2;
      await page.close();
    }
    console.log(JSON.stringify({checks,hosts:['codex-inline-rpc-mock','dsh-browser-mock'],widths:[320,1100],themes:['dark','light'],real_model_calls:0,graph_network_calls:0,task_mutations:0}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
