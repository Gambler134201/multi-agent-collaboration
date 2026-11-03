// Actual shared page/inline bundle; all requests intercepted, no real mutations.
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{launch}=require('./browser_helpers.cjs');
const root=path.resolve(__dirname,'../..'),web=path.join(root,'.local-worker/panel');
const run=code=>execFileSync(process.env.MODEL_DESK_PYTHON||'python',['-c',code],{cwd:root,encoding:'utf8',env:{...process.env,PYTHONIOENCODING:'utf-8'},maxBuffer:8*1024*1024});
const inline=run("import sys;sys.path.insert(0,'.local-worker');import codex_mcp_server as c;print(c.build_workbench_html())");
const fixture=JSON.parse(run(`import sys,json
sys.path.insert(0,'.local-worker')
import endpoint_readiness as d
settings={'enabled':True,'readable':True,'model_scope':'all','paid_allowed':True,'execution_agent':'pi','enforce_paid_budget':False,'enforce_model_call_limit':False,'daily':{}}
codes=['configuration_missing','runtime_ready','native_channel_unavailable','auto_connect_pending']
rows=[];models=[];agents={}
for key,code in zip(['pi','dsh','workbuddy','zcode'],codes):
 m={'id':key+'-model','name':key+' model','model':'fixture','provider':'local' if key=='pi' else key if key in ('workbuddy','zcode') else 'deepseek','enabled':True,'default_agent':key,'api_base':'http://model.invalid','max_input_tokens':32768,'max_output_tokens':4096}
 if key=='zcode':m['native_selection']={'providerId':'account:zai-start-plan','modelId':'GLM-5.3-Flash','options':{'reasoningLevel':'max'}}
 info={'ready':key in ('dsh','zcode'),'readiness':d.runtime(code),'auto_connect_pending':key=='zcode','configuration':{'model_fields':[]},'access_mode':'project','native_catalog':{'models':[]},'configuration_values':{}}
 row={'id':key,'name':{'pi':'Pi Agent','dsh':'DeepSeek Harness','workbuddy':'WorkBuddy','zcode':'ZCode'}[key],'enabled':True,'readable':True,'ready':info['ready'],'models':[m],'model':m,'current_model':m['id'],'runtime':info}
 row['diagnostic']=d.diagnose(row,settings)
 rows.append(row);models.append(m);agents[key]=info
print(json.dumps({'models':models,'default_model':models[0]['id'],'tasks':[],'execution_agents':agents,'execution_endpoints':{'readable':True,'rows':rows},'view':{'mode':'project','project':'E:/readiness-fixture'},'workflow':settings,'codex_usage':{'status':'unavailable','buckets':[]}},ensure_ascii=False))`));
fixture.execution_endpoints.rows[2].diagnostic.issues[0].next_step+=' <img src=x onerror="window.pwn=1">';
function response(apiPath,view){
 if(apiPath==='/api/session')return {token:'synthetic-only'};
 if(apiPath==='/api/state')return {...fixture,view:view||fixture.view};
 if(apiPath==='/api/tree')return {goals:[],stats:{},view:view||fixture.view};
 if(apiPath==='/api/projects')return {projects:[],recent_projects:[]};
 return {};
}
function hostHtml(){return '<!doctype html><style>body{margin:0}iframe{width:100%;height:100vh;border:0}</style><iframe src="/inline"></iframe><script>const fixture='+JSON.stringify(fixture)+';'+response.toString()+`;window.__calls=[];addEventListener('message',e=>{const m=e.data;if(!m||m.id===undefined)return;let result={};if(m.method==='ui/initialize')e.source.postMessage({jsonrpc:'2.0',method:'ui/notifications/tool-input',params:{arguments:{project:fixture.view.project}}},'*');else if(m.method==='tools/call'){const a=m.params.arguments;__calls.push({path:a.path,method:a.method});result={structuredContent:{status:200,data:response(a.path,a.view)}};}e.source.postMessage({jsonrpc:'2.0',id:m.id,result},'*');});</script>`;}
(async()=>{
 const browser=await launch();let checks=0;
 try{
  for(const host of ['codex','dsh'])for(const width of [390,1100])for(const theme of ['dark','light']){
   const page=await browser.newPage({viewport:{width,height:850},colorScheme:theme}),calls=[],errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.pathname==='/host')return route.fulfill({contentType:'text/html; charset=utf-8',body:hostHtml()});
    if(url.pathname==='/inline')return route.fulfill({contentType:'text/html; charset=utf-8',body:inline});
    if(url.pathname.startsWith('/api/')){
     calls.push({path:url.pathname,method:request.method()});
     const bound=request.headers()['x-model-desk-view'];
     return route.fulfill({json:response(url.pathname,bound?JSON.parse(Buffer.from(bound,'base64url')):undefined)});
    }
    const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if(/^[a-z-]+\.(html|js|css)$/.test(name)&&fs.existsSync(path.join(web,name)))return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(web,name),'utf8')});
    return route.fulfill({status:404,body:''});
   });
   await page.goto(host==='codex'?'http://fixture.invalid/host':'http://fixture.invalid/?host=dsh&project='+encodeURIComponent(fixture.view.project));
   const scope=host==='codex'?page.frameLocator('iframe'):page;
   const evaluate=fn=>scope.locator('body').evaluate(fn);
   await scope.locator('.endpoint-card').first().waitFor();await evaluate(async()=>{await refresh();stopAutoPoll();});
   const dialog=scope.locator('#endpoint-dialog');
   assert.equal(await scope.locator('.endpoint-card').count(),4);checks++;
   const labels=['缺少安装配置','本地预检通过','原生通道未就绪','可自动连接'];
   const ids=['pi','dsh','workbuddy','zcode'];
   for(let i=0;i<ids.length;i++){
    const card=scope.locator('[data-endpoint-id="'+ids[i]+'"]');
    assert.match(await card.innerText(),new RegExp(labels[i]));checks++;
    const before=await card.boundingBox();
    await card.locator('[data-endpoint-diagnose]').click();
    assert.equal(await scope.locator('#endpoint-diagnostic-panel').isVisible(),true);checks++;
    assert.match(await scope.locator('#endpoint-diagnostics').innerText(),/不调用模型/);checks++;
    assert.match(await scope.locator('#endpoint-diagnostics').innerText(),/不证明模型服务/);checks++;
    const height=(await card.boundingBox()).height;assert(Math.abs(height-before.height)<2);checks++;
    if(ids[i]==='workbuddy'){
     assert.match(await scope.locator('#endpoint-diagnostics').innerText(),/手动发一条普通消息/);checks++;
     assert.equal(await scope.locator('#endpoint-diagnostics img').count(),0);checks++;
    }
    await dialog.locator('.close-dialog').click();
   }
   await scope.locator('[data-endpoint-expand="pi"]').click();
   assert.equal(await scope.locator('#endpoint-preset-panel').isVisible(),true);checks++;
   await scope.locator('#endpoint-notes').fill('unsaved draft');
   await scope.locator('[data-endpoint-tab="diagnostics"]').click();
   await evaluate(async()=>{await refresh();stopAutoPoll();});
   assert.equal(await scope.locator('#endpoint-diagnostic-panel').isVisible(),true);checks++;
   await scope.locator('[data-endpoint-tab="preset"]').click();
   assert.equal(await scope.locator('#endpoint-notes').inputValue(),'unsaved draft');checks++;
   await scope.locator('[data-endpoint-tab="diagnostics"]').click();
   await evaluate(()=>{document.documentElement.dataset.fontScale='xlarge';});
   assert(await dialog.evaluate(e=>e.scrollWidth<=e.clientWidth+1));checks++;
   const rpc=host==='codex'?await page.evaluate(()=>window.__calls):[];
   assert([...calls,...rpc].filter(c=>c.method!=='GET').every(c=>c.path==='/api/projects/remember'));checks++;
   assert.deepEqual(errors,[]);checks++;
   if(process.env.MODEL_DESK_QA_DIR&&width===1100&&theme==='dark'){
    fs.mkdirSync(process.env.MODEL_DESK_QA_DIR,{recursive:true});await dialog.screenshot({path:path.join(process.env.MODEL_DESK_QA_DIR,'readiness-'+host+'.png')});
   }
   await page.close();
  }
  console.log(JSON.stringify({checks,hosts:['codex-inline-rpc-mock','dsh-browser-mock'],widths:[390,1100],themes:['dark','light'],real_model_calls:0,real_mutations:0}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
