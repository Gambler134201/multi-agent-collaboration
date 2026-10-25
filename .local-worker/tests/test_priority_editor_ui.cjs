// Pure view and actual panel DOM. All service/provider requests are intercepted.
const {chromium}=require('./browser_helpers.cjs');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const directory=path.resolve(__dirname,'../panel');
const ctx=vm.createContext({});vm.runInContext(fs.readFileSync(path.join(directory,'routing-priority-view.js'),'utf8'),ctx);
const view=ctx.ModelDeskPriorityView;
const original={enabled:true,readable:true,slots:[{label:'First','model_id':'a',unavailable_reason:''},
 {label:'Missing free account',model_id:null,unavailable_reason:'explicit hole'},
 {label:'Second',model_id:'b',unavailable_reason:''},{label:'Removed model',model_id:'deleted',unavailable_reason:''}]};
const base={id:'a',name:'A model',model:'a-model',default_agent:'workbuddy',enabled:true};
let html=view.render({models:[base,{...base,id:'constructor',name:'Constructor model'}],policy:original},{});
assert.match(html,/workbuddy|WorkBuddy/);assert.match(html,/Constructor model/);assert.match(html,/explicit hole/);assert.match(html,/已缺失/);
html=view.render({models:[{id:'a',name:'Legacy model'}],policy:{enabled:false,slots:[original.slots[0]]}},{});
assert.ok(!html.includes('已停用'));assert.ok(!/data-priority-enabled[^>]*checked/.test(html));assert.match(html,/沿用现有的自动路由规则/);
html=view.render({models:[base],policy:{...original,readable:false,error:'broken policy'}},{dirty:true});assert.match(html,/data-priority-save disabled/);assert.match(html,/data-priority-select="0"[^>]*disabled/);assert.match(html,/broken policy/);
const reordered={slots:[{label:'Disabled moved',model_id:'a',unavailable_reason:''}]};
html=view.render({models:[{...base,enabled:false}],policy:{...original,slots:[{label:'Old',model_id:null,unavailable_reason:'WRONG INDEX REASON'}]}},reordered);
assert.ok(!html.includes('WRONG INDEX REASON'),'draft empty reason must not inherit index neighbor');
html=view.render({models:[{...base,id:'"><script>BAD</script>',name:'<img onerror=BAD()>'}],policy:original},{error:'<script>BAD</script>',dirty:true,busy:true});
assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img'));assert.match(html,/&lt;img/);assert.match(html,/data-priority-save disabled/);
html=view.render({models:[base],policy:original},{dirty:true,conflict:true});assert.match(html,/data-priority-save disabled/);assert.match(html,/data-priority-reset>/);
html=view.render({models:[base],policy:original},{slots:Array.from({length:20},()=>original.slots[1]),dirty:true});assert.match(html,/data-priority-add disabled/);assert.equal((html.match(/data-priority-select=/g)||[]).length,20);
console.log('ok - priority pure view: actual Agent labels, hostile strings/IDs, missing/disabled/empty/reordered reasons, busy/conflict/max20');
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{for(const host of ['codex','dsh']){
  const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[],writes=[],pickerOps=[];
  page.on('pageerror',e=>errors.push(e.message));
  const model={...base,provider:'custom',api_base:'https://fixture.invalid/v1',default_agent:'dsh',max_input_tokens:32768,max_output_tokens:4096};
  const models=[model,{...model,id:'b',name:'B model'},{...model,id:'c',name:'Disabled model',enabled:false}];
  const runtime={name:'DeepSeek Harness',ready:true,access_mode:'project',configuration:{model_fields:[]}};
  const fixture={models,default_model:'a',tasks:[],view:{mode:'project',project:'C:\\fixture'},execution_agents:{dsh:runtime},
   workflow:{enabled:true,execution_agent:'dsh',model_scope:'all',paid_allowed:true,daily:{}},stages:{stats:{},recovery:[]},codex_usage:{status:'unavailable',buckets:[]},
   routing_priority:structuredClone(original),execution_endpoints:{readable:true,rows:[{id:'dsh',name:'DeepSeek Harness',enabled:true,ready:true,model,models,runtime}]}};
  let failNext=false;
  const nativeCatalog=engine=>({endpoint:engine,label:engine==='workbuddy'?'WorkBuddy':'CommandCode',revision:'catalog-'+engine,
   sources:[{id:'native:'+engine,label:engine+' 登录账户',kind:'native'}],current_model:'',current_reasoning:'default',models:[
    {id:'choice:'+engine+'-a',source_id:'native:'+engine,name:'GLM fixture',model:'glm-fixture',reasoning:[{value:'default',label:'默认'},{value:'high',label:'高'}],selectable:true,registered_id:null,choice_revision:'token-'+engine+'-a'},
    {id:'choice:'+engine+'-b',source_id:'native:'+engine,name:'MiniMax fixture',model:'minimax-fixture',reasoning:[{value:'default',label:'默认'},{value:'high',label:'高'}],selectable:true,registered_id:null,choice_revision:'token-'+engine+'-b'}]});
  await page.route('http://priority.invalid/**',async route=>{
   const req=route.request(),p=new URL(req.url()).pathname;
   if(p==='/'||(/^\/[a-z0-9-]+\.(js|css)$/.test(p)&&fs.existsSync(path.join(directory,p.slice(1))))){const name=p==='/'?'index.html':p.slice(1);return route.fulfill({body:fs.readFileSync(path.join(directory,name)),contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});}
   let data=p==='/api/state'?fixture:p==='/api/session'?{token:'fixture'}:p==='/api/tree'?{goals:[],stats:{},view:fixture.view}:p==='/api/projects'?{projects:[],recent_projects:[]}:{};
   if(req.method()==='POST'){
    const body=req.postDataJSON();
    if(body.endpoint_settings?.values.model_picker){const op=body.endpoint_settings.values.model_picker;pickerOps.push(op);assert.notEqual(op.action,'select','Slot picker must not save endpoint defaults');data={ok:true,catalog:nativeCatalog(body.endpoint_settings.id),model_called:false};}
    if(body.routing_priority){
     writes.push(body);
     if(failNext){failNext=false;return route.fulfill({status:503,json:{error:'保存失败 fixture'}});}
     const next=body.routing_priority,current={enabled:fixture.routing_priority.enabled,slots:fixture.routing_priority.slots};
     if(JSON.stringify(next.expected)!==JSON.stringify(current))return route.fulfill({status:409,json:{error:'模型使用顺序已经变化，请重新加载后再保存'}});
     const mapping={},saved_models=[];
     for(const selection of next.selections||[]){const m={id:'registered-'+selection.id.slice(8),name:selection.endpoint+' fixture',model:'selected-native',default_agent:selection.endpoint,enabled:true,native_reasoning:{value:selection.reasoning}};mapping[selection.id]=m.id;saved_models.push(m);fixture.models.push(m);}
     fixture.routing_priority={schema:1,enabled:next.enabled,slots:next.slots.map(s=>mapping[s.model_id]?{...s,model_id:mapping[s.model_id]}:s),readable:true,error:''};data={ok:true,...fixture.routing_priority,saved_models};
    }
   }
   return route.fulfill({json:data});
  });
  await page.goto(`http://priority.invalid/?host=${host}&project=C%3A%5Cfixture`);await page.locator('[data-priority-select="0"]').waitFor();
  const values=()=>page.locator('[data-priority-select]').evaluateAll(rows=>rows.map(r=>r.value));
  assert.deepEqual(await values(),['a','','b','deleted']);assert.equal(writes.length,0);assert.equal(await page.locator('[data-priority-save]').isDisabled(),true);
  assert.equal(await page.locator('#routing-priority-summary').isVisible(),false);
  await page.locator('[data-priority-select="0"]').selectOption('b');assert.deepEqual(await values(),['b','','a','deleted']);assert.equal(writes.length,0);
  await page.evaluate(()=>refresh());assert.deepEqual(await values(),['b','','a','deleted'],'polling preserves draft');
  await page.locator('[data-priority-down="0"]').click();assert.deepEqual(await values(),['','b','a','deleted']);
  await page.locator('[data-priority-reset]').click();assert.deepEqual(await values(),['a','','b','deleted']);
  await page.locator('[data-priority-add]').click();assert.equal((await values()).length,5);await page.locator('[data-priority-select="4"]').selectOption('c');
  assert.match(await page.locator('.priority-editor__slot').last().textContent(),/已停用/);await page.locator('[data-priority-remove="4"]').click();
  await page.locator('[data-priority-select="0"]').selectOption('b');await page.locator('[data-priority-save]').click();await page.waitForFunction(()=>document.querySelector('[data-priority-save]').disabled);
  assert.equal(writes.length,1);assert.deepEqual(writes[0].routing_priority.expected,{enabled:true,slots:original.slots});assert.deepEqual(fixture.routing_priority.slots.map(s=>s.model_id),['b',null,'a','deleted']);
  await page.locator('[data-priority-enabled]').uncheck();assert.equal(fixture.routing_priority.enabled,true,'toggle drafts until save');await page.locator('[data-priority-save]').click();await page.waitForFunction(()=>document.querySelector('[data-priority-save]').disabled);assert.equal(fixture.routing_priority.enabled,false);
  // External changes visible during polling block stale drafts, reset picks up latest.
  await page.locator('[data-priority-select="0"]').selectOption('a');fixture.routing_priority.slots=structuredClone(original.slots);await page.evaluate(()=>refresh());
  assert.equal(await page.locator('[data-priority-save]').isDisabled(),true);assert.match(await page.locator('.priority-editor__conflict').textContent(),/更新/);
  await page.locator('[data-priority-reset]').click();assert.deepEqual(await values(),['a','','b','deleted']);
  // External change between the last poll and POST is rejected by expected snapshot.
  await page.locator('[data-priority-select="0"]').selectOption('b');fixture.routing_priority.enabled=true;await page.locator('[data-priority-save]').click();await page.locator('.priority-editor__error').waitFor();
  assert.equal(await page.locator('[data-priority-save]').isDisabled(),true);await page.evaluate(()=>refresh());await page.locator('[data-priority-reset]').click();
  // Transport error preserves draft and permits an explicit retry, no automatic retry.
  await page.locator('[data-priority-select="0"]').selectOption('b');const before=writes.length;failNext=true;await page.locator('[data-priority-save]').click();await page.locator('.priority-editor__error').waitFor();assert.equal(writes.length,before+1);assert.deepEqual(await values(),['b','','a','deleted']);assert.equal(await page.locator('[data-priority-save]').isEnabled(),true);
  await page.locator('[data-priority-reset]').click();
  // Full shared client/model picker writes a draft; final priority save batches it.
  const beforePick=writes.length;
  await page.locator('[data-priority-pick="1"]').click();await page.locator('[data-picker-client]').waitFor();
  assert.equal(await page.locator('[data-picker-client]').inputValue(),'');assert.equal(await page.locator('[data-picker-pin]').count(),0);assert.equal(await page.locator('[data-picker-apply]').isDisabled(),true);
  assert.match(await page.locator('.model-picker__title').textContent(),/第 2 顺位/);
  await page.locator('[data-picker-client]').selectOption('workbuddy');await page.locator('[data-picker-query]:enabled').waitFor();await page.locator('[data-picker-query]').fill('GLM');
  await page.locator('[data-picker-choice="choice:workbuddy-a"]').click();await page.locator('[data-picker-reasoning]:enabled').waitFor();await page.locator('[data-picker-reasoning]').selectOption('high');
  await page.keyboard.press('Escape');assert.equal((await values())[1],'','Cancel must not alter the slot');
  await page.locator('[data-priority-pick="1"]').click();await page.locator('[data-picker-client]').selectOption('workbuddy');await page.locator('[data-picker-choice="choice:workbuddy-a"]:enabled').waitFor();
  await page.locator('[data-picker-choice="choice:workbuddy-a"]').click();await page.locator('[data-picker-reasoning]:enabled').waitFor();await page.locator('[data-picker-reasoning]').selectOption('high');await page.locator('[data-picker-apply]').click();
  await page.locator('#model-picker-dialog').waitFor({state:'hidden'});assert.match((await values())[1],/^pending:/);assert.equal(writes.length,beforePick);assert.match(await page.locator('.priority-editor__slot').nth(1).textContent(),/WorkBuddy.*思考：高/);
  await page.evaluate(()=>refresh());assert.match((await values())[1],/^pending:/,'Polling preserves native choice draft');
  await page.locator('[data-priority-pick="1"]').click();await page.locator('[data-picker-query]:enabled').waitFor();assert.equal(await page.locator('[data-picker-client]').inputValue(),'workbuddy');assert.equal(await page.locator('[data-picker-reasoning]').inputValue(),'high');
  await page.setViewportSize({width:390,height:844});const pickerBox=await page.locator('#model-picker-dialog').boundingBox();assert.ok(pickerBox.x>=0&&pickerBox.x+pickerBox.width<=391&&pickerBox.y>=0&&pickerBox.y+pickerBox.height<=845);assert.equal(await page.locator('[data-picker-apply]').isVisible(),true);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);await page.setViewportSize({width:1280,height:1000});await page.keyboard.press('Escape');
  await page.locator('[data-priority-pick="3"]').click();await page.locator('[data-picker-client]').selectOption('commandcode');await page.locator('[data-picker-choice="choice:commandcode-b"]:enabled').waitFor();
  await page.locator('[data-picker-choice="choice:commandcode-b"]').click();await page.locator('[data-picker-reasoning]').selectOption('high');await page.locator('[data-picker-apply]').click();await page.locator('#model-picker-dialog').waitFor({state:'hidden'});
  assert.equal(writes.length,beforePick);await page.locator('[data-priority-save]').click();await page.waitForFunction(()=>document.querySelector('[data-priority-save]').disabled);
  const batch=writes.at(-1).routing_priority;assert.equal(batch.selections.length,2);assert.deepEqual(batch.selections.map(s=>[s.endpoint,s.reasoning]),[['workbuddy','high'],['commandcode','high']]);
  assert.equal((await values()).some(v=>v.startsWith('pending:')),false);assert.ok(pickerOps.every(op=>['catalog','details','refresh'].includes(op.action)));
  // Normal card picker still retains its endpoint default/pin behavior.
  await page.locator('[data-open-model-picker="dsh"]').click();await page.locator('[data-picker-query]:enabled').waitFor();assert.equal(await page.locator('[data-picker-client]').count(),0);assert.equal(await page.locator('[data-picker-pin]').count(),1);await page.keyboard.press('Escape');
  for(const width of [1280,736,390,360]){await page.setViewportSize({width,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${host}/${width} overflow`);const box=await page.locator('#routing-priority-editor').boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width+1);}
  assert.deepEqual(errors,[]);await page.close();console.log(`ok - ${host}: select/swap/move/add/remove/reset/save/enable, polling draft, optimistic conflict, transport failure and responsive`);
 }}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
