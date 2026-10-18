// Real browser + mocked HTTP: per-model default agent saves, persists across
// refresh, and the new-task default never sends the global value as an override.
const {chromium}=require('./browser_helpers.cjs');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..'),project='C:\\model-agent-defaults-fixture';
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'}),page=await browser.newPage({viewport:{width:1280,height:1000}});
  const workflow={enabled:true,execution_agent:'dsh',paid_allowed:true,model_scope:'all',
    enforce_paid_budget:false,enforce_model_call_limit:false,task_budget_cny:5,daily_budget_cny:20,max_model_calls:30};
  const models=[
    {id:'home',name:'本地 Qwen',provider:'local',model:'qwen-home',enabled:true,api_base:'http://home.invalid/v1',
      max_input_tokens:32768,max_output_tokens:4096,default_agent:'follow_global',default_agent_valid:true,
      preferred_tasks:['信息整理'],max_task_complexity:'simple',cost_preference:'economy',agent_preset:'native'},
    {id:'flash',name:'DeepSeek Flash',provider:'custom',model:'deepseek-flash',enabled:true,api_base:'https://flash.invalid/v1',
      max_input_tokens:100000,max_output_tokens:8192,default_agent:'pi',default_agent_valid:true,
      preferred_tasks:['多文件修改'],max_task_complexity:'complex',cost_preference:'balanced',agent_preset:'standard'}];
  const agents={
    dsh:{name:'DeepSeek Harness',ready:true,configuration:{description:'DSH only',model_fields:[
      {key:'agent_preset',label:'运行预设',type:'select',default:'standard',options:[{value:'native',label:'原生模式'},{value:'standard',label:'标准模式'}]}]}},
    pi:{name:'Pi Agent',ready:true,configuration:{description:'Pi 无需额外设置',model_fields:[]}}};
  const modelSaves=[],workflowSaves=[],dispatches=[],errors=[],unexpected=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),p=url.pathname;
    const json=data=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
    if(url.hostname!=='model-desk.test'){unexpected.push(url.href);return route.abort();}
    if(p==='/api/session')return json({token:'mock'});
    if(p==='/api/state')return json({workflow,models,default_model:'home',tasks:[],execution_agents:agents,
      agent:{ready:true,version:'fixture'},server_time:new Date().toISOString(),view:{mode:'project',project}});
    if(p==='/api/projects')return json({projects:[{path:project}]});
    if(p==='/api/tree')return json({goals:[],stats:{}});
    if(p==='/api/db/space')return json({initialized:false,databases:[]});
    if(p==='/api/projects/remember')return json({ok:true});
    if(p==='/api/tasks'&&request.method()==='POST'){
      const body=request.postDataJSON();dispatches.push(body);
      return json({...body,id:'mock-task-'+dispatches.length,status:'queued',engine:body.engine||'model-default'});
    }
    if(p==='/api/workflow'&&request.method()==='POST'){
      const body=request.postDataJSON();workflowSaves.push(body);Object.assign(workflow,body);return json(workflow);
    }
    let match=p.match(/^\/api\/models\/([^/]+)\/save$/);
    if(match&&request.method()==='POST'){
      const id=match[1],body=request.postDataJSON(),model=models.find(item=>item.id===id);
      // Simulate a task starting while the editor is open: only a scoped
      // preference patch is allowed; the connection-edit lock stays active.
      if(Object.keys(body).some(key=>key!=='default_agent'))return route.fulfill({status:400,
        contentType:'application/json',body:JSON.stringify({error:'有任务运行，不能修改连接配置'})});
      modelSaves.push({id,body});Object.assign(model,body);return json(model);
    }
    const asset={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[p];
    if(asset)return route.fulfill({status:200,contentType:p.endsWith('.js')?'text/javascript':p.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,'panel',asset))});
    unexpected.push(request.method()+' '+p);return json({});
  });
  try{
    await page.goto('http://model-desk.test/?project='+encodeURIComponent(project));
    await page.locator('#agent-state').filter({hasText:'DeepSeek Harness'}).waitFor();
    const card=id=>page.locator(`article.model-card:has([data-model-action="edit"][data-id="${id}"])`);
    assert.match(await card('home').textContent(),/默认 Agent：DeepSeek Harness · 跟随全局默认/);
    assert.match(await card('flash').textContent(),/默认 Agent：Pi Agent · 模型指定/);

    // Save the home model's own default through the normal model editor.
    await page.locator('[data-model-action=edit][data-id=home]').click();
    assert.equal(await page.locator('#model-default-agent').inputValue(),'follow_global');
    await page.locator('#model-default-agent').selectOption('pi');
    await page.locator('#model-form button[type=submit]').click();
    await page.locator('#model-dialog').waitFor({state:'hidden'});
    assert.equal(modelSaves[0].id,'home');
    assert.equal(modelSaves[0].body.default_agent,'pi');
    assert.deepEqual(modelSaves[0].body,{default_agent:'pi'});
    assert.deepEqual(workflowSaves,[]);

    // Refresh/reload: the saved preference must survive and the card shows the
    // actual inherited/model-specific source.
    await page.reload();
    await page.locator('#agent-state').filter({hasText:'Pi Agent 就绪 · 模型指定'}).waitFor();
    assert.match(await card('home').textContent(),/默认 Agent：Pi Agent · 模型指定/);
    await page.locator('[data-model-action=edit][data-id=home]').click();
    assert.equal(await page.locator('#model-default-agent').inputValue(),'pi');
    await page.locator('#model-dialog .close-dialog').first().click();
    await page.locator('#model-dialog').waitFor({state:'hidden'});

    // The second model can inherit the still-global DSH.
    await page.locator('[data-model-action=edit][data-id=flash]').click();
    await page.locator('#model-default-agent').selectOption('follow_global');
    await page.locator('#model-form button[type=submit]').click();
    await page.locator('#model-dialog').waitFor({state:'hidden'});
    assert.equal(modelSaves[1].id,'flash');
    assert.equal(modelSaves[1].body.default_agent,'follow_global');
    assert.match(await card('flash').textContent(),/默认 Agent：DeepSeek Harness · 跟随全局默认/);
    // No global default control is offered any more; the quick entry edits one
    // model at a time and never writes the legacy global fallback setting.
    assert.equal(await page.locator('#execution-agent').count(),0);
    assert.deepEqual(workflowSaves,[]);
    assert.equal(workflow.execution_agent,'dsh');
    await page.locator('#agent-entry-model').selectOption('flash');
    assert.equal(await page.locator('#agent-entry-agent').inputValue(),'follow_global');
    await page.locator('#agent-state').filter({hasText:'DeepSeek Harness 就绪 · 全局回退'}).waitFor();
    await page.locator('#agent-entry-agent').selectOption('pi');
    assert.match(await page.locator('#agent-entry-status').textContent(),/未保存/);
    await page.locator('#agent-entry-save').click();
    await page.waitForFunction(()=>/新的任务生效/.test(document.querySelector('#agent-entry-status').textContent));
    assert.deepEqual(modelSaves[2],{id:'flash',body:{default_agent:'pi'}});
    assert.deepEqual(workflowSaves,[]);
    assert.match(await card('flash').textContent(),/默认 Agent：Pi Agent · 模型指定/);
    assert.match(await card('home').textContent(),/默认 Agent：Pi Agent · 模型指定/);
    await page.locator('#agent-entry-model').selectOption('home');
    assert.equal(await page.locator('#agent-entry-agent').inputValue(),'pi');
    assert.equal(workflow.execution_agent,'dsh');

    // A new task defaults to "use model default" and omits engine entirely.
    await page.locator('#new-task').click();
    assert.equal(await page.locator('#task-engine').inputValue(),'model_default');
    await page.locator('#task-route').selectOption('manual');
    await page.locator('#task-model').selectOption('home');
    assert.match(await page.locator('#task-agent-hint').textContent(),/模型保存的默认 Agent（Pi Agent）/);
    await page.locator('#task-prompt').fill('default-model-agent fixture');
    await page.locator('#task-form button[type=submit]').click();
    await page.locator('#task-dialog').waitFor({state:'hidden'});
    assert.equal(dispatches[0].model_id,'home');
    assert.equal('engine' in dispatches[0],false);
    assert.equal(workflow.execution_agent,'dsh');

    // Explicit selection is the only thing sent as an override.
    await page.locator('#new-task').click();
    await page.locator('#task-route').selectOption('manual');
    await page.locator('#task-model').selectOption('home');
    await page.locator('#task-engine').selectOption('dsh');
    assert.match(await page.locator('#task-agent-hint').textContent(),/本次显式指定 DeepSeek Harness/);
    await page.locator('#task-prompt').fill('explicit-agent fixture');
    await page.locator('#task-form button[type=submit]').click();
    await page.locator('#task-dialog').waitFor({state:'hidden'});
    assert.equal(dispatches[1].engine,'dsh');

    // The parameter entry is explicitly separate from "who executes".
    await page.locator('#agent-settings-open').click();
    assert.match(await page.locator('#agent-settings-dialog').textContent(),/各执行 Agent 参数设置/);
    assert.match(await page.locator('#agent-settings-dialog').textContent(),/不决定谁执行/);
    assert.match(await page.locator('#agent-settings-default-summary').textContent(),/当前模型默认执行 Agent：Pi Agent · 模型指定/);
    await page.locator('#agent-settings-open-model').click();
    assert.equal(await page.locator('#model-dialog').isVisible(),true);
    assert.equal(await page.locator('#model-id').inputValue(),'home');
    assert.equal(await page.locator('#model-default-agent').inputValue(),'pi');
    await page.locator('#model-dialog .close-dialog').first().click();

    assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
    console.log(JSON.stringify({ok:true,model_default_saved:true,refresh_retained:true,
      task_default_omits_override:true,explicit_override_only:true,agent_parameters_separate:true,
      production_mutations:0,provider_calls:0}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
