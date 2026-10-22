// Real DOM, intercepted HTTP only: both host views, inline drafts and toggles.
const {chromium}=require('./browser_helpers.cjs');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const directory=path.resolve(__dirname,'../panel');
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    for(const host of ['codex','dsh']){
      const page=await browser.newPage({viewport:{width:1100,height:1000}}),errors=[],writes=[];
      const model={id:'home',name:'Local fixture',provider:'local',model:'qwen',api_base:'http://local.invalid/v1',
        enabled:true,default_agent:'pi',max_input_tokens:32768,max_output_tokens:4096};
      const paid={...model,id:'paid',name:'Paid fixture',provider:'deepseek',model:'deepseek-flash',
        api_base:'https://api.deepseek.com',default_agent:'dsh',billing:{pricing:{period:'unknown',label:'fixture'},
          balance:{status:'ready',balance_infos:[{currency:'CNY',total_balance:'12.33'}]}}};
      const agents={pi:{name:'Pi Agent',ready:true,configuration:{model_fields:[]}},
        dsh:{name:'DeepSeek Harness',ready:true,configuration:{model_fields:[]}}};
      const fixture={models:[model,paid],default_model:'home',tasks:[],view:{mode:'project',project:'C:\\fixture'},
        execution_agents:agents,workflow:{enabled:true,execution_agent:'pi',model_scope:'all',paid_allowed:true,
          enforce_paid_budget:false,enforce_model_call_limit:false,daily:{}},
        codex_usage:{status:'unavailable',buckets:[]},stages:{stats:{},recovery:[]},
        execution_endpoints:{readable:true,rows:[
          {id:'pi',name:'Pi Agent',enabled:true,ready:true,model,models:[model],runtime:agents.pi},
          {id:'dsh',name:'DeepSeek Harness',enabled:true,ready:true,model:paid,models:[paid],runtime:agents.dsh},
          {id:'workbuddy',name:'WorkBuddy',enabled:false,ready:false,models:[],runtime:{}}]}};
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('http://modeldesk.invalid/**',async route=>{
        const request=route.request(),pathname=new URL(request.url()).pathname;
        if(['/','/app.js','/style.css'].includes(pathname)){
          const name=pathname==='/'?'index.html':pathname.slice(1);
          return route.fulfill({body:fs.readFileSync(path.join(directory,name)),contentType:
            name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});
        }
        let data=pathname==='/api/session'?{token:'fixture'}:pathname==='/api/state'?fixture:
          pathname==='/api/tree'?{goals:[],stats:{},view:fixture.view}:
          pathname==='/api/projects'?{projects:[],recent_projects:[]}:{};
        if(request.method()==='POST'){
          writes.push({path:pathname,body:request.postDataJSON()});
          if(pathname==='/api/workflow'&&request.postDataJSON().endpoint_settings){
            const block=request.postDataJSON().endpoint_settings;
            const row=fixture.execution_endpoints.rows.find(e=>e.id===block.id);
            Object.assign(row,block.values);data={ok:true};
            if(block.values.native_model){
              const picked={...row.model,id:'native-glm',model:block.values.native_model,name:'WorkBuddy · GLM-5.3'};
              row.model=picked;row.models.push(picked);row.current_model=picked.id;fixture.models.push(picked);
            }
          }
        }
        await route.fulfill({json:data});
      });
      await page.goto(`http://modeldesk.invalid/?host=${host}&project=C%3A%5Cfixture`);
      await page.locator('.endpoint-card').first().waitFor();
      assert.equal(await page.locator('.endpoint-card').count(),3);
      assert.equal(await page.locator('#model-agent-entry').isVisible(),false);
      assert.match(await page.locator('[data-endpoint-id="dsh"]').textContent(),/12\.33/);
      assert.match(await page.locator('[data-endpoint-id="workbuddy"]').textContent(),/未同步/);
      await page.locator('[data-endpoint-expand="pi"]').click();
      await page.locator('[data-endpoint-id="pi"] [data-model-action="edit"]').click();
      assert.equal(await page.locator('.endpoint-inline-editor #model-form').count(),1);
      await page.locator('#model-name').fill('unsaved draft');
      await page.evaluate(()=>refresh());
      assert.equal(await page.locator('#model-name').inputValue(),'unsaved draft');
      await page.locator('#model-form .dialog-footer .close-dialog').click();
      assert.equal(await page.locator('#model-dialog #model-form').count(),1);
      await page.locator('[data-endpoint-toggle="pi"]').click();
      await page.waitForFunction(()=>document.querySelector('[data-endpoint-toggle="pi"]').getAttribute('aria-checked')==='false');
      assert.deepEqual(writes.find(w=>w.path==='/api/workflow').body,{endpoint_settings:{id:'pi',values:{enabled:false}}});
      assert.equal(fixture.workflow.enabled,true);
      await page.locator('[data-endpoint-expand="workbuddy"]').click();
      assert.match(await page.locator('#endpoint-settings-workbuddy').textContent(),/无命令/);
      const native={...model,id:'workbuddy-deepseek-flash',provider:'workbuddy',model:'deepseek-v4.1-flash',
        name:'WorkBuddy native fixture',default_agent:'workbuddy',api_base:''};
      fixture.models.push(native);
      agents.workbuddy={name:'WorkBuddy',ready:true,configuration:{model_fields:[]},native_catalog:{models:[
        {model_id:'deepseek-v4.1-flash',name:'DeepSeek V4.1 Flash'}, {model_id:'glm-5.3',name:'GLM-5.3'}]}};
      const endpoint=fixture.execution_endpoints.rows.find(e=>e.id==='workbuddy');
      Object.assign(endpoint,{enabled:true,ready:true,model:native,models:[native],runtime:agents.workbuddy});
      await page.evaluate(()=>refresh());
      await page.locator('[data-native-setup] input[name="home"]').fill('C:\\native-fixture');
      await page.evaluate(()=>refresh());
      assert.equal(await page.locator('[data-native-setup] input[name="home"]').inputValue(),'C:\\native-fixture');
      await page.locator('[data-native-model]').selectOption('glm-5.3');
      await page.evaluate(()=>refresh());
      assert.equal(await page.locator('[data-native-model]').inputValue(),'glm-5.3');
      await page.locator('[data-native-selection] button[type="submit"]').click();
      await page.waitForFunction(()=>document.querySelector('[data-endpoint-id="workbuddy"] .model-code').textContent==='glm-5.3');
      assert.ok(writes.some(w=>w.body.endpoint_settings?.values.native_model==='glm-5.3'));
      assert.equal(fixture.default_model,'home');
      await page.locator('#new-task').click();
      await page.locator('#task-route').selectOption('manual');
      await page.locator('#task-model').selectOption(native.id);
      const nativeBody=await page.evaluate(()=>taskBody());
      assert.equal(nativeBody.model_id,native.id);
      assert.equal(nativeBody.engine,undefined,'native model must own the engine, not an API override');
      assert.equal(await page.locator('#task-test').isDisabled(),true);
      assert.match(await page.locator('#task-agent-hint').textContent(),/WorkBuddy 登录账户/);
      await page.locator('#task-dialog .dialog-footer .close-dialog').click();
      for(const width of [1100,736,390]){
        await page.setViewportSize({width,height:1000});
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${host} ${width}px overflow`);
      }
      assert.deepEqual(errors,[]);
      await page.close();
      console.log(`ok - ${host}: endpoint cards, API balance, inline draft, disabled switch, native limits, responsive layout`);
    }
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
