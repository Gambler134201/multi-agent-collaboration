'use strict';
// Real headless browser, mocked HTTP only. No running panel or model account.
const {chromium}=require('./browser_helpers.cjs');
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const panel=path.resolve(__dirname,'../panel');
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    for(const host of ['codex','dsh']){
      const page=await browser.newPage(),sent=[],errors=[],unexpected=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('**/*',async route=>{
        const req=route.request(),url=new URL(req.url());
        const json=data=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
        if(url.hostname!=='task-policy.test'){unexpected.push(url.href);return route.abort();}
        if(url.pathname==='/api/session')return json({token:'fixture'});
        if(url.pathname==='/api/state')return json({host,models:[{id:'home',name:'Fixture',provider:'local',model:'fixture',enabled:true}],
          default_model:'home',tasks:[],workflow:{enabled:true,model_scope:'all',execution_agent:'pi'},
          execution_agents:{pi:{name:'Pi Agent',ready:true}},view:{mode:'project',project:'C:\\fixture-project'},
          agent:{ready:true},server_time:new Date().toISOString()});
        if(url.pathname==='/api/tasks'&&req.method()==='POST'){
          sent.push(req.postDataJSON());return json({...sent.at(-1),id:'fixture-'+sent.length,status:'queued'});
        }
        if(url.pathname.startsWith('/api/'))return json({projects:[],goals:[],stats:{}});
        const file=url.pathname==='/'?'index.html':url.pathname.slice(1);
        if(!/^[A-Za-z0-9_.-]+$/.test(file))return route.abort();
        const target=path.join(panel,file);
        if(!fs.existsSync(target))return route.fulfill({status:404,body:''});
        return route.fulfill({status:200,body:fs.readFileSync(target),
          contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html'});
      });
      await page.goto('http://task-policy.test/?host='+host+'&project=C%3A%5Cfixture-project');
      await page.locator('#new-task').click();
      await page.locator('#task-kind').selectOption('edit');
      await page.locator('#task-prompt').fill('Create the requested result and test it.');
      assert.equal(await page.locator('#task-files').evaluate(el=>el.required),false);
      await page.locator('#task-form button[type=submit]').click();
      await page.waitForFunction(()=>!document.querySelector('#task-dialog').open);
      assert.equal(sent.length,1);assert.deepEqual(sent[0].files,[]);
      assert.equal(Object.hasOwn(sent[0],'write_scope'),false);
      await page.locator('#new-task').click();
      await page.locator('#task-kind').selectOption('edit');
      await page.locator('#task-write-scope').selectOption('declared_files');
      assert.equal(await page.locator('#task-files').evaluate(el=>el.required),true);
      await page.locator('#task-files').fill('main.js');
      await page.locator('#task-prompt').fill('Only change main.js.');
      await page.locator('#task-form button[type=submit]').click();
      await page.waitForFunction(()=>!document.querySelector('#task-dialog').open);
      assert.equal(sent.length,2);assert.equal(sent[1].write_scope,'declared_files');
      assert.deepEqual(sent[1].files,['main.js']);
      assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
      await page.close();
      process.stdout.write(host+': optional deliverables and explicit file restriction passed\n');
    }
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
