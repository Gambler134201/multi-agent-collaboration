// Pure renderer + real panel DOM; all requests are intercepted offline.
const {chromium}=require('./browser_helpers.cjs');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const panel=path.resolve(__dirname,'../panel');
const context=vm.createContext({});vm.runInContext(fs.readFileSync(path.join(panel,'model-picker-view.js'),'utf8'),context);
const view=context.ModelDeskModelPickerView;
const sample={endpoint:'opencode',label:'OpenCode',revision:'r1',sources:[{id:'saved',label:'已保存',kind:'saved'},
  {id:'native:oc',label:'OpenCode Go',kind:'native',source_ref:'go'}],current_model:'saved:go',current_reasoning:'default',models:[
    {id:'saved:go',source_id:'saved',registered_id:'go',name:'当前 Flash',model:'opencode-go/deepseek',selectable:true,reasoning:[{value:'default',label:'默认'}]},
    {id:'native-haiku',source_id:'native:oc',name:'Claude Haiku',model:'opencode-go/claude-haiku',selectable:true,reasoning:[{value:'default',label:'默认'}]},
    {id:'unavailable',source_id:'native:oc',name:'Unavailable',model:'hidden',selectable:false,unavailable_reason:'客户端不支持',reasoning:[]}]};
assert.equal(view.filter(sample,'haIKu','all').length,1);assert.equal(view.filter(sample,'opencode go','all').length,2);
assert.equal(view.filter(sample,'当前','saved').length,1);
let html=view.render(sample,{selected_id:'native-haiku',reasoning:'bogus'});assert.match(html,/✓/);assert.match(html,/待应用/);assert.match(html,/data-picker-apply disabled/);
assert.match(view.render(sample,{selected_id:'unavailable'}),/data-picker-apply disabled/);
assert.match(view.render(sample,{selected_id:'native-haiku',reasoning:'default',busy:true}),/data-picker-apply disabled/);
assert.match(view.render({models:[],sources:[]},{}),/暂无可选模型/);
assert.match(view.render(sample,{error:'fixture failure'}),/fixture failure/);
const many={...sample,models:Array.from({length:501},(_,i)=>({...sample.models[1],id:String(i)}))};
html=view.render(many,{});assert.equal((html.match(/data-picker-choice=/g)||[]).length,200);assert.match(html,/501/);
const attack={...sample,models:[{...sample.models[1],id:'"><script>BAD()</script>',name:'<img src=x onerror=BAD()>',reasoning:[{value:'default',label:'<script>BAD()</script>'}]}]};
html=view.render(attack,{selected_id:attack.models[0].id});assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img'));assert.match(html,/&lt;img/);
const priority={...sample,endpoint:'workbuddy',priority_context:{slot:'2',clients:[{id:'workbuddy',label:'WorkBuddy'},{id:'"><img src=x>',label:'<script>BAD</script>'}]}};
html=view.render(priority,{selected_id:'native-haiku',reasoning:'default',client_id:'workbuddy'});
assert.match(html,/选择第 2 顺位的客户端与模型/);assert.match(html,/data-picker-client/);assert.match(html,/用于此顺位/);assert.match(html,/选择仅加入排序草稿/);assert.ok(!html.includes('data-picker-pin'));assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img'));
for(const client of ['', 'unknown']){html=view.render({...priority,endpoint:''},{selected_id:'native-haiku',reasoning:'default',client_id:client});assert.match(html,/data-picker-apply disabled/);}
html=view.render(priority,{selected_id:'native-haiku',reasoning:'default',client_id:'workbuddy',loading:true});assert.match(html,/data-picker-client[^>]*disabled/);assert.match(html,/data-picker-apply disabled/);
console.log('ok - pure renderer: grouping/search/escaping/current/pending/invalid/empty/busy/501-row cap; no browser globals available');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{for(const host of ['codex','dsh']){
  const page=await browser.newPage({viewport:{width:1100,height:1000}}),errors=[],ops=[];
  page.on('pageerror',e=>errors.push(e.message));
  const model={id:'go',name:'Go fixture',provider:'custom',model:'deepseek',api_base:'https://opencode.ai/zen/go/v1',api_key_set:true,enabled:true,default_agent:'opencode',max_input_tokens:32768,max_output_tokens:4096};
  const runtime={name:'OpenCode',ready:true,access_mode:'project',configuration:{model_fields:[]}};
  const quota={status:'never',can_refresh:true,capability:'native',limits:[],sync:{enabled:false,interval_seconds:180}};
  const fixture={models:[model],default_model:'go',tasks:[],view:{mode:'project',project:'C:\\fixture'},execution_agents:{opencode:runtime},
    workflow:{enabled:true,execution_agent:'opencode',model_scope:'all',paid_allowed:true,daily:{}},stages:{stats:{},recovery:[]},
    codex_usage:{status:'unavailable',buckets:[]},routing_priority:{enabled:false,readable:true,slots:[]},
    execution_endpoints:{readable:true,rows:[{id:'opencode',name:'OpenCode',enabled:true,ready:true,model,models:[model],runtime,quota}]}};
  let catalog=structuredClone(sample);
  await page.route('http://picker.invalid/**',async route=>{
    const req=route.request(),p=new URL(req.url()).pathname;
    if(p==='/'||(/^\/[a-z0-9-]+\.(js|css)$/.test(p)&&fs.existsSync(path.join(panel,p.slice(1))))){const name=p==='/'?'index.html':p.slice(1);return route.fulfill({body:fs.readFileSync(path.join(panel,name)),contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});}
    let data=p==='/api/state'?fixture:p==='/api/session'?{token:'fixture'}:p==='/api/tree'?{goals:[],stats:{},view:fixture.view}:p==='/api/projects'?{projects:[],recent_projects:[]}:{};
    if(req.method()==='POST'){
      const op=req.postDataJSON().endpoint_settings?.values.model_picker;
      if(op){ops.push(op);
        if(op.action==='details'){catalog.models[1].reasoning=[{value:'default',label:'默认'},{value:'high',label:'高'}];catalog.revision='r2';}
        if(op.action==='refresh')catalog.revision='r3';
        data=op.action==='select'?{ok:true,message:'模型已保存，仅影响新任务。'}:{ok:true,catalog,model_called:false};
      }
    }
    await route.fulfill({json:data});
  });
  await page.goto(`http://picker.invalid/?host=${host}&project=C%3A%5Cfixture`);await page.locator('[data-open-model-picker]').waitFor();
  assert.equal(ops.length,0,'render must not read catalogs');
  await page.locator('[data-open-model-picker]').click();await page.locator('[data-picker-query]:enabled').waitFor();
  assert.equal(ops.filter(o=>o.action==='catalog').length,1);assert.equal(ops.length,1,'fresh cache must not refresh');
  await page.locator('[data-picker-query]').fill('hAIKu');assert.equal(await page.locator('[data-picker-choice]').count(),1);assert.equal(ops.length,1,'search must stay local');
  await page.locator('[data-picker-choice="native-haiku"]').click();await page.locator('[data-picker-reasoning]:enabled option[value="high"]').waitFor({state:'attached'});
  await page.locator('[data-picker-reasoning]').selectOption('high');await page.locator('[data-picker-pin]').uncheck();
  await page.locator('[data-picker-apply]').click();await page.locator('#model-picker-dialog').waitFor({state:'hidden'});
  const saved=ops.find(o=>o.action==='select');assert.deepEqual(saved.selection,{choice_id:'native-haiku',reasoning:'high',revision:'r2',pin:false});
  await page.locator('[data-open-model-picker]').click();await page.locator('[data-picker-query]:enabled').waitFor();await page.locator('[data-picker-query]').fill('nonempty');await page.keyboard.press('Escape');assert.equal(await page.locator('#model-picker-dialog').isVisible(),false);
  assert.equal(ops.filter(o=>o.action==='select').length,1,'cancel must not save');
  await page.locator('[data-open-model-picker]').click();await page.locator('[data-picker-query]:enabled').waitFor();await page.locator('[data-picker-manage]').click();
  await page.locator('#model-dialog').waitFor();assert.equal(await page.locator('#model-key').inputValue(),'');assert.match(await page.locator('#key-hint').textContent(),/已有密钥/);await page.locator('#model-dialog .close-dialog').first().click();
  await page.locator('[data-open-model-picker]').click();await page.locator('[data-picker-query]:enabled').waitFor();
  for(const width of [1100,736,390]){await page.setViewportSize({width,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);const box=await page.locator('#model-picker-dialog').boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width+1&&box.y>=0&&box.y+box.height<=845);assert.equal(await page.locator('[data-picker-apply]').isVisible(),true);assert.equal(await page.locator('.model-picker__pin>span').isVisible(),true);assert.equal(await page.locator('.model-picker__footer').evaluate(el=>getComputedStyle(el).display),'block');}
  assert.deepEqual(errors,[]);await page.close();console.log(`ok - ${host}: real panel opens/searches/native thinking/select payload/cancel/masked-key management/mobile without provider requests`);
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
