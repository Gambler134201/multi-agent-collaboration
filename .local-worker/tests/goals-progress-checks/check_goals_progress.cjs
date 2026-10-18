// Home-page goals on demand + history goals inside 任务记录管理.
// Real panel front-end files served by an isolated local mock HTTP server; the live
// workbench (127.0.0.1:8765) is never contacted, no real record/config is touched and
// every save/delete/dispatch path stays unexercised except the mocked preview/cancel.
//   node .local-worker/tests/goals-progress-checks/check_goals_progress.cjs
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('../browser_helpers.cjs');
const web=path.resolve(__dirname,'..','..','panel');
// Generated screenshots/reports go to the ignored top-level tests evidence paths.
const outDir=path.resolve(__dirname,'..');
const A='C:\\fixture\\goalsA',B='C:\\fixture\\goalsB',C='C:\\fixture\\goalsEmpty';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const ACTIVE_TITLE='进行中目标：首页按需显示，历史目标放进记录管理，长标题在窄屏下必须省略号截断而不是撑破布局';
const DONE_TITLE='历史目标甲（已完成）',CANCEL_TITLE='历史目标乙（已取消）',B_TITLE='B 项目历史目标（已完成）';

const treeTask=(id,title,status,reviewState,reviewLabel)=>({task_id:id,title,status,phase:'回复完成',kind:'edit',engine:'dsh',
  model_id:'home',model_name:'FIXTURE 本地',model:'fixture-model',created_at:'2026-10-17 09:00:00',ended_at:'2026-10-17 09:20:00',
  review_state:reviewState,review_label:reviewLabel,review_error:null,files:['panel/app.js'],files_readonly:false,
  file_hashes:{'panel/app.js':'abcdef1234567890'},children:{},children_state:{state:'未验证'}});
const stage=(id,ordinal,title,status,label,extra={})=>({id,ordinal,title,status,status_label:label,kind:'edit',
  kind_label:'完全权限修改 · edit',objective:'按验收条件交付',session_policy:'new_session',session_key:'k-'+id,
  depends_on:[],readiness:{ready:true,reasons:[],dependency_states:{}},
  delivery:{deliveries:[],warnings:[]},tasks:[],...extra});
const goal=(id,title,status,label,stages)=>({id,title,objective:'真实记录中的目标说明',status,status_label:label,
  created_at:'2026-10-17 08:00:00',updated_at:'2026-10-17 10:00:00',stage_count:stages.length,
  accepted_count:stages.filter(row=>['accepted','final_accepted'].includes(row.status)).length,stages});

function goalsFixture(){
  return{
    [A]:[
      goal('g-active',ACTIVE_TITLE,'active','进行中',[
        stage('s-accept',1,'调查现有渲染',  'accepted','独立验收通过',{delivery:{deliveries:[{task_id:'t-hand',summary:'交接摘要：首页只保留 active 目标',fresh:{valid:true,message:'hash 一致'},files:[{path:'panel/app.js',sha256:'abcdef1234567890'}]}],warnings:[]},tasks:[treeTask('t-hand','交接任务','completed','accepted','独立验收通过')]}),
        stage('s-exec',2,'实现折叠行','executor_completed','执行器完成 · 待独立验收'),
        stage('s-run',3,'离线浏览器检查','running','执行中'),
        stage('s-rec',4,'恢复中断的检查','recovery_pending','待恢复 · 未完成派发需人工确认'),
        stage('s-block',5,'窄屏布局复核','blocked','受阻 · 需主脑判断'),
        stage('s-pending',6,'等待前置验收','pending','待前置阶段通过',{readiness:{ready:false,reasons:['前置阶段未验收'],dependency_states:{'s-exec':'executor_completed'}}})]),
      goal('g-done',DONE_TITLE,'completed','已完成',[
        stage('s-done1',1,'旧阶段一','accepted','独立验收通过'),stage('s-done2',2,'旧阶段二','final_accepted','主脑介入 · 最终验收通过'),
        stage('s-done3',3,'旧阶段三','accepted','独立验收通过')]),
      goal('g-cancel',CANCEL_TITLE,'cancelled','已取消',[stage('s-cancel',1,'未继续的阶段','cancelled','已取消')])],
    [B]:[goal('g-b',B_TITLE,'completed','已完成',[stage('s-b1',1,'B 阶段一','accepted','独立验收通过'),stage('s-b2',2,'B 阶段二','accepted','独立验收通过')])],
    [C]:[],
  };
}
const mkRecord=(id,status,deletable,blocked_reason='')=>({id,title:`记录 ${id}`,status,created_at:'2026-10-12 09:00:00',
  ended_at:'2026-10-12 09:20:00',model_name:'FIXTURE 本地',goal_id:'g-done',group_title:'组甲',deletable,blocked_reason});
function recordsFixture(){
  return{[A]:[mkRecord('t-done','completed',true),mkRecord('t-locked','completed',false,'阶段已通过独立验收'),
                 mkRecord('t-running','running',false,'任务正在执行，不能删除')],
        [B]:[mkRecord('t-b','completed',true)],[C]:[]};
}
function taskFixture(){
  return{[A]:[{id:'t-run',title:'运行中的检查任务',kind:'edit',engine:'dsh',model_id:'home',model_name:'FIXTURE 本地',project:A,
               status:'running',phase:'模型执行中',created_at:new Date().toISOString()}],
        [B]:[{id:'t-b',title:'记录 t-b',kind:'chat',engine:'dsh',model_id:'home',model_name:'FIXTURE 本地',project:B,
              status:'completed',phase:'回复完成',created_at:new Date().toISOString()}],
        [C]:[]};
}

async function main(){
  const goals=goalsFixture(),records=recordsFixture(),tasks=taskFixture();
  let treeFail=false;
  const base={models:[{id:'home',name:'FIXTURE 本地',provider:'local',api_base:'http://127.0.0.1:9/v1',model:'fixture-model',
      enabled:true,bypass_proxy:true,max_input_tokens:32768,max_output_tokens:4096,last_check:{ok:true,latency_ms:120}}],
    default_model:'home',workspace:'C:\\fixture',agent:{ready:true,name:'DeepSeek Harness',version:'fixture'},
    execution_agents:{dsh:{name:'DeepSeek Harness'},pi:{name:'Pi Agent'}},execution_activity:{scope:'global',total:0,complete:true,tasks:[]},
    workflow:{enabled:true,paid_allowed:true,enforce_paid_budget:false,enforce_model_call_limit:false,task_budget_cny:5,
      daily_budget_cny:20,max_model_calls:30,daily:{settled_cny:0,has_actual:false,unsettled_cny:0,unknown_calls:0,calls:0}},
    codex_usage:{status:'ready',buckets:[]},delegation:{delegated:0,executor_completed:0,accepted:0,returned:0},
    stages:{stats:{goals:0,stages:0,accepted:0,recovery_pending:0,by_status:{}},recovery:[]},
    executor_capabilities:{schema:1,executor:'dsh',default_verdict:'deny',note:'fixture',models:[]},
    server_time:new Date().toISOString()};
  const mutations=[],errors=[],requests=[],previewBodies=[];
  const statsFor=rows=>{const stages=rows.flatMap(goal=>goal.stages);
    return{goals:rows.length,stages:stages.length,accepted:stages.filter(row=>['accepted','final_accepted'].includes(row.status)).length,
           recovery_pending:stages.filter(row=>row.status==='recovery_pending').length,by_status:{}}};
  const server=http.createServer((req,res)=>{
    const pathname=new URL(req.url,'http://local').pathname;
    const json=(value,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(value));};
    if(!pathname.startsWith('/api/')){
      const file={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[pathname];
      if(!file)return json({},404);
      res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});
      return res.end(fs.readFileSync(path.join(web,file)));
    }
    let view;try{view=JSON.parse(Buffer.from(req.headers['x-model-desk-view']||'','base64url').toString('utf8'));}
    catch{return json({error:'missing explicit view'},400);}
    const project=view.mode==='project'?view.project:null;
    requests.push((req.method==='GET'?'GET ':'POST ')+pathname+(project?'@'+project:''));
    if(req.method!=='GET'&&!['/api/projects/remember','/api/task-records/preview'].includes(pathname))mutations.push(pathname);
    if(pathname==='/api/session')return json({token:'fixture-token'});
    if(pathname==='/api/projects')return json({projects:[A,B,C].map(p=>({path:p,label:p})),
      recent_projects:[{path:A},{path:B},{path:C}]});
    if(pathname==='/api/projects/remember')return json({projects:[{path:project}]});
    if(pathname==='/api/db/space')return json({scope:view.mode,project,databases:[{path:(project||'')+'\\.model-desk\\records.db',
      project,scope:'project',initialized:true,exists:true,writable:true,linked:false,reason:'',size_bytes:4096,free_bytes:1024}]});
    if(pathname==='/api/state'){
      const rows=goals[project]||[];
      return json({...base,tasks:tasks[project]||[],view,workspace:'C:\\fixture',
        stages:{stats:statsFor(rows),recovery:[]},server_time:new Date().toISOString()});
    }
    if(pathname==='/api/tree'){
      if(treeFail==='payload')return json({goals:[],error:'目标库暂不可读'});
      if(treeFail==='schema')return json({stats:{}});
      if(treeFail)return json({error:'目标接口模拟故障'},500);
      const rows=goals[project]||[];
      return json({goals:rows,stats:statsFor(rows),recovery:[],note:'fixture',view});
    }
    if(pathname==='/api/observability')return json({tasks:[],stats:{tasks:0,shown:0},view});
    if(pathname==='/api/task-records')return json({project,tasks:records[project]||[],view});
    if(pathname==='/api/task-records/preview'){
      let body='';req.on('data',chunk=>body+=chunk);
      return req.on('end',()=>{
        const ids=JSON.parse(body||'{}').task_ids||[];previewBodies.push({project,ids});
        return json({project,token:'preview-token',expires_in:120,requested_ids:ids,added_ids:[],
          tasks:ids.map(id=>({id,title:`记录 ${id}`,status:'completed'})),goals:[{id:'g-done',title:DONE_TITLE}],
          stage_count:3,warnings:[]});
      });
    }
    if(pathname==='/api/task-records/delete')return json({error:'本检查不允许删除真实记录',deleted_tasks:0,deleted_goals:0,deleted_stages:0},409);
    if(pathname==='/api/tasks/t-hand/observability')return json({cost:{known:true,settled_cny:0.12345,settled_calls:1},provider_usage:{},meter:{},context:{note:'历史任务独立观测已载入'}});
    if(/^\/api\/tasks\/[^/]+$/.test(pathname))return json({task:(tasks[project]||[]).find(row=>row.id===pathname.split('/')[3])||null,prompt:'fixture'});
    return json({error:'not mocked: '+pathname},404);
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const url=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const checks=[];const ok=name=>checks.push(name);
  const newPage=async(width,height)=>{
    const context=await browser.newContext({viewport:{width,height}});
    context.setDefaultTimeout(15000);
    const page=await context.newPage();
    page.on('pageerror',error=>errors.push(error.message));
    return {context,page};
  };
  const settled=async page=>{await page.waitForFunction(()=>/最近同步/.test(document.getElementById('last-sync').textContent),
    null,{timeout:10000});await sleep(120);};
  try{
    // ---- 1) mixed states: only the active goal drives the home line ------------------
    const {context,page}=await newPage(1280,900);
    await page.goto(`${url}/?project=${encodeURIComponent(A)}`);
    await settled(page);
    assert.equal(await page.locator('#goals-panel').isVisible(),true,'home shows the block while a goal is active');
    assert.equal(await page.locator('#goals-detail').isVisible(),false,'the block starts collapsed');
    assert.equal(await page.locator('#goals-toggle').getAttribute('aria-expanded'),'false');
    const summary=await page.locator('#goals-summary').textContent();
    assert.ok(summary.includes('当前需求进度')===false,'the label lives outside the summary span');
    assert.ok(summary.includes('阶段 1/6 已验收'),'active-only stage progress, got: '+summary);
    assert.ok(summary.includes('待前置验收 1')&&!summary.includes('状态未知'),'pending is a known dependency wait: '+summary);
    assert.ok(summary.includes('执行完成待验收 1'),'executor completion is not acceptance: '+summary);
    assert.ok(summary.includes('待恢复 1')&&summary.includes('失败/受阻 1'),'failed/blocked/pending never look done: '+summary);
    for(const leaked of [DONE_TITLE,CANCEL_TITLE])assert.ok(!summary.includes(leaked),'history goal stays off the home line');
    assert.equal(await page.locator('#goal-count').textContent(),'1','only the active goal is counted');
    ok('mixed states: one collapsed line, active-only progress, acceptance separated from execution');

    // ---- 2) expand keeps the original tree, actions and history out ------------------
    await page.locator('#goals-toggle').click();
    assert.equal(await page.locator('#goals-detail').isVisible(),true);
    assert.equal(await page.locator('#goals-toggle').getAttribute('aria-expanded'),'true');
    assert.equal(await page.locator('#goals-tree .goal-node').count(),1,'only active goals live on the home page');
    assert.equal(await page.locator('#goals-tree .goal-node[data-goal="g-done"]').count(),0);
    assert.equal(await page.locator('#goals-tree .goal-node[data-goal="g-cancel"]').count(),0);
    const treeText=await page.locator('#goals-tree').textContent();
    assert.ok(treeText.includes('交接摘要：首页只保留 active 目标')&&treeText.includes('独立验收通过'),
      'handover and acceptance stay visible');
    assert.ok((await page.locator('#goals-tree [data-stage-action="dispatch"]').count())>=1,
      'existing stage actions are still reachable after expanding');
    assert.ok((await page.locator('#goals-tree [data-goal-action="complete"]').count())===1);
    ok('expanded row keeps goals/stages/tasks, handover, acceptance and the existing actions');

    // ---- 3) auto-poll keeps the user expansion and the opened inner node -------------
    await page.locator('#goals-tree .goal-node summary').first().click();
    assert.equal(await page.locator('#goals-tree .goal-node').first().evaluate(el=>el.open),true);
    await sleep(5000);                                                   // several live polls
    assert.equal(await page.locator('#goals-detail').isVisible(),true,'polling must not collapse the row');
    assert.equal(await page.locator('#goals-toggle').getAttribute('aria-expanded'),'true');
    assert.equal(await page.locator('#goals-tree .goal-node').first().evaluate(el=>el.open),true,
      'polling must not collapse a goal the user opened');
    ok('auto-poll preserves the expanded state and the opened goal node');

    // ---- 4) active -> completed leaves the home page, new-goal entry stays -----------
    goals[A][0].status='completed';goals[A][0].status_label='已完成';
    await page.waitForFunction(()=>document.getElementById('goals-panel').hidden===true,null,{timeout:10000});
    assert.equal(await page.locator('#goals-panel').isVisible(),false,'no active goal means no home block');
    assert.equal(await page.locator('#new-goal').isVisible(),true,'the new-goal entry survives without active goals');
    await page.locator('#new-goal').click();
    assert.equal(await page.locator('#goal-dialog').evaluate(el=>el.open),true,'new goal still opens the same dialog');
    assert.equal(await page.locator('#goal-project').inputValue(),A,'the bound project is prefilled unchanged');
    await page.locator('#goal-dialog .close-dialog').first().click();
    ok('completed goal disappears from home; 新建目标 entry still opens with the bound project');

    // ---- 5) history goals inside 任务记录管理 (read-only) ----------------------------
    await page.locator('#task-history-open').click();
    await page.waitForFunction(()=>document.getElementById('records-goals-count').textContent==='3',null,{timeout:10000});
    await page.locator('#records-goals').evaluate(el=>{el.open=true;});
    const history=await page.locator('#records-goals-list').textContent();
    assert.ok(history.includes(DONE_TITLE)&&history.includes(CANCEL_TITLE),'completed + cancelled goals are reachable');
    assert.ok(history.includes(ACTIVE_TITLE),'the goal that just finished is visible in the history entry');
    assert.ok(history.includes('交接摘要：首页只保留 active 目标'),'existing handover stays readable');
    assert.ok(history.includes('主脑介入 · 最终验收通过'),'acceptance records stay readable');
    assert.equal(await page.locator('#records-goals-list [data-stage-action]').count(),0,'history is read-only: no dispatch/cancel/requeue/sync');
    assert.equal(await page.locator('#records-goals-list [data-goal-action]').count(),0,'history is read-only: no goal actions');
    for(const node of ['[data-goal="g-active"]','[data-stage="s-accept"]','[data-task="t-hand"]']){
      const detail=page.locator('#records-goals-list '+node);
      if(!await detail.evaluate(el=>el.open))await detail.locator(':scope > summary').click();
    }
    await page.waitForFunction(()=>document.querySelector('#records-goals-list [data-task="t-hand"]').textContent.includes('历史任务独立观测已载入'),null,{timeout:6000});
    assert.ok(requests.includes('GET /api/tasks/t-hand/observability@'+A),'history lazy-loads task observations within the bound project');
    await page.locator('#task-records-refresh').click();
    await page.waitForFunction(()=>!document.getElementById('records-goals-list').textContent.includes('正在读取'),null,{timeout:6000});
    assert.equal(await page.locator('#records-goals-list [data-goal="g-active"]').evaluate(el=>el.open),true,'refresh retains the opened history goal');
    assert.equal(await page.locator('#records-goals-list [data-stage="s-accept"]').evaluate(el=>el.open),true,'refresh retains the opened history stage');
    ok('任务记录管理 lists completed/cancelled goals read-only with stages, tasks, handover and acceptance');

    // ---- 6) the existing record selection / preview / cancel protections -------------
    assert.equal(await page.locator('#task-records-preview').isDisabled(),true,'preview stays disabled without a selection');
    assert.equal(await page.locator('#task-records-list input[data-record-id="t-locked"]').isDisabled(),true,
      'accepted-stage records stay undeletable');
    assert.equal(await page.locator('#task-records-list input[data-record-id="t-running"]').isDisabled(),true,
      'running records stay undeletable');
    await page.locator('#task-records-list input[data-record-id="t-done"]').check();
    assert.equal(await page.locator('#task-records-preview').isDisabled(),false);
    await page.locator('#task-records-preview').click();
    await page.waitForFunction(()=>document.getElementById('task-records-confirm-dialog').open,null,{timeout:8000});
    assert.equal(await page.locator('#task-records-delete').isEnabled(),true);
    await page.locator('#task-records-cancel').click();
    assert.equal(await page.locator('#task-records-confirm-dialog').evaluate(el=>el.open),false);
    assert.ok(!mutations.includes('/api/task-records/delete'),'nothing was deleted by this check');
    await page.locator('#task-records-dialog .close-records-dialog').first().click();
    ok('record selection, locked rows, preview confirmation and cancel are unchanged');

    // ---- 7) project switch never leaks the previous project's goals ------------------
    await page.locator('#project-view-switch').click();
    await page.locator('#project-view-known .project-menu-item',{hasText:B}).click();
    await settled(page);
    assert.equal(await page.locator('#goals-panel').isVisible(),false,'project B has no active goal, so home stays hidden');
    const homeText=await page.locator('.app').textContent();
    for(const leaked of [ACTIVE_TITLE,DONE_TITLE,CANCEL_TITLE])
      assert.ok(!homeText.includes(leaked),`project A records must not follow into project B (${leaked})`);
    await page.locator('#task-history-open').click();
    await page.waitForFunction(()=>document.getElementById('records-goals-count').textContent==='1',null,{timeout:10000});
    await page.locator('#records-goals').evaluate(el=>{el.open=true;});
    const historyB=await page.locator('#records-goals-list').textContent();
    assert.ok(historyB.includes(B_TITLE),'project B shows its own history goal');
    assert.ok(!historyB.includes(DONE_TITLE),'project B never shows project A history');
    await page.locator('#task-records-dialog .close-records-dialog').first().click();
    ok('project switch hides the old goals and keeps history per project');

    // ---- 8) a failing tree is reported, never read as "no goals / all done" ----------
    treeFail=true;
    await page.waitForFunction(()=>document.getElementById('goals-panel').hidden===false,null,{timeout:10000});
    assert.equal(await page.locator('#goals-detail').isVisible(),false,'an unknown tree is not silently expanded');
    const goalError=await page.locator('#goals-error').textContent();
    assert.ok(goalError.includes('目标/阶段数据未能确认')&&goalError.includes('目标接口模拟故障'),'home states the failure: '+goalError);
    const failedSummary=await page.locator('#goals-summary').textContent();
    assert.ok(!/全部完成|没有进行中的目标|暂无/.test(failedSummary),'unknown is never shown as empty or done: '+failedSummary);
    await page.locator('#task-history-open').click();
    await page.waitForFunction(()=>document.getElementById('records-goals-error').hidden===false,null,{timeout:10000});
    const failedHistory=await page.locator('#records-goals-list').textContent();
    assert.ok(failedHistory.includes('历史目标读取失败')&&!failedHistory.includes('暂无'),
      'history states the failure instead of claiming there is none');
    assert.ok((await page.locator('#records-goals-error').textContent()).includes('目标接口模拟故障'));
    treeFail=false;
    await page.locator('#task-records-refresh').click();
    await page.waitForFunction(()=>document.getElementById('records-goals-count').textContent==='1',null,{timeout:10000});
    await page.locator('#task-records-dialog .close-records-dialog').first().click();
    ok('tree/API failures are stated honestly on home and in the records dialog, then recover');

    for(const failure of ['payload','schema']){
      treeFail=failure;
      await page.waitForFunction(()=>!document.getElementById('goals-error').hidden,null,{timeout:10000});
      await page.locator('#task-history-open').click();
      await page.waitForFunction(()=>!document.getElementById('records-goals-error').hidden,null,{timeout:6000});
      assert.ok(!(await page.locator('#records-goals-list').textContent()).includes('暂无'),'HTTP 200 with error/missing goals is unknown, not empty');
      treeFail=false;
      await page.locator('#task-records-refresh').click();
      await page.waitForFunction(()=>document.getElementById('records-goals-count').textContent==='1',null,{timeout:6000});
      await page.locator('#task-records-dialog .close-records-dialog').first().click();
      await page.waitForFunction(()=>document.getElementById('goals-error').hidden,null,{timeout:10000});
    }
    ok('HTTP 200 payload errors and missing goal lists remain explicitly unknown in both entries');

    // ---- 9) a project without any goal hides the block ------------------------------
    await page.locator('#project-view-switch').click();
    await page.locator('#project-view-known .project-menu-item',{hasText:C}).click();
    await settled(page);
    assert.equal(await page.locator('#goals-panel').isVisible(),false,'a project with zero goals keeps home clean');
    assert.equal(await page.locator('#new-goal').isVisible(),true);
    await page.locator('#task-history-open').click();
    await page.waitForFunction(()=>document.getElementById('records-goals-count').textContent==='0',null,{timeout:10000});
    assert.ok((await page.locator('#records-goals-list').textContent()).includes('暂无已完成 / 已取消的目标'));
    await page.locator('#task-records-dialog .close-records-dialog').first().click();
    ok('empty project: home block hidden, history entry says there is nothing');
    await context.close();

    // ---- 10) 420 / 1280 layout with a long active goal title -------------------------
    goals[A][0].status='active';goals[A][0].status_label='进行中';
    const layouts=[];
    for(const width of [420,1280]){
      const shot=await newPage(width,900);
      await shot.page.goto(`${url}/?project=${encodeURIComponent(A)}`);
      await settled(shot.page);
      assert.equal(await shot.page.locator('#goals-panel').isVisible(),true,`home block visible at ${width}`);
      const collapsed=await shot.page.evaluate(()=>({
        overflow:document.documentElement.scrollWidth<=innerWidth,
        rowInside:document.getElementById('goals-toggle').getBoundingClientRect().right<=
                  document.querySelector('.goals-panel').getBoundingClientRect().right+1,
        toggleHeight:document.getElementById('goals-toggle').getBoundingClientRect().height,
        rowHeight:document.querySelector('.goals-row').getBoundingClientRect().height}));
      assert.ok(collapsed.overflow,`no horizontal overflow at ${width} (collapsed)`);
      assert.ok(collapsed.rowInside,`the progress row stays inside the panel at ${width}`);
      assert.ok(collapsed.toggleHeight<34,`the progress line itself stays on one line at ${width}, got ${collapsed.toggleHeight}px`);
      assert.ok(collapsed.rowHeight<70,`the progress row stays compact at ${width}, got ${collapsed.rowHeight}px`);
      await shot.page.screenshot({path:path.join(outDir,`goals-progress-${width}-home.png`),fullPage:false});
      await shot.page.locator('#goals-toggle').click();
      const summaryBox=await shot.page.locator('#goals-summary').evaluate(el=>({
        ellipsis:el.scrollWidth>el.clientWidth+1||el.getBoundingClientRect().width<el.parentElement.getBoundingClientRect().width,
        overflow:el.scrollWidth<=el.clientWidth+1?true:false}));
      assert.ok(summaryBox.ellipsis,`long titles are truncated, not wrapped over the row, at ${width}`);
      await sleep(200);
      assert.ok(await shot.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),
        `no horizontal overflow at ${width} (expanded tree)`);
      await shot.page.screenshot({path:path.join(outDir,`goals-progress-${width}-home-expanded.png`),fullPage:false});
      await shot.page.locator('#task-history-open').click();
      await shot.page.waitForFunction(()=>document.getElementById('records-goals-count').textContent==='2',null,{timeout:10000});
      await shot.page.locator('#records-goals').evaluate(el=>{el.open=true;});
      await sleep(200);
      const recordsBox=await shot.page.evaluate(()=>({
        overflow:document.documentElement.scrollWidth<=innerWidth,
        wide:Array.from(document.querySelectorAll('#records-goals-list *')).every(el=>el.getBoundingClientRect().right<=
          document.getElementById('records-goals-list').getBoundingClientRect().right+1)}));
      assert.ok(recordsBox.overflow,`no horizontal overflow at ${width} (records dialog)`);
      assert.ok(recordsBox.wide,`history goal content stays inside its column at ${width}`);
      await shot.page.screenshot({path:path.join(outDir,`goals-progress-${width}-records.png`),fullPage:false});
      layouts.push({width,overflow:collapsed.overflow&&recordsBox.overflow,row_height:Math.round(collapsed.rowHeight),
        line_height:Math.round(collapsed.toggleHeight)});
      await shot.context.close();
    }
    ok('420 and 1280 layouts: one compact row, ellipsized long titles, no horizontal overflow');

    assert.deepEqual(mutations,[],'the whole check performs no save/delete/dispatch/recover write');
    assert.deepEqual(errors,[],'no page error while rendering goals, history and failures');
    fs.writeFileSync(path.join(outDir,'goals-progress-report.json'),JSON.stringify({passed:true,checks,layouts,
      requests:[...new Set(requests)].sort(),mutations,previewBodies:previewBodies.length,pageErrors:errors},null,1));
    console.log(JSON.stringify({passed:true,checks,layouts}));
  }catch(error){
    fs.writeFileSync(path.join(outDir,'goals-progress-report.json'),JSON.stringify({passed:false,error:String(error&&error.stack||error),
      checks,requests:[...new Set(requests)].sort(),mutations,pageErrors:errors},null,1));
    throw error;
  }finally{await browser.close();server.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
