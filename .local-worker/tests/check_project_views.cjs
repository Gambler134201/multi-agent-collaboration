// Real panel frontend against isolated HTTP fixtures: simplified project entry
// (current project name + lightweight switch menu), native picker, real storage state.
// Every response is served by the local fixture below; no live workbench or record
// is read or written, so the check stays offline by default.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('./browser_helpers.cjs');
const web=path.resolve(__dirname,'../panel');
const A='C:\\fixture\\A',B='C:\\fixture\\B',N='C:\\fixture\\New';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const base={models:[{id:'home',name:'Qwen',provider:'local',api_base:'http://127.0.0.1:9/v1',model:'fixture-model',enabled:true,
    bypass_proxy:true,max_input_tokens:32768,max_output_tokens:4096,last_check:{ok:true,latency_ms:120},
    default_agent:'follow_global',default_agent_valid:true}],
  default_model:'home',workspace:'C:\\fixture',
  agent:{ready:true,edit_sandbox_mode:'danger-full-access',chat_sandbox_mode:'read-only'},
  workflow:{enabled:true,readable:true,error:'',paid_allowed:true,enforce_paid_budget:false,enforce_model_call_limit:false,
    task_budget_cny:5,daily_budget_cny:20,max_model_calls:30,model_scope:'all',execution_agent:'dsh',
    daily:{settled_cny:0,has_actual:false,unsettled_cny:0,unknown_calls:0}},
  execution_agents:{dsh:{name:'DeepSeek Harness',ready:true,configuration:{description:'',model_fields:[]}},
    pi:{name:'Pi Agent',ready:true,configuration:{description:'',model_fields:[]}}},
  codex_usage:{status:'ready',buckets:[]},
  delegation:{delegated:0,executor_completed:0,accepted:0,returned:0,recovery_pending:0},
  stages:{stats:{goals:0,stages:0,accepted:0,recovery_pending:0,by_status:{}},recovery:[]},
  server_time:new Date().toISOString()};
async function main(){
 const mutations=[],errors=[],requests=[],storageReads=[];
 let nextPick={status:'cancelled'},pickerResult=null,pickCount=0,pickCreateDelayMs=0;
 const tasks=[A,B].map((project,i)=>({id:i?'b':'a',title:i?'TASK_B':'TASK_A',kind:'chat',engine:'dsh',model_id:'home',model_name:'Qwen',project,status:'completed',phase:'回复完成',dsh_session_id:'s'+i,created_at:new Date().toISOString()}));
 const storage={
  [A]:{path:A+'\\.model-desk\\records.db',project:A,scope:'project',initialized:true,exists:true,writable:true,linked:false,reason:'',size_bytes:40960,free_bytes:8192},
  [B]:{path:B+'\\.model-desk\\records.db',project:B,scope:'project',initialized:true,exists:true,writable:true,linked:false,reason:'',size_bytes:20480,free_bytes:0}};
 const uninitialized=project=>({path:project+'\\.model-desk\\records.db',project,scope:'project',initialized:false,exists:false,writable:true,linked:false,reason:'项目尚未初始化记录库',size_bytes:0,free_bytes:0});
 const legacyRow={path:'E:\\fixture-workbench\\stages.db',scope:'legacy',initialized:true,exists:true,size_bytes:65536};
 const server=http.createServer(async(req,res)=>{
  const pathname=new URL(req.url,'http://local').pathname;
  const json=(v,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(v));};
  if(!pathname.startsWith('/api/')){const file={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[pathname];if(!file)return json({},404);res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});return res.end(fs.readFileSync(path.join(web,file)));}
  let view;try{view=JSON.parse(Buffer.from(req.headers['x-model-desk-view']||'','base64url').toString('utf8'));}catch{return json({error:'missing explicit view'},400);}
  requests.push({pathname,view});if(req.method!=='GET'&&!['/api/projects/pick','/api/projects/remember'].includes(pathname))mutations.push(pathname);
  if(pathname==='/api/projects/pick'&&req.method==='POST'){pickCount++;if(pickCreateDelayMs)await sleep(pickCreateDelayMs);pickerResult={id:'c'.repeat(32),...nextPick};return json({id:pickerResult.id,status:'pending'});}
  if(pathname==='/api/projects/pick/'+'c'.repeat(32))return json(pickerResult);
  if(pathname==='/api/db/space'){
    storageReads.push(view.mode==='project'?view.project:view.mode);
    return view.mode==='project'
      ? json({scope:'project',project:view.project,databases:[storage[view.project]||uninitialized(view.project)]})
      : json({scope:view.mode,project:null,databases:[legacyRow]});
  }
  if(pathname==='/api/db/compact'&&req.method==='POST'){
    if(view.mode!=='project')return json({error:'请先明确选择要压缩的项目'},400);
    const row=storage[view.project];
    if(!row||!row.exists)return json({scope:'project',project:view.project,compacted:[],reclaimed_bytes:0,initialized:false,message:'该项目尚无 records.db，无需压缩'});
    return json({scope:'project',project:view.project,compacted:[{path:row.path,before:row.size_bytes,after:row.size_bytes-4096,reclaimed_bytes:4096}],reclaimed_bytes:4096});
  }
  const selected=view.mode==='all'?tasks:view.mode==='project'?tasks.filter(t=>t.project===view.project):[];
  const goals=selected.map(t=>({id:'g'+t.id,title:'GOAL_'+t.id.toUpperCase(),project:t.project,status:'active',stages:[],stage_count:0,accepted_count:0}));
  const stats={goals:goals.length,stages:0,accepted:0,recovery_pending:0,by_status:{}};
  if(pathname==='/api/session')return json({token:'fixture'});
  if(pathname==='/api/projects')return json({projects:[A,B,N].map(p=>({path:p,label:p})),recent_projects:[{path:A}]});
  if(pathname==='/api/projects/remember')return json({projects:[{path:view.project}]});
  if(pathname==='/api/state')return json({...base,tasks:selected,workspace:'E:\\wrong\\installation',view,execution_activity:{scope:'global',total:1,complete:true,tasks:[{id:'active-a',title:'LIVE_JOB_A',status:'running',project:A,model_id:'home',model_name:'Qwen'}]},delegation:{delegated:0},stages:{stats,recovery:[]},workflow:{...base.workflow,paid_allowed:false,daily_scope:'global'}});
  if(pathname==='/api/tree')return json({goals,stats,recovery:[],view});
  const id=pathname.split('/')[3],task=selected.find(t=>t.id===id);
  if(pathname.endsWith('/observability'))return json({task_id:id,cost:{known:false},view});
  if(pathname==='/api/tasks/a')return setTimeout(()=>json({task,prompt:'OLD_A_PROMPT'}),350);
  if(task)return json({task,prompt:'B_PROMPT'});
  return json({error:'fixture denied'},403);
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1280,height:800}});
  context.setDefaultTimeout(12000);
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  const url=`http://127.0.0.1:${server.address().port}/`;
  const checks=[];const ok=name=>checks.push(name);
  const openMenu=async p=>{const menu=p.locator('#project-view-menu');if(!(await menu.isVisible()))await p.locator('#project-view-switch').click();await menu.waitFor({state:'visible'});};
  const menuPick=async(p,project)=>{await openMenu(p);await p.evaluate(proj=>{const node=[...document.querySelectorAll('#project-view-known [data-project]')].find(n=>n.dataset.project===proj);if(!node)throw new Error('menu item missing: '+proj);node.click();},project);};
  const startPicker=async p=>{await openMenu(p);await p.locator('#project-view-choose').click();};
  const pickerSettled=p=>p.waitForFunction(()=>{const b=document.querySelector('#project-view-choose');return !!b&&!b.disabled;},{timeout:20000});
  const selectViaPicker=async(p,project)=>{nextPick={status:'selected',project};await startPicker(p);await pickerSettled(p);await p.waitForFunction(expected=>document.querySelector('#project-view-status').textContent.includes(expected),project,{timeout:15000});};

  await page.goto(url);await page.locator('.model-card').first().waitFor();
  // 1) unbound: only "尚未选择项目 + 选择项目"; no dropdown, path input, recent row or duplicate hint.
  assert.equal(await page.locator('.task-row').count(),0);
  assert.equal((await page.locator('#project-view-name').textContent()).trim(),'尚未选择项目');
  assert.equal((await page.locator('#project-view-switch').textContent()).trim(),'选择项目');
  assert.equal(await page.locator('#project-view-mode').count(),0);
  assert.equal(await page.locator('#project-view-recent').count(),0);
  assert.equal(await page.locator('input#project-view-path').count(),0);
  assert.match(await page.locator('#task-list').textContent(),/尚未选择项目/);
  assert.equal(await page.locator('#project-path-row').isVisible(),false);
  assert.equal(await page.locator('#execution-activity').isVisible(),true);
  assert.match(await page.locator('#execution-activity').textContent(),/LIVE_JOB_A/);
  assert.match(await page.locator('.model-card').first().textContent(),/正在执行/);
  ok('unbound shows only 尚未选择项目 + 选择项目, no dropdown/path input/recent row');

  // 2) the switch menu integrates known/recent projects and the system picker; Escape/outside click close it.
  await openMenu(page);
  assert.match(await page.locator('#project-view-menu').textContent(),/已知 \/ 最近项目/);
  assert.ok(await page.locator('#project-view-known [data-project]').count()>=3);
  assert.equal(await page.locator('#project-view-choose').isVisible(),true);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#project-view-menu').isVisible(),false);
  assert.equal(await page.locator('#project-view-switch').getAttribute('aria-expanded'),'false');
  await openMenu(page);await page.locator('h1').click();
  assert.equal(await page.locator('#project-view-menu').isVisible(),false);
  ok('switch menu lists known projects and closes on Escape/outside click');

  // 3) explicit menu selection; the full path stays hover/viewable and never becomes a writable input.
  await menuPick(page,B);await page.locator('[data-task-id="b"]').waitFor();
  assert.equal((await page.locator('#project-view-name').textContent()).trim(),'B');
  assert.equal(await page.locator('#project-view-name').getAttribute('title'),B);
  assert.equal(await page.locator('#project-view-path').textContent(),B);
  assert.equal(await page.locator('#project-path-row').isVisible(),false);
  await page.locator('#project-view-name').click();
  assert.equal(await page.locator('#project-path-row').isVisible(),true);
  assert.equal(await page.locator('#project-view-path').evaluate(el=>el.tagName),'CODE');
  assert.equal(await page.locator('#project-view-path').evaluate(el=>el.isContentEditable),false);
  await page.locator('#project-view-name').click();
  assert.equal((await page.locator('#project-view-switch').textContent()).trim(),'切换项目');
  ok('menu selection binds B; full path is shown on hover/title and expandable, read-only');

  // 4) native picker: selected / cancelled / failed each keep or apply the exact project.
  await selectViaPicker(page,A);
  assert.equal(await page.locator('#project-view-path').textContent(),A);
  assert.equal(new URL(page.url()).searchParams.get('project'),A);
  nextPick={status:'cancelled'};await startPicker(page);await pickerSettled(page);
  assert.match(await page.locator('#toast').textContent(),/取消选择/);
  assert.equal(await page.locator('#project-view-path').textContent(),A);
  nextPick={status:'failed',error:'picker fixture error'};await startPicker(page);await pickerSettled(page);
  assert.match(await page.locator('#toast').textContent(),/picker fixture error/);
  assert.equal(await page.locator('#project-view-path').textContent(),A);
  assert.equal(pickCount,3);
  ok('native picker selected/cancelled/failed keeps or applies the exact project');

  // 5) reload / same-panel return remember the binding; an explicit URL project wins over it.
  await page.reload();await page.locator('[data-task-id="a"]').waitFor();
  assert.equal(await page.locator('#project-view-path').textContent(),A);
  await page.goto(url);await page.locator('[data-task-id="a"]').waitFor();
  assert.equal(await page.locator('#project-view-path').textContent(),A,'same panel session restores its binding');
  await page.goto(`${url}?project=${encodeURIComponent(B)}`);await page.locator('[data-task-id="b"]').waitFor();
  assert.equal(await page.locator('#project-view-path').textContent(),B,'an explicit URL project wins over the remembered one');
  ok('refresh/same-panel memory and explicit-URL priority');

  // 6) independent panels: a new panel starts unbound and restores a known project only on explicit choice.
  const other=await context.newPage();other.on('pageerror',e=>errors.push(e.message));
  await other.goto(url);await other.locator('.model-card').first().waitFor();
  assert.equal((await other.locator('#project-view-name').textContent()).trim(),'尚未选择项目');
  assert.equal(await other.locator('.task-row').count(),0);
  await menuPick(other,A);await other.locator('[data-task-id="a"]').waitFor();
  assert.equal(await page.locator('[data-task-id="b"]').count(),1);
  assert.equal(await other.locator('[data-task-id="b"]').count(),0);
  ok('panels keep independent projects; a new panel restores a known project only when explicitly chosen');

  // 7) real storage state: an uninitialized project is not reported as lost records, and nothing is written.
  await other.goto(url);await other.locator('[data-task-id="a"]').waitFor();
  await menuPick(other,N);
  await other.waitForFunction(()=>document.querySelector('#project-view-status').textContent.includes('New'));
  await other.waitForFunction(()=>/尚未初始化记录/.test(document.querySelector('#task-list').textContent));
  assert.match(await other.locator('#project-storage-state').textContent(),/尚未初始化记录/);
  assert.match(await other.locator('#project-storage-state').textContent(),/不是记录丢失/);
  assert.equal(await other.locator('#project-storage-state').isVisible(),true);
  assert.ok(storageReads.includes(N),'the uninitialized project state is read from /api/db/space, not guessed');
  assert.deepEqual(mutations,[],'binding an uninitialized project must not write any record');
  ok('uninitialized project shows 尚未初始化记录 (never lost records) and writes no record');

  // 8) a late task response after a project switch never lands in the new detail.
  await page.goto(`${url}?project=${encodeURIComponent(A)}`);await page.locator('[data-task-id="a"]').waitFor();
  await page.locator('[data-task-id="a"]').click();
  await page.locator('[data-tab="prompt"]').evaluate(e=>e.click());
  await menuPick(page,B);await page.locator('[data-task-id="b"]').waitFor();
  await sleep(500);
  assert.ok(!(await page.locator('#task-detail').textContent()).includes('OLD_A_PROMPT'));
  ok('a late task response after switching projects is discarded');

  // 9) a late picker result never overrides a newer explicit choice made while it was pending.
  await page.goto(`${url}?project=${encodeURIComponent(A)}`);await page.locator('[data-task-id="a"]').waitFor();
  nextPick={status:'selected',project:N};pickCreateDelayMs=1200;
  await startPicker(page);
  await sleep(1350);
  await menuPick(page,B);await page.locator('[data-task-id="b"]').waitFor();
  await pickerSettled(page);await sleep(900);
  pickCreateDelayMs=0;
  assert.equal(await page.locator('#project-view-path').textContent(),B,'stale picker must not overwrite the newer choice');
  ok('a stale picker result never overwrites a newer explicit project choice');

  // 10) new task / goal prefill the explicitly bound project directory.
  await page.locator('#new-task').click();assert.equal(await page.locator('#task-project').inputValue(),B);await page.locator('#task-dialog .close-dialog').first().click();
  await page.locator('#new-goal').click();assert.equal(await page.locator('#goal-project').inputValue(),B);await page.locator('#goal-dialog .close-dialog').first().click();
  ok('new task/goal prefill the bound project directory');

  // 11) all-projects is an explicit URL/host view only; record management still needs one project.
  await page.goto(`${url}?view=all`);await page.locator('[data-task-id="b"]').waitFor();
  assert.equal(await page.locator('.task-row').count(),2);
  assert.equal((await page.locator('#project-view-name').textContent()).trim(),'全部项目（显式视图）');
  assert.equal((await page.locator('#project-view-switch').textContent()).trim(),'选择项目');
  await page.locator('#task-history-open').click();
  assert.equal(await page.evaluate(()=>$('task-records-dialog').open),false);
  assert.match(await page.locator('#toast').textContent(),/只按具体项目操作/);
  ok('all-projects stays explicit; record management still requires a concrete project');

  // 12) theme + narrow screens: bar and menu never overflow horizontally, menu stays inside the viewport.
  const layouts=[];
  for(const spec of [{width:1280,height:600,font:'standard',theme:'light'},{width:480,height:640,font:'xlarge',theme:'dark'},{width:380,height:560,font:'xlarge',theme:'light'}]){
   await page.setViewportSize({width:spec.width,height:spec.height});
   await page.locator('#font-scale').selectOption(spec.font);await page.locator('#theme-mode').selectOption(spec.theme);
   await page.goto(`${url}?project=${encodeURIComponent(A)}`);await page.locator('[data-task-id="a"]').waitFor();
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,`bar overflow at ${spec.width}`);
   await openMenu(page);
   const box=await page.locator('#project-view-menu').boundingBox();
   assert.ok(box.x>=-1&&box.x+box.width<=spec.width+1,JSON.stringify({spec,box}));
   assert.ok(box.height<=spec.height,JSON.stringify({spec,box}));
   await page.keyboard.press('Escape');
   layouts.push(spec);
  }
  // 13) settings dialog keeps its actions visible at the same narrow sizes (unchanged behavior).
  for(const spec of [{width:1280,height:600,font:'standard',theme:'light'},{width:480,height:640,font:'xlarge',theme:'dark'},{width:800,height:400,font:'xlarge',theme:'light'}]){
   await page.setViewportSize({width:spec.width,height:spec.height});await page.locator('#font-scale').selectOption(spec.font);await page.locator('#theme-mode').selectOption(spec.theme);
   await page.locator('#workflow-settings').click();
   const save=page.locator('#workflow-form button[type="submit"]'),cancel=page.locator('#workflow-form .dialog-footer button[type="button"]');
   for(const button of [save,cancel]){const rect=await button.boundingBox();assert.ok(rect.y>=0&&rect.y+rect.height<=spec.height+1,JSON.stringify({spec,rect}));assert.equal(await button.isVisible(),true);await button.click({trial:true});}
   await page.locator('#workflow-paid').check();assert.match(await page.locator('#workflow-save-status').textContent(),/未保存/);
   await page.locator('.workflow-scroll').evaluate(e=>{e.scrollTop=e.scrollHeight;});await page.locator('#workflow-max-calls').scrollIntoViewIfNeeded();
   const r=await save.boundingBox();assert.ok(r.y+r.height<=spec.height+1);await cancel.click();
  }
  assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
  const report={passed:true,checks,layouts,paid_generation:false,pickCount,mutations,errors,storageReads:[...new Set(storageReads)]};
  fs.writeFileSync(path.join(__dirname,'project-views-ui-report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
 }catch(error){console.error(error);throw error;}finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
