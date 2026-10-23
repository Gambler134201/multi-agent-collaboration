'use strict';
// Offline browser checks: real shared component, fixture map, no panel/provider.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('./browser_helpers.cjs');
const panel=path.join(__dirname,'../panel');
const sources=['panel_server.py','brain_gateway.py','dispatch_task.py','execution_engines.py','review_task.py','model_router.py','workbuddy_worker.py','execution_result.py'];
const fixture={project:'E:/fixture',nodes:sources.map(name=>({id:'.local-worker/'+name,path:'.local-worker/'+name,symbols:[],tests:[]})),edges:[]};

async function mount(page,payload,theme='dark'){
  await page.setContent('<!doctype html><html data-theme="'+theme+'"><head><meta charset="utf-8"></head><body><button id="opener">项目地图</button></body></html>');
  for(const name of ['style.css','project-map.css'])await page.addStyleTag({path:path.join(panel,name)});
  await page.addScriptTag({path:path.join(panel,'project-map.js')});
  await page.evaluate(async data=>{
    window.mapCalls=0;window.view={mode:'project',project:data.project};
    window.mapInstance=ModelDeskProjectMap.create({api:async()=>{window.mapCalls++;return data;},getView:()=>window.view});
    document.querySelector('#opener').focus();await window.mapInstance.open();
  },payload);
}

test('interactive architecture is readable, keyboard accessible and project-bound',async t=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    for(const width of [320,750,1280])for(const theme of ['dark','light'])await t.test(`${width}px ${theme}`,async()=>{
      const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('**/*',route=>route.abort());
      try{
        await mount(page,fixture,theme);
        await page.waitForFunction(()=>document.querySelectorAll('.pm-arch-edge').length>=4);
        const review=page.locator('[data-pm-step="review"]');
        await review.focus();await review.press('Space');
        assert.equal(await review.getAttribute('aria-pressed'),'true');
        assert.equal(await page.evaluate(()=>document.activeElement.dataset.pmStep),'review');
        assert.match(await page.locator('#pm-arch-detail').innerText(),/退回整改/);
        assert.match(await page.locator('#pm-arch-detail').innerText(),/主脑检查真实交付/);
        assert.equal(await page.evaluate(()=>window.mapCalls),1);
        const layout=await page.evaluate(()=>{
          const elements=[document.querySelector('.pm-dialog'),document.querySelector('#pm-flow'),...document.querySelectorAll('.pm-flow-node')];
          return elements.map(element=>({width:element.clientWidth,scroll:element.scrollWidth,left:element.getBoundingClientRect().left,right:element.getBoundingClientRect().right}));
        });
        for(const box of layout){assert.ok(box.scroll<=box.width+1,JSON.stringify(box));assert.ok(box.left>=0&&box.right<=width+1,JSON.stringify(box));}
        assert.equal(await page.locator('#pm-arch-lines').evaluate(svg=>[...svg.querySelectorAll('path[d]')].every(p=>!/(NaN|undefined)/.test(p.getAttribute('d')))),true);
        if(process.env.MODEL_DESK_UI_ARTIFACTS){
          fs.mkdirSync(process.env.MODEL_DESK_UI_ARTIFACTS,{recursive:true});
          await page.locator('.pm-dialog').screenshot({path:path.join(process.env.MODEL_DESK_UI_ARTIFACTS,`architecture-${width}-${theme}.png`)});
        }
        await page.locator('#pm-arch-detail [data-pm-id]').first().click();
        assert.equal(await page.locator('#pm-code').isVisible(),true);
        assert.equal(await page.evaluate(()=>document.activeElement.dataset.pmId),'.local-worker/review_task.py');
        await page.evaluate(()=>{window.view={mode:'project',project:'E:/other'};window.mapInstance.onProjectChange();});
        assert.equal(await page.locator('.pm-dialog').evaluate(d=>d.open),false);
        assert.equal(await page.locator('#pm-overview').innerHTML(),'');
        assert.deepEqual(errors,[]);
      }finally{await page.close();}
    });
    await t.test('generic projects do not inherit this project workflow or render hostile paths',async()=>{
      const page=await browser.newPage({viewport:{width:750,height:1000}});
      try{
        const payload={project:'E:/fixture',nodes:[{id:'ui/<img onerror="x">.js',path:'ui/<img onerror="x">.js',symbols:[]},{id:'api/server.py',path:'api/server.py',symbols:[]}],edges:[]};
        await mount(page,payload);
        assert.equal(await page.locator('.pm-flow-explained').count(),0);
        assert.equal(await page.locator('.pm-arch-edge').count(),0);
        assert.equal(await page.locator('#pm-overview img').count(),0);
        assert.match(await page.locator('.pm-arch-basis').innerText(),/暂未自动理解业务逻辑/);
        await page.locator('[data-pm-step]').last().click();
        assert.match(await page.locator('#pm-arch-detail').innerText(),/api\/server.py/);
      }finally{await page.close();}
    });
  }finally{await browser.close();}
});
