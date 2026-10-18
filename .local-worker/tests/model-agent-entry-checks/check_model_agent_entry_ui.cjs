// Model default agent entry: real browser + mocked HTTP only (never the live
// service).  The entry sits where the old global default select used to be and
// must patch exactly {default_agent} for one model, keep unsaved choices across
// polling, report failures honestly, and survive empty/deleted/invalid rows.
const {chromium}=require('../browser_helpers.cjs');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'../..'),out=path.resolve(__dirname,'..');
const project='C:\\model-agent-entry-fixture';
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  const page=await browser.newPage({viewport:{width:1280,height:1000}});
  const workflow={enabled:true,execution_agent:'dsh',paid_allowed:true,model_scope:'all',
    enforce_paid_budget:false,enforce_model_call_limit:false,task_budget_cny:5,daily_budget_cny:20,max_model_calls:30};
  const baselineWorkflow={...workflow};
  const models=[
    {id:'home',name:'本地 Qwen',provider:'local',model:'qwen-home',enabled:true,api_base:'http://home.invalid/v1',
      max_input_tokens:32768,max_output_tokens:4096,default_agent:'follow_global',default_agent_valid:true,agent_preset:'native'},
    {id:'flash',name:'DeepSeek Flash',provider:'custom',model:'deepseek-flash',enabled:true,api_base:'https://flash.invalid/v1',
      max_input_tokens:100000,max_output_tokens:8192,default_agent:'pi',default_agent_valid:true,agent_preset:'standard'}];
  const agents={dsh:{name:'DeepSeek Harness',ready:true,configuration:{description:'DSH only',model_fields:[
      {key:'agent_preset',label:'运行预设',type:'select',default:'standard',options:[{value:'native',label:'原生模式'},{value:'standard',label:'标准模式'}]}]}},
    pi:{name:'Pi Agent',ready:true,version:'2.1.0',configuration:{description:'Pi 无需额外设置',model_fields:[]}}};
  let failNextSave=false,modelsGone=false;
  const modelSaves=[],workflowSaves=[],errors=[],unexpected=[];let stateReads=0;
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),p=url.pathname;
    const json=data=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
    if(url.hostname!=='model-desk.test'){unexpected.push(url.href);return route.abort();}
    if(p==='/api/session')return json({token:'mock'});
    if(p==='/api/state'){stateReads++;
      return json({workflow,models:modelsGone?[]:models,default_model:modelsGone?null:'home',tasks:[],
        execution_agents:agents,agent:{ready:true,version:''},server_time:new Date().toISOString(),view:{mode:'project',project}});}
    if(p==='/api/projects')return json({projects:[{path:project}]});
    if(p==='/api/tree')return json({goals:[],stats:{}});
    if(p==='/api/db/space')return json({initialized:false,databases:[]});
    if(p==='/api/projects/remember')return json({ok:true});
    if(p==='/api/workflow'&&request.method()==='POST'){workflowSaves.push(request.postDataJSON());return json(workflow);}
    let match=p.match(/^\/api\/models\/([^/]+)\/save$/);
    if(match&&request.method()==='POST'){
      const id=match[1],body=request.postDataJSON();
      // Only a preference-only patch is a legitimate request from this entry.
      if(setOf(body)!=='default_agent')return route.fulfill({status:400,contentType:'application/json',
        body:JSON.stringify({error:'模型默认 Agent 需要单独保存，不能混入模型连接或其他参数'})});
      if(failNextSave)return route.fulfill({status:400,contentType:'application/json',
        body:JSON.stringify({error:'模型不存在'})});
      const model=models.find(item=>item.id===id);
      if(!model)return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'模型不存在'})});
      modelSaves.push({id,body});Object.assign(model,body);
      // The real registry recomputes validity when the row is published.
      model.default_agent_valid=['follow_global','dsh','pi'].includes(model.default_agent);
      return json(model);
    }
    const asset={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[p];
    if(asset)return route.fulfill({status:200,contentType:p.endsWith('.js')?'text/javascript':p.endsWith('.css')?'text/css':'text/html',
      body:fs.readFileSync(path.join(root,'panel',asset))});
    unexpected.push(request.method()+' '+p);return json({});
  });
  const setOf=body=>Object.keys(body||{}).sort().join(',');
  const entry=page.locator('#model-agent-entry');
  const modelSelect=page.locator('#agent-entry-model'),agentSelect=page.locator('#agent-entry-agent');
  const status=page.locator('#agent-entry-status'),agentState=page.locator('#agent-state');
  const card=id=>page.locator(`article.model-card:has([data-model-action="edit"][data-id="${id}"])`);
  const waitForPoll=async()=>{const before=stateReads;
    await page.waitForFunction(n=>false,0,{timeout:1}).catch(()=>{});
    for(let i=0;i<40&&stateReads===before;i++)await page.waitForTimeout(200);
    assert.ok(stateReads>before,'auto poll did not run');};
  const overflow=async width=>{
    const metrics=await page.evaluate(()=>{
      const bar=document.querySelector('.workflow-bar'),entry=document.querySelector('#model-agent-entry');
      return {scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth,
        bar:{scroll:bar.scrollWidth,client:bar.clientWidth,bottom:bar.getBoundingClientRect().bottom},
        entry:{scroll:entry.scrollWidth,client:entry.clientWidth,right:entry.getBoundingClientRect().right,
          bottom:entry.getBoundingClientRect().bottom,top:entry.getBoundingClientRect().top}};
    });
    assert.ok(metrics.scroll<=metrics.client+1,`page overflows at ${width}px: ${JSON.stringify(metrics)}`);
    assert.ok(metrics.bar.scroll<=metrics.bar.client+1,`workflow bar overflows at ${width}px: ${JSON.stringify(metrics)}`);
    assert.ok(metrics.entry.scroll<=metrics.entry.client+1,`agent entry overflows at ${width}px: ${JSON.stringify(metrics)}`);
    assert.ok(metrics.entry.right<=metrics.client+1,`agent entry escapes the viewport at ${width}px: ${JSON.stringify(metrics)}`);
    assert.ok(metrics.entry.bottom<=metrics.bar.bottom+1,`agent entry overflows the bar vertically at ${width}px: ${JSON.stringify(metrics)}`);
    assert.ok(metrics.entry.bottom>metrics.entry.top,`agent entry has no height at ${width}px`);
    const box=await page.locator('#agent-entry-save').boundingBox();
    assert.ok(box&&box.x>=0&&box.x+box.width<=metrics.client+1,`save button is not reachable at ${width}px: ${JSON.stringify(box)}`);
    assert.ok(await page.locator('#agent-entry-save').isVisible());
    assert.ok(await page.locator('#agent-entry-status').isVisible());
    return metrics;};
  try{
    await page.goto('http://model-desk.test/?project='+encodeURIComponent(project));
    await agentState.filter({hasText:'DeepSeek Harness 就绪 · 全局回退'}).waitFor();

    // The global default control is gone; the model entry owns that place.
    assert.equal(await page.locator('#execution-agent').count(),0);
    assert.equal(await page.locator('select#agent-entry-model').count(),1);
    assert.equal(await page.locator('select#agent-entry-agent').count(),1);
    assert.equal(await page.locator('#model-agent-entry #agent-entry-save').count(),1);
    assert.ok(await entry.evaluate(node=>!!node.closest('.workflow-context')),'entry is not in the collaboration bar');
    assert.ok(await entry.evaluate(node=>node.compareDocumentPosition(document.getElementById('agent-settings-open'))&Node.DOCUMENT_POSITION_FOLLOWING),
      'entry must sit before the agent parameter button, where the old global select was');
    assert.deepEqual(await modelSelect.locator('option').evaluateAll(xs=>xs.map(x=>x.value)),['home','flash']);
    assert.deepEqual(await agentSelect.locator('option').evaluateAll(xs=>xs.map(x=>x.value)),['follow_global','dsh','pi']);
    assert.equal(await modelSelect.inputValue(),'home');
    assert.equal(await agentSelect.inputValue(),'follow_global');
    assert.match(await status.textContent(),/当前显示已保存值/);

    // Switching model shows that model's own saved value, and the adapter status
    // follows the agent this model really resolves to.
    await modelSelect.selectOption('flash');
    assert.equal(await agentSelect.inputValue(),'pi');
    await agentState.filter({hasText:'Pi Agent 2.1.0 就绪 · 模型指定'}).waitFor();
    await modelSelect.selectOption('home');
    assert.equal(await agentSelect.inputValue(),'follow_global');
    await agentState.filter({hasText:'DeepSeek Harness 就绪 · 全局回退'}).waitFor();

    // Save the second model through the entry: exactly one scoped patch.
    await modelSelect.selectOption('flash');
    await agentSelect.selectOption('dsh');
    assert.match(await status.textContent(),/未保存/);
    await page.locator('#agent-entry-save').click();
    await page.waitForFunction(()=>document.querySelector('#toast').hidden===false);
    assert.deepEqual(modelSaves,[{id:'flash',body:{default_agent:'dsh'}}]);
    assert.deepEqual(workflowSaves,[]);
    assert.equal(models[1].default_agent,'dsh');
    assert.match(await status.textContent(),/新的任务生效/);
    assert.match(await card('flash').textContent(),/默认 Agent：DeepSeek Harness · 模型指定/);
    await agentState.filter({hasText:'DeepSeek Harness 就绪 · 模型指定'}).waitFor();

    // A poll must not swallow an unsaved choice for the other model.
    await modelSelect.selectOption('home');
    await agentSelect.selectOption('pi');
    await waitForPoll();
    assert.equal(await modelSelect.inputValue(),'home');
    assert.equal(await agentSelect.inputValue(),'pi');
    assert.match(await status.textContent(),/未保存/);
    assert.equal(models[0].default_agent,'follow_global');
    assert.equal(modelSaves.length,1,'polling must not save anything by itself');

    // A failing save is reported as a failure, never as success.
    failNextSave=true;
    await page.locator('#agent-entry-save').click();
    await page.waitForFunction(()=>/保存失败/.test(document.querySelector('#agent-entry-status').textContent));
    assert.match(await status.textContent(),/保存失败：模型不存在/);
    assert.equal(await page.locator('#toast.error').count(),1);
    assert.match(await page.locator('#toast').textContent(),/保存失败/);
    assert.equal(models[0].default_agent,'follow_global');
    assert.match(await card('home').textContent(),/默认 Agent：DeepSeek Harness · 跟随全局默认/);
    assert.equal(await agentSelect.inputValue(),'pi','failed save keeps the attempted choice visible');
    await waitForPoll();
    assert.match(await status.textContent(),/保存失败/,'polling must not hide the failed save');

    // Retrying the same save succeeds and is then really persisted.
    failNextSave=false;
    await page.locator('#agent-entry-save').click();
    await page.waitForFunction(()=>/新的任务生效/.test(document.querySelector('#agent-entry-status').textContent));
    assert.equal(models[0].default_agent,'pi');
    assert.deepEqual(modelSaves[1],{id:'home',body:{default_agent:'pi'}});
    assert.match(await card('home').textContent(),/默认 Agent：Pi Agent · 模型指定/);
    assert.deepEqual(workflowSaves,[]);

    // Reload: both saved values come back from the server, per model.
    await page.reload();
    await agentState.filter({hasText:'Pi Agent 2.1.0 就绪 · 模型指定'}).waitFor();
    assert.equal(await modelSelect.inputValue(),'home');
    assert.equal(await agentSelect.inputValue(),'pi');
    await modelSelect.selectOption('flash');
    assert.equal(await agentSelect.inputValue(),'dsh');
    await agentState.filter({hasText:'DeepSeek Harness 就绪 · 模型指定'}).waitFor();

    // An invalid historical value stays visible and repairable.
    models[0].default_agent='typo';models[0].default_agent_valid=false;
    await waitForPoll();
    await modelSelect.selectOption('home');
    assert.equal(await agentSelect.inputValue(),'typo');
    assert.match(await agentSelect.locator('option[value=typo]').textContent(),/已保存值无效/);
    assert.match(await status.textContent(),/已保存值无效/);
    await agentState.filter({hasText:'执行器未确定'}).waitFor();
    await agentSelect.selectOption('dsh');
    await page.locator('#agent-entry-save').click();
    await page.waitForFunction(()=>/新的任务生效/.test(document.querySelector('#agent-entry-status').textContent));
    assert.equal(models[0].default_agent,'dsh');
    await agentState.filter({hasText:'DeepSeek Harness 就绪 · 模型指定'}).waitFor();

    // A deleted model does not break the entry.
    await modelSelect.selectOption('flash');
    assert.equal(await agentSelect.inputValue(),'dsh');
    models.splice(models.findIndex(m=>m.id==='flash'),1);
    await waitForPoll();
    assert.deepEqual(await modelSelect.locator('option').evaluateAll(xs=>xs.map(x=>x.value)),['home']);
    assert.equal(await modelSelect.inputValue(),'home');
    assert.equal(await agentSelect.inputValue(),'dsh');
    assert.match(await status.textContent(),/原模型已移除/);

    // No models at all: usable, disabled, explained, no crash.
    modelsGone=true;
    await waitForPoll();
    assert.equal(await modelSelect.isDisabled(),true);
    assert.equal(await agentSelect.isDisabled(),true);
    assert.equal(await page.locator('#agent-entry-save').isDisabled(),true);
    assert.match(await status.textContent(),/还没有已配置模型/);
    assert.match(await agentState.textContent(),/未选择模型/);

    // Layout at both required widths, no horizontal overflow.
    await page.setViewportSize({width:1280,height:1000});
    modelsGone=false;
    await waitForPoll();
    await overflow(1280);
    await page.screenshot({path:path.join(out,'model-agent-entry-1280.png')});
    await page.setViewportSize({width:420,height:900});
    await page.waitForTimeout(300);
    await overflow(420);
    await page.screenshot({path:path.join(out,'model-agent-entry-420.png')});

    assert.deepEqual(workflowSaves,[]);
    assert.deepEqual(workflow,baselineWorkflow,'global execution_agent must not change');
    assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
    console.log(JSON.stringify({ok:true,entry_replaces_global_control:true,per_model_saves:modelSaves.length,
      scoped_patch_only:true,global_untouched:true,poll_keeps_unsaved:true,failed_save_reported:true,
      reload_retained:true,invalid_value_repairable:true,deleted_model_safe:true,empty_models_safe:true,
      state_reads:stateReads,production_mutations:0,provider_calls:0}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
