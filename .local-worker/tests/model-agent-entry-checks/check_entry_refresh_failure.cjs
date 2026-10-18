// Independent boundary check: a confirmed save remains usable even when the
// following state refresh fails. All HTTP is mocked; no production mutations.
const {chromium}=require('../browser_helpers.cjs');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'}),page=await browser.newPage();
  const panel=path.resolve(__dirname,'../../panel'),project='C:\\entry-refresh-fixture';
  const model={id:'home',name:'Qwen',model:'fixture',provider:'local',enabled:true,
    api_base:'http://fixture.invalid/v1',default_agent:'dsh',default_agent_valid:true};
  let failState=false,saves=0;
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),p=url.pathname;
    assert.equal(url.hostname,'entry-refresh.test');
    const json=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(p==='/api/session')return json({token:'fixture'});
    if(p==='/api/state')return failState?json({error:'状态刷新暂时不可用'},503):json({
      workflow:{enabled:true,execution_agent:'pi',model_scope:'all'},models:[model],default_model:'home',tasks:[],
      execution_agents:{dsh:{name:'DeepSeek Harness',ready:true},pi:{name:'Pi Agent',ready:true}},
      view:{mode:'project',project},server_time:new Date().toISOString()});
    if(p==='/api/models/home/save'){
      assert.deepEqual(request.postDataJSON(),{default_agent:'pi'});
      saves++;model.default_agent='pi';failState=true;return json(model);
    }
    if(p==='/api/projects')return json({projects:[]});
    if(p==='/api/tree')return json({goals:[],stats:{}});
    if(p==='/api/db/space')return json({initialized:false,databases:[]});
    if(p==='/api/projects/remember')return json({ok:true});
    const asset={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[p];
    if(asset)return route.fulfill({status:200,contentType:p.endsWith('.js')?'text/javascript':p.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(panel,asset))});
    throw new Error('Unexpected request '+p);
  });
  try{
    await page.goto('http://entry-refresh.test/?project='+encodeURIComponent(project));
    await page.locator('#agent-entry-agent').selectOption('pi');
    await page.locator('#agent-entry-save').click();
    await page.locator('#last-sync').filter({hasText:'状态刷新暂时不可用'}).waitFor();
    assert.equal(saves,1);
    assert.equal(await page.locator('#agent-entry-save').isDisabled(),false,'confirmed save must unlock controls when refresh fails');
    assert.match(await page.locator('#agent-entry-status').textContent(),/已保存/);
    assert.equal(await page.locator('#agent-entry-agent').inputValue(),'pi');
    assert.match(await page.locator('#agent-state').textContent(),/Pi Agent/);
    assert.match(await page.locator('.model-card .model-agent').textContent(),/Pi Agent/);
    failState=false;await page.reload();
    await page.locator('#agent-state').filter({hasText:'Pi Agent'}).waitFor();
    assert.equal(await page.locator('#agent-entry-agent').inputValue(),'pi');
    assert.equal(saves,1);
    console.log(JSON.stringify({ok:true,confirmed_save_survives_refresh_failure:true,controls_unlocked:true,reload_retained:true,production_mutations:0}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
