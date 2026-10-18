// Task history management UI: real panel frontend against isolated HTTP fixtures.
// The live workbench (127.0.0.1:8765) is never contacted; no real record, config or project file is touched.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('./browser_helpers.cjs');
const web=path.resolve(__dirname,'../panel');
const A='C:\\fixture\\historyA',B='C:\\fixture\\historyB';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const mkRecord=(id,status,deletable,blocked_reason='')=>({id,title:`记录 ${id}`,status,created_at:'2026-10-12 09:00:00',ended_at:'2026-10-12 09:20:00',model_name:'FIXTURE 本地',goal_id:'g1',group_title:'组甲',deletable,blocked_reason});
function seedRecords(){
  const list=[mkRecord('t-done','completed',true),mkRecord('t-failed','failed',true),mkRecord('t-cancel','cancelled',true),
    mkRecord('t-running','running',false,'任务正在执行，不能删除'),mkRecord('t-protected','completed',false,'阶段已通过独立验收'),
    {...mkRecord('t-xss','completed',true),title:'<img src=x onerror=alert(1)>恶意标题'},mkRecord('t-linked','completed',true)];
  for(let i=1;i<=18;i++)list.push(mkRecord(`t-fill${i}`,'completed',true));
  return list;
}
async function main(){
  const records={[A]:seedRecords(),[B]:[mkRecord('t-b','completed',true)]};
  const tasks={[A]:[{id:'t-done',title:'记录 t-done',kind:'chat',engine:'dsh',model_id:'home',model_name:'FIXTURE 本地',project:A,status:'completed',phase:'回复完成',created_at:new Date().toISOString()},
                 {id:'t-failed',title:'记录 t-failed',kind:'chat',engine:'dsh',model_id:'home',model_name:'FIXTURE 本地',project:A,status:'failed',phase:'执行失败',created_at:new Date().toISOString()},
                 {id:'t-linked',title:'记录 t-linked',kind:'chat',engine:'dsh',model_id:'home',model_name:'FIXTURE 本地',project:A,status:'completed',phase:'回复完成',created_at:new Date().toISOString()},
                 {id:'t-protected',title:'记录 t-protected',kind:'chat',engine:'dsh',model_id:'home',model_name:'FIXTURE 本地',project:A,status:'completed',phase:'回复完成',created_at:new Date().toISOString()}],
               [B]:[{id:'t-b',title:'记录 t-b',kind:'chat',engine:'dsh',model_id:'home',model_name:'FIXTURE 本地',project:B,status:'completed',phase:'回复完成',created_at:new Date().toISOString()}]};
  const base={models:[{id:'home',name:'FIXTURE 本地',provider:'local',api_base:'http://127.0.0.1:9/v1',model:'fixture-model',enabled:true,bypass_proxy:true,max_input_tokens:32768,max_output_tokens:4096,last_check:{ok:true,latency_ms:120}}],
    default_model:'home',workspace:'C:\\fixture',agent:{ready:true,edit_sandbox_mode:'danger-full-access',chat_sandbox_mode:'read-only'},
    workflow:{enabled:true,paid_allowed:true,enforce_paid_budget:false,enforce_model_call_limit:false,task_budget_cny:5,daily_budget_cny:20,max_model_calls:30,daily:{settled_cny:0,has_actual:false,unsettled_cny:0,unknown_calls:0}},
    codex_usage:{status:'ready',buckets:[]},delegation:{delegated:0,executor_completed:0,accepted:0,returned:0},
    stages:{stats:{goals:0,stages:0,accepted:0,recovery_pending:0,by_status:{}},recovery:[]},server_time:new Date().toISOString()};
  const mutations=[],errors=[],recordsCalls=[],previewBodies=[],deleteBodies=[],violations=[],compactCalls=[];
  const tokens=new Map();let tokenSeq=0,slowPreviewMs=0,busyProject=null;
  const server=http.createServer(async(req,res)=>{
    const pathname=new URL(req.url,'http://local').pathname;
    const json=(v,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(v));};
    if(!pathname.startsWith('/api/')){const file={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[pathname];if(!file)return json({},404);res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});return res.end(fs.readFileSync(path.join(web,file)));}
    let view;try{view=JSON.parse(Buffer.from(req.headers['x-model-desk-view']||'','base64url').toString('utf8'));}catch{return json({error:'missing explicit view'},400);}
    if(req.method!=='GET'&&pathname!=='/api/projects/remember')mutations.push(pathname);
    const body=await new Promise(resolve=>{let raw='';req.on('data',chunk=>raw+=chunk);req.on('end',()=>{try{resolve(JSON.parse(raw||'{}'));}catch{resolve({});}});});
    if(pathname==='/api/session')return json({token:'fixture'});
    if(pathname==='/api/state'){const selected=view.mode==='all'?Object.values(tasks).flat():view.mode==='project'?tasks[view.project]||[]:[];
      return json({...base,tasks:selected,view,server_time:new Date().toISOString()});}
    if(pathname==='/api/tree')return json({goals:[],stats:{goals:0,stages:0,accepted:0,recovery_pending:0,by_status:{}},recovery:[],view});
    if(pathname==='/api/projects')return json({projects:[A,B].map(p=>({path:p,label:p})),recent_projects:[{path:A}]});
    if(pathname==='/api/db/space'){
      if(view.mode==='project')return json({scope:'project',project:view.project,databases:[{path:view.project+'\\.model-desk\\records.db',project:view.project,scope:'project',initialized:true,exists:true,writable:view.project!==B,linked:false,reason:view.project===B?'fixture 只读目录':'',size_bytes:40960,free_bytes:8192}]});
      return json({scope:view.mode,project:null,databases:[{path:'E:\\fixture-workbench\\stages.db',scope:'legacy',initialized:true,exists:true,size_bytes:65536}]});
    }
    if(pathname==='/api/db/compact'&&req.method==='POST'){
      compactCalls.push(view.mode==='project'?view.project:view.mode);
      if(view.mode!=='project')return json({error:'未绑定不会静默压缩所有数据库'},400);
      if(view.project===B)return json({error:'项目记录库不可写，拒绝压缩'},400);
      if(busyProject===view.project)return json({error:'该项目仍有未结束任务，拒绝压缩：t-running'},400);
      return json({scope:'project',project:view.project,compacted:[{path:view.project+'\\.model-desk\\records.db',before:40960,after:36864,reclaimed_bytes:4096}],reclaimed_bytes:4096});
    }
    if(pathname.endsWith('/observability'))return json({task_id:pathname.split('/')[3],cost:{known:false},view});
    if(pathname==='/api/task-records'){
      recordsCalls.push(view.mode);
      if(view.mode!=='project')return json({error:'fixture: 只允许 project 视图'},400);
      const list=records[view.project]||[];
      return json({project:view.project,tasks:list,total:list.length});
    }
    if(pathname==='/api/task-records/preview'){
      if(view.mode!=='project')return json({error:'fixture: 只允许 project 视图'},400);
      const store=records[view.project]||[];
      const ids=(Array.isArray(body.task_ids)?body.task_ids:[]).map(String);
      previewBodies.push({project:view.project,ids});
      const unsafe=ids.filter(id=>{const row=store.find(item=>item.id===id);return !row||row.deletable!==true;});
      if(unsafe.length){violations.push({where:'preview',ids:unsafe});return json({error:'fixture: 受保护记录不能进入预览'},403);}
      if(slowPreviewMs)await sleep(slowPreviewMs);
      const linked=store.find(item=>item.id==='t-linked');
      const chosen=ids.map(id=>store.find(item=>item.id===id)).filter(Boolean);
      const extra=linked&&!ids.includes('t-linked')?['t-linked']:[];
      const finalTasks=[...chosen,...(extra.map(id=>store.find(item=>item.id===id)).filter(Boolean))].map(({id,title,status})=>({id,title,status}));
      const token='tok-'+(++tokenSeq);
      tokens.set(token,{project:view.project,ids:[...ids,...extra],used:false});
      return json({project:view.project,token,expires_in:300,tasks:finalTasks,requested_ids:ids,added_ids:extra,
        goals:[{id:'g1',title:'目标 <b>甲</b>'}],stage_count:2,
        warnings:['永久删除提示词、结果、日志、验收与交接','不删除项目文件，已发生费用总账保留']});
    }
    if(pathname==='/api/task-records/delete'){
      if(view.mode!=='project')return json({error:'fixture: 只允许 project 视图'},400);
      deleteBodies.push({project:view.project,token:body.token});
      const entry=tokens.get(body.token);
      if(!entry){violations.push({where:'delete',reason:'unknown token'});return json({error:'fixture: 未知确认令牌'},403);}
      if(entry.used){violations.push({where:'delete',reason:'replayed token'});return json({error:'fixture: 确认令牌已使用'},409);}
      if(entry.project!==view.project){violations.push({where:'delete',reason:'project mismatch'});return json({error:'fixture: 令牌项目不一致'},403);}
      entry.used=true;
      const gone=new Set(entry.ids);
      records[view.project]=(records[view.project]||[]).filter(item=>!gone.has(item.id));
      tasks[view.project]=(tasks[view.project]||[]).filter(item=>!gone.has(item.id));
      return json({deleted_tasks:gone.size,deleted_goals:1,deleted_stages:2});
    }
    return json({error:'fixture denied'},403);
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const url=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const checks=[];
  const ok=name=>checks.push(name);
  try{
    const context=await browser.newContext({viewport:{width:1280,height:820}});context.setDefaultTimeout(12000);
    const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`${url}/`);await page.locator('.model-card').first().waitFor();

    // 1) unbound / explicit all: the entry guides the user to choose a concrete project instead of opening the dialog.
    await page.locator('#task-history-open').click();
    assert.equal(await page.evaluate(()=>$('task-records-dialog').open),false);
    assert.match(await page.locator('#toast').textContent(),/需要先选择具体项目/);
    assert.deepEqual(recordsCalls,[],'unbound must never fetch project records');
    const allPage=await context.newPage();allPage.on('pageerror',e=>errors.push(e.message));
    await allPage.goto(`${url}/?view=all`);await allPage.locator('.task-row').first().waitFor();
    await allPage.locator('#task-history-open').click();
    assert.equal(await allPage.evaluate(()=>$('task-records-dialog').open),false);
    assert.match(await allPage.locator('#toast').textContent(),/只按具体项目操作/);
    assert.deepEqual(recordsCalls,[],'the explicit all view must never fetch project records');
    await allPage.close();
    ok('unbound + explicit all guide to choose a project and never call /api/task-records');

    // 2) bound project: list, filters, keyword, locked rows, escaping.
    const pageA=await context.newPage();pageA.on('pageerror',e=>errors.push(e.message));
    await pageA.goto(`${url}/?project=${encodeURIComponent(A)}`);await pageA.locator('.model-card').first().waitFor();
    assert.match(await pageA.locator('#project-view-status').textContent(),/当前项目/);
    await pageA.locator('#task-history-open').click();
    await pageA.locator('#task-records-dialog').waitFor({state:'visible'});
    await pageA.locator('#task-records-list .record-row').first().waitFor();
    assert.equal(await pageA.locator('#task-records-project').textContent(),A);
    assert.equal(await pageA.locator('#task-records-list .record-row').count(),25);
    assert.equal((await pageA.locator('#task-records-count').textContent()).includes('可删除 23'),true);
    assert.equal(await pageA.locator('#task-records-preview').isDisabled(),true,'preview needs a selection');
    await pageA.locator('[data-record-status="failed"]').click();
    assert.equal(await pageA.locator('#task-records-list .record-row').count(),1);
    await pageA.locator('[data-record-status="all"]').click();
    await pageA.locator('#task-records-search').fill('t-fill1');
    assert.equal(await pageA.locator('#task-records-list .record-row').count(),10);
    await pageA.locator('#task-records-search').fill('');
    assert.equal(await pageA.locator('#task-records-list .record-row').count(),25);
    ok('project-bound list with status filter + keyword filter and deletable count');

    // running / protected records cannot be selected, and the reason is visible.
    assert.equal(await pageA.locator('input[data-record-id="t-running"]').isDisabled(),true);
    assert.equal(await pageA.locator('input[data-record-id="t-protected"]').isDisabled(),true);
    assert.match(await pageA.locator('.record-row:has(input[data-record-id="t-running"]) .record-blocked').textContent(),/任务正在执行/);
    await pageA.locator('input[data-record-id="t-protected"]').click({force:true});
    assert.equal(await pageA.evaluate(()=>historySelected.size),0,'DOM click must not select a protected record');
    // dynamic titles are escaped, never injected as HTML
    assert.equal(await pageA.locator('#task-records-list img').count(),0);
    assert.equal(await pageA.locator('input[data-record-id="t-xss"]').evaluate(el=>el.closest('.record-row').textContent.includes('<img src=x onerror=alert(1)>')),true);
    ok('running/protected rows disabled with reason; backend titles escaped');

    // select-all only covers deletable rows of the current filter
    await pageA.locator('#task-records-select-all').check();
    assert.equal(await pageA.evaluate(()=>historySelected.size),23);
    assert.equal(await pageA.evaluate(()=>[...historySelected].some(id=>id==='t-running'||id==='t-protected')),false);
    await pageA.locator('#task-records-select-all').uncheck();
    assert.equal(await pageA.evaluate(()=>historySelected.size),0);
    ok('select-all selects only deletable records');

    // 3) preview needs a second human confirmation; cancelling deletes nothing and clears the token
    await pageA.locator('input[data-record-id="t-done"]').check();
    assert.deepEqual(await pageA.evaluate(()=>[...historySelected]),['t-done']);
    await pageA.locator('#task-records-preview').click();
    await pageA.locator('#task-records-confirm-dialog').waitFor({state:'visible'});
    assert.deepEqual(previewBodies,[{project:A,ids:['t-done']}]);
    assert.equal(await pageA.locator('#task-records-confirm-list li').count(),2);
    assert.match(await pageA.locator('#task-records-confirm-list').textContent(),/联动加入/);
    assert.match(await pageA.locator('#task-records-confirm-summary').textContent(),/最终删除任务2我勾选的1额外联动任务1目标 \/ 阶段1 \/ 2/);
    assert.match(await pageA.locator('#task-records-confirm-warnings').textContent(),/永久删除提示词、结果、日志、验收与交接/);
    assert.match(await pageA.locator('.records-caution').textContent(),/不删项目文件，已发生费用总账保留/);
    assert.equal(await pageA.locator('#task-records-confirm-goals img').count(),0);
    assert.equal(await pageA.evaluate(()=>!!historyToken),true);
    await pageA.locator('#task-records-cancel').click();
    await pageA.locator('#task-records-confirm-dialog').waitFor({state:'hidden'});
    assert.equal(await pageA.evaluate(()=>historyToken),null,'cancel must clear the preview token');
    assert.equal(deleteBodies.length,0,'cancel must not send delete');
    assert.equal(await pageA.locator('#task-records-preview').isDisabled(),false,'selection survives cancel');
    ok('preview shows final list/linkage/goals/stages/warnings; cancel clears token and deletes nothing');

    // 4) narrow viewport: long list scrolls, footer buttons stay visible (dark and light)
    for(const theme of ['light','dark']){
      await pageA.locator('#theme-mode').selectOption(theme);
      await pageA.setViewportSize({width:380,height:560});await pageA.waitForTimeout(200);
      const box=await pageA.locator('#task-records-preview').boundingBox();
      assert.ok(box&&box.y+box.height<=560,`footer must stay visible at 380/${theme}`);
      const scroll=await pageA.evaluate(()=>{const el=document.querySelector('#task-records-dialog .records-scroll');return{h:el.scrollHeight,c:el.clientHeight};});
      assert.ok(scroll.h>scroll.c,'long record list must scroll');
      for(const width of [1280,700,420]){await pageA.setViewportSize({width,height:900});await pageA.waitForTimeout(120);
        assert.equal(await pageA.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`horizontal overflow at ${width}/${theme}`);}
      await pageA.setViewportSize({width:380,height:560});await pageA.waitForTimeout(150);
      const rowColor=await pageA.evaluate(()=>getComputedStyle(document.querySelector('#task-records-list .record-row')).backgroundColor);
      (pageA.__colors??={})[theme]=rowColor;
    }
    assert.notEqual(pageA.__colors.light,pageA.__colors.dark);
    await pageA.locator('#theme-mode').selectOption('system');
    await pageA.setViewportSize({width:1280,height:820});await pageA.waitForTimeout(150);
    ok('long list scrolls with always-visible footer, light/dark consistent, no horizontal overflow');

    // 5) refresh button re-reads the list and invalidates any pending confirmation
    const before=recordsCalls.length;
    await pageA.locator('#task-records-refresh').click();
    await pageA.waitForFunction(n=>document.querySelectorAll('#task-records-list .record-row').length===n,25);
    assert.ok(recordsCalls.length>before);
    ok('refresh list re-reads records for the same bound project');

    // 6) delete only after the second confirmation; double click sends exactly one delete
    await pageA.locator('input[data-record-id="t-done"]').check();
    await pageA.locator('#task-records-preview').click();
    await pageA.locator('#task-records-confirm-dialog').waitFor({state:'visible'});
    await pageA.locator('#task-records-delete').evaluate(el=>{el.click();el.click();el.click();});
    await pageA.waitForFunction(()=>!document.getElementById('task-records-confirm-dialog').open);
    assert.equal(deleteBodies.length,1,'double click must send one delete');
    assert.deepEqual(deleteBodies[0],{project:A,token:'tok-2'});
    await pageA.waitForFunction(()=>document.querySelectorAll('#task-records-list .record-row').length===23);
    assert.equal(await pageA.locator('input[data-record-id="t-done"]').count(),0);
    assert.equal(await pageA.locator('input[data-record-id="t-linked"]').count(),0,'linked record removed from the list too');
    ok('delete sent once after second confirmation; list refreshed without page reload');

    // 7) deleted records leave no stale detail cache
    await pageA.locator('#task-records-dialog .close-records-dialog').first().click();
    await pageA.locator('[data-task-id="t-failed"]').click();
    await pageA.evaluate(()=>{state.observability['t-failed']={cost:{known:false}};detailPrompt='OLD PROMPT';promptTask='t-failed';});
    assert.match(await pageA.locator('#task-detail').textContent(),/记录 t-failed/);
    await pageA.locator('#task-history-open').click();
    await pageA.locator('#task-records-list .record-row').first().waitFor();
    await pageA.locator('input[data-record-id="t-failed"]').check();
    await pageA.locator('#task-records-preview').click();
    await pageA.locator('#task-records-confirm-dialog').waitFor({state:'visible'});
    await pageA.locator('#task-records-delete').click();
    await pageA.waitForFunction(()=>!document.getElementById('task-records-confirm-dialog').open);
    await pageA.waitForFunction(n=>document.querySelectorAll('#task-records-list .record-row').length===n,22);
    assert.equal(await pageA.evaluate(()=>state.observability['t-failed']),undefined,'deleted task observability cache cleared');
    assert.notEqual(await pageA.evaluate(()=>selectedTask),'t-failed','deleted selected task cleared');
    assert.equal(await pageA.evaluate(()=>promptTask),null);
    assert.equal(await pageA.locator('[data-task-id="t-failed"]').count(),0);
    assert.equal(await pageA.locator('#task-detail').textContent().then(t=>t.includes('记录 t-failed')),false);
    ok('successful delete clears the deleted task detail/observability caches before refresh');

    // 7b) auxiliary storage facts + idle compaction boundaries (compaction never deletes records)
    await pageA.locator('#records-storage').evaluate(el=>{el.open=true;});
    await pageA.waitForFunction(()=>/记录库就绪/.test(document.querySelector('#records-storage-state').textContent));
    await pageA.waitForFunction(()=>/旧全局库/.test(document.querySelector('#records-storage-legacy').textContent));
    await pageA.locator('#task-records-compact').click();
    await pageA.waitForFunction(()=>/回收/.test(document.querySelector('#task-records-compact-result').textContent));
    assert.match(await pageA.locator('#task-records-compact-result').textContent(),/记录未变化/);
    await pageA.waitForFunction(()=>!document.querySelector('#task-records-compact').disabled);
    busyProject=A;
    await pageA.locator('#task-records-compact').click();
    await pageA.waitForFunction(()=>/压缩未执行/.test(document.querySelector('#task-records-compact-result').textContent));
    assert.match(await pageA.locator('#task-records-compact-result').textContent(),/未结束任务/);
    await pageA.waitForFunction(()=>!document.querySelector('#task-records-compact').disabled);
    busyProject=null;
    assert.deepEqual(compactCalls,[A,A],'compaction is always scoped to the bound project');
    assert.match(await pageA.locator('.records-storage-body').textContent(),/不删除任何记录/);
    ok('auxiliary storage facts and idle compaction boundaries; compaction never deletes records');

    // 8) late preview after a project switch must never open or apply the old confirmation
    const pageB=await context.newPage();pageB.on('pageerror',e=>errors.push(e.message));
    await pageB.goto(`${url}/?project=${encodeURIComponent(A)}`);await pageB.locator('.model-card').first().waitFor();
    await pageB.locator('#task-history-open').click();
    await pageB.locator('#task-records-list .record-row').first().waitFor();
    await pageB.locator('input[data-record-id="t-cancel"]').check();
    slowPreviewMs=900;
    await pageB.locator('#task-records-preview').click();
    // The management dialog is modal, so drive the (inert) project switch programmatically.
    await pageB.evaluate(proj=>{document.querySelector('#project-view-switch').click();const node=[...document.querySelectorAll('#project-view-known [data-project]')].find(n=>n.dataset.project===proj);if(!node)throw new Error('menu item missing: '+proj);node.click();},B);
    await pageB.locator('[data-task-id="t-b"]').waitFor();
    await sleep(1400);slowPreviewMs=0;
    assert.equal(await pageB.evaluate(()=>$('task-records-confirm-dialog').open),false,'late preview must not open');
    assert.equal(await pageB.evaluate(()=>$('task-records-dialog').open),false,'project switch closes the management dialog');
    assert.equal(await pageB.evaluate(()=>historyToken),null,'project switch clears the token');
    assert.equal(deleteBodies.length,2,'no delete from the abandoned confirmation');
    await pageB.locator('#task-history-open').click();
    await pageB.locator('#task-records-list .record-row').first().waitFor();
    assert.equal(await pageB.locator('#task-records-project').textContent(),B,'records now belong to the newly chosen project');
    ok('project switch / late response never reuses the old confirmation');

    assert.deepEqual([...new Set(mutations)].sort(),['/api/db/compact','/api/task-records/delete','/api/task-records/preview']);
    assert.deepEqual(violations,[],'frontend never sent protected records, replayed or cross-project tokens');
    assert.deepEqual(errors,[],'no page errors');
    const report={passed:true,checks,layouts:['1280x820','380x560','1280/700/420 light+dark'],recordsCalls:[...new Set(recordsCalls)],
      previewBodies,deleteBodies,mutations:[...new Set(mutations)],violations,errors,paid_generation:false,live_panel_contact:false};
    fs.writeFileSync(path.join(__dirname,'task-history-ui-report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
    await browser.close();
  }catch(error){await browser.close();console.error(JSON.stringify({checks,previewBodies,deleteBodies,violations,errors}));throw error;}
  finally{server.closeAllConnections();await new Promise(r=>server.close(r));}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
