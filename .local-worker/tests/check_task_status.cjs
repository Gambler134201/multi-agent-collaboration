// Real shared bundle against synthetic data only: no running workbench/accounts.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {launch} = require('./browser_helpers.cjs');
const root = path.resolve(__dirname, '../..');
const panel = path.join(root, '.local-worker/panel');
const inline = execFileSync(process.env.MODEL_DESK_PYTHON || 'python', ['-c',
  "import sys;sys.path.insert(0,'.local-worker');import codex_mcp_server;print(codex_mcp_server.build_workbench_html())"],
  {cwd: root, encoding:'utf8', env:{...process.env,PYTHONIOENCODING:'utf-8'}, maxBuffer:8*1024*1024});
const project = 'E:\\status-fixture';
const now = '2026-10-27T00:00:00Z';
const cases = [
  ['awaiting_review','等待独立验收','warning','completed'],
  ['waiting_resource','等待执行资源','neutral','queued'],
  ['outcome_unknown','结果未知，等待核对','warning','failed'],
  ['cleanup_attention','收尾待确认','warning','failed'],
  ['integration_awaiting_review','等待集成验收','warning','completed'],
  ['accepted','已独立验收','success','completed'],
  ['review_unrecorded','执行结束，验收未记录','neutral','completed']
];
const tasks = cases.map(([code,label,tone,status], i) => ({
  id:'status-'+i, title:label, kind:'edit', engine:'zcode', status,
  project, model_name:'演示模型', model:'fixture', created_at:now,
  ended_at:status==='queued'?null:now, phase:'旧的自由文本：执行完成',
  files:[], changed_files:[], source:'codex',
  controller_finalization:{schema:1,state:code==='cleanup_attention'?'attention':'settled'},
  review:{state:[4,5].includes(i)?'accepted':'executor_completed',label:'真实验收记录',acceptance_valid:[4,5].includes(i)},
  execution_result:{schema_version:1, read_only:true, stages:[],issues:[],
    execution:{status,engine:'zcode'}, review:{state:[4,5].includes(i)?'accepted':null,accepted:[4,5].includes(i)},
    controller_finalization:{state:code==='cleanup_attention'?'attention':'settled'},
    artifacts:{state:'unchanged',files:[],scope_label:'未记录文件变化',attribution:'observed_only'},
    integration:i===4?{state:'awaiting_review',detail:'隔离副本通过检查；原项目未被覆盖。',records:[{name:'batch.1',state:'awaiting_review'}]}:{state:'none',records:[]},
    summary:{schema:1,source:'controller_projection',code,label,tone,
      detail:'执行、验收、收尾与集成分别核对。',next_step:'由主脑检查后记录下一步，不自动重派。',raw_status:status,
      facts:[{label:'执行',value:status==='completed'?'执行结束':'原始执行记录保留'},
        {label:'验收',value:[4,5].includes(i)?'独立验收通过':'尚未独立验收'},
        {label:'收尾',value:code==='cleanup_attention'?'待核对':'已确认'},{label:'集成',value:i===4?'等待集成验收':'未记录集成'}]}}
}));
const fixture = {models:[],tasks,default_model:null,workspace:project,
  workflow:{enabled:true,readable:true,model_scope:'all',daily:{}},
  delegation:{delegated:7,executor_completed:4,accepted:1,returned:0},
  agent:{ready:false},codex_usage:{status:'unknown',buckets:[]},execution_endpoints:[],stages:{stats:{goals:1}}};
const stageSummary={schema:1,source:'controller_projection',code:'waiting_dependency',label:'等待前置验收',tone:'neutral',detail:'前置阶段尚未验收。',next_step:'先验收前置阶段。',facts:[]};
const tree={goals:[{id:'goal',title:'演示目标',status:'active',stages:[{id:'stage',title:'后续阶段',status:'pending',ordinal:2,depends_on:['previous'],readiness:{ready:false},delivery:{deliveries:[],warnings:[]},summary:stageSummary,tasks:[]}]}]};
function response(apiPath, view) {
  const pathname=apiPath.split('?')[0];
  if(pathname==='/api/session')return {token:'synthetic-only'};
  if(pathname==='/api/state')return {...fixture,view:view||{mode:'project',project}};
  if(pathname==='/api/tree')return tree;
  if(pathname==='/api/observability')return {tasks:[],stats:{}};
  if(pathname.endsWith('/observability'))return {};
  if(pathname.startsWith('/api/tasks/'))return {task:tasks.find(t=>pathname.includes(t.id)),prompt:'合成演示任务'};
  return {};
}
function hostHtml() {
  // The same bridge used by Codex, with a test-only JSON-RPC host in the parent.
  return '<!doctype html><meta charset="utf-8"><style>body{margin:0}iframe{width:100%;height:100vh;border:0}</style>'+
    '<iframe src="/inline"></iframe><script>const project='+JSON.stringify(project)+';const fixture='+JSON.stringify(fixture)+';const tree='+JSON.stringify(tree)+';'+response.toString()+
    ';const tasks=fixture.tasks;window.addEventListener("message",e=>{const m=e.data;if(!m||m.id===undefined)return;let result={};if(m.method==="ui/initialize"){e.source.postMessage({jsonrpc:"2.0",method:"ui/notifications/tool-input",params:{arguments:{project}}},"*");}else if(m.method==="tools/call"){const a=m.params.arguments;window.__calls.push({path:a.path,method:a.method});result={structuredContent:{status:200,data:response(a.path,a.view)}};}e.source.postMessage({jsonrpc:"2.0",id:m.id,result},"*");});window.__calls=[];</script>';
}
(async()=>{
  const browser=await launch();let checks=0;
  try {
    for(const host of ['dsh','codex'])for(const width of [320,1100])for(const theme of ['dark','light']) {
      const page=await browser.newPage({viewport:{width,height:950},colorScheme:theme});
      const errors=[],mutations=[],missing=[];
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/*',async route=>{
        const req=route.request(),url=new URL(req.url());
        if(req.method()!=='GET')mutations.push(req.method()+' '+url.pathname);
        if(url.pathname==='/host')return route.fulfill({contentType:'text/html',body:hostHtml()});
        if(url.pathname==='/inline')return route.fulfill({contentType:'text/html',body:inline});
        if(url.pathname.startsWith('/api/'))return route.fulfill({json:response(url.pathname)});
        const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
        if(/^[a-z-]+\.(html|js|css)$/.test(name)&&fs.existsSync(path.join(panel,name)))
          return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(panel,name),'utf8')});
        missing.push(url.pathname);return route.fulfill({status:404,body:''});
      });
      await page.goto(host==='codex'?'http://fixture.invalid/host':'http://fixture.invalid/?host=dsh&project='+encodeURIComponent(project));
      const scope=host==='codex'?page.frameLocator('iframe'):page;
      await scope.locator('#task-list .md-task-badge').first().waitFor();
      await scope.locator('#task-detail .md-task-status[data-status-code="awaiting_review"]').waitFor();
      for(const [code,label] of cases){assert.match(await scope.locator('#task-list').innerText(),new RegExp(label));checks++;}
      assert.equal(await scope.locator('#task-list .task-row').first().locator('.task-right').innerText().then(x=>x.includes('已完成')),false);checks++;
      await scope.locator('#task-list [data-task-id="status-4"]').click();
      await scope.locator('#task-detail .md-task-status[data-status-code="integration_awaiting_review"]').waitFor();
      await scope.locator('#task-detail details').filter({hasText:'集成记录（1 份）'}).first().locator('summary').click();
      assert.match(await scope.locator('#task-detail').innerText(),/batch\.1/);checks++;
      await scope.locator('#goals-toggle').click();
      await scope.locator('.goal-node > summary').first().click();
      await scope.locator('.stage-node > summary [data-status-code="waiting_dependency"]').waitFor();checks++;
      const box=await scope.locator('#task-detail .md-task-status').first().boundingBox();
      assert(box&&box.width<=width&&box.width>100);checks++;
      const stress=await scope.locator('#task-detail').evaluate(element=>{
        const probe=document.createElement('section');
        probe.style.cssText='width:280px;max-width:100%;min-width:0';
        const html=window.ModelDeskTaskStatus.render({schema:1,source:'controller_projection',code:'fixture_stress',
          label:'<img src=x onerror="window.__statusXss=1">'+ '长标题'.repeat(300),
          detail:'超长说明'.repeat(1000),next_step:'https://example.invalid/'.repeat(500),
          facts:[{label:'verylonglabel'.repeat(100),value:'X'.repeat(10000)}]});
        probe.innerHTML=html;element.appendChild(probe);
        const result={fits:probe.scrollWidth<=probe.clientWidth+1,
          safe:probe.querySelectorAll('img,script').length===0&&!window.__statusXss};
        probe.remove();return result;
      });
      assert(stress.fits);assert(stress.safe);checks+=2;
      // Opening a project already remembers its locator in the existing app.
      // This baseline behavior is mocked, not a new task/status mutation.
      const hostCalls=host==='codex'?await page.evaluate(()=>window.__calls):[];
      const writes=mutations.concat(hostCalls.filter(c=>c.method!=='GET').map(c=>c.method+' '+c.path));
      assert(writes.length<=1&&writes.every(c=>c==='POST /api/projects/remember'),JSON.stringify(writes));
      assert.deepEqual(errors,[]);assert.deepEqual(missing,[]);checks+=3;
      if(process.env.MODEL_DESK_QA_DIR&&width===1100&&theme==='dark') {
        fs.mkdirSync(process.env.MODEL_DESK_QA_DIR,{recursive:true});
        await scope.locator('#task-detail').screenshot({path:path.join(process.env.MODEL_DESK_QA_DIR,'task-status-'+host+'.png')});
      }
      await page.close();
    }
    console.log(JSON.stringify({checks,hosts:['codex-inline-rpc-mock','dsh-browser-mock'],widths:[320,1100],themes:['dark','light'],real_model_calls:0,task_mutations:0,baseline_project_remember:'at most one mocked POST per page'}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
