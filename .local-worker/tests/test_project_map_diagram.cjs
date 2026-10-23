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
const snapshot='a'.repeat(64);
function explanationPayload(state='matched'){
  return {project:'E:/fixture',index:{hash:snapshot},summary:{files:2,edges:0,unresolved:0},limits:{},unresolved:[],edges:[],nodes:[
    {id:'.local-worker/panel_server.py',path:'.local-worker/panel_server.py',sha256:'b'.repeat(64),symbols:[],tests:[]},
    {id:'.local-worker/review_task.py',path:'.local-worker/review_task.py',sha256:'c'.repeat(64),symbols:[],tests:[]}
  ],architecture:{
    source:'project-architecture.json',provenance:'project_authored',state,title:'订单交付项目',summary:'把订单需求转成可验收的交付结果。',snapshot_hash:state==='stale'?'d'.repeat(64):snapshot,
    nodes:[
      {id:'result',title:'需求受理',role:'接收并澄清订单',description:'整理用户目标、范围与验收条件。',output:'需求清单',state:'matched',evidence:[
        {path:'.local-worker/panel_server.py',sha256:'b'.repeat(64),state:'unchanged'},
        {path:'docs/missing.md',sha256:'e'.repeat(64),state:'unindexed'}
      ]},
      {id:'delivery',title:'交付验收',role:'独立检查交付',description:'核对交付内容，决定通过或退回。',output:'验收结论',state:state==='unknown'?'unknown':'matched',evidence:[
        {path:'.local-worker/review_task.py',sha256:'c'.repeat(64),state:'unchanged'}
      ]}
    ],
    edges:[
      {source:'result',target:'delivery',label:'提交需求',confidence:'documented'},
      {source:'delivery',target:'result',label:'退回修改',confidence:'inferred'}
    ]
  }};
}

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
    for(const width of [320,750,1280])await t.test(`project explanation at ${width}px`,async()=>{
      const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('**/*',route=>route.abort());
      try{
        await mount(page,explanationPayload());
        await page.waitForFunction(()=>document.querySelectorAll('.pm-arch-node').length===2);
        await page.waitForFunction(()=>document.querySelectorAll('.pm-arch-edge').length===2);
        assert.equal(await page.locator('.pm-flow-explained').count(),0);
        assert.equal(await page.locator('.pm-step-result').count(),0);
        assert.deepEqual(await page.locator('.pm-arch-node').evaluateAll(nodes=>nodes.map(node=>node.dataset.pmArch)),['pa-0','pa-1']);
        assert.match(await page.locator('.pm-intro').innerText(),/订单交付项目/);
        assert.match(await page.locator('.pm-intro').innerText(),/把订单需求转成可验收的交付结果/);
        assert.match(await page.locator('.pm-explain-banner').innerText(),/依据哈希相符/);
        assert.match(await page.locator('#pm-arch-detail').innerText(),/需求受理/);
        assert.match(await page.locator('#pm-arch-detail').innerText(),/接收并澄清订单/);
        assert.match(await page.locator('#pm-arch-detail').innerText(),/需求清单/);
        assert.match(await page.locator('#pm-arch-detail').innerText(),/哈希相符/);
        assert.equal(await page.locator('#pm-arch-detail .pm-evidence-missing').count(),1);
        assert.match(await page.locator('#pm-arch-detail .pm-evidence-missing').innerText(),/docs\/missing\.md/);
        assert.match(await page.locator('#pm-arch-detail .pm-evidence-missing').innerText(),/未收录需核对/);
        assert.equal(await page.locator('#pm-arch-detail .pm-evidence-missing [data-pm-id]').count(),0);
        const layout=await page.evaluate(()=>[document.querySelector('.pm-dialog'),document.querySelector('#pm-flow'),...document.querySelectorAll('.pm-flow-node'),document.querySelector('.pm-explain-banner'),document.querySelector('#pm-arch-detail')].filter(Boolean).map(element=>({width:element.clientWidth,scroll:element.scrollWidth,left:element.getBoundingClientRect().left,right:element.getBoundingClientRect().right})));
        for(const box of layout){assert.ok(box.scroll<=box.width+1,JSON.stringify(box));assert.ok(box.left>=0&&box.right<=width+1,JSON.stringify(box));}
        assert.equal(await page.locator('#pm-arch-lines').evaluate(svg=>[...svg.querySelectorAll('path[d]')].every(path=>!/(NaN|undefined)/.test(path.getAttribute('d')))),true);
        if(process.env.MODEL_DESK_UI_ARTIFACTS){
          fs.mkdirSync(process.env.MODEL_DESK_UI_ARTIFACTS,{recursive:true});
          await page.locator('.pm-dialog').screenshot({path:path.join(process.env.MODEL_DESK_UI_ARTIFACTS,`explanation-${width}.png`)});
        }
        const second=page.locator('[data-pm-arch="pa-1"]');await second.focus();await second.press('Space');
        assert.equal(await second.getAttribute('aria-pressed'),'true');
        assert.equal(await page.evaluate(()=>document.activeElement.dataset.pmArch),'pa-1');
        const detail=await page.locator('#pm-arch-detail').innerText();
        assert.match(detail,/交付验收/);assert.match(detail,/核对交付内容，决定通过或退回/);assert.match(detail,/验收结论/);
        assert.match(detail,/提交需求 · 文档描述/);assert.match(detail,/退回修改 · 推断（未确认）/);
        await page.locator('#pm-arch-detail button.pm-evidence').click();
        assert.equal(await page.locator('#pm-code').isVisible(),true);
        assert.equal(await page.evaluate(()=>document.activeElement.dataset.pmId),'.local-worker/review_task.py');
        assert.deepEqual(errors,[]);
      }finally{await page.close();}
    });
    for(const state of ['stale','unknown'])await t.test(`${state} explanation is accurate and keeps the project explanation`,async()=>{
      const page=await browser.newPage({viewport:{width:750,height:1000}}),errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('**/*',route=>route.abort());
      try{
        await mount(page,explanationPayload(state));
        await page.waitForFunction(()=>document.querySelectorAll('.pm-arch-node').length===2);
        const banner=await page.locator('.pm-explain-banner').innerText();
        if(state==='stale'){assert.match(banner,/讲解需要更新/);assert.match(banner,/依据|快照/);}
        else{assert.match(banner,/部分依据待核对/);assert.match(banner,/未收录|无法核对/);}
        assert.equal(await page.locator(`.pm-explain-${state}`).count(),1);
        assert.equal(await page.locator('.pm-flow-project').count(),1);
        assert.deepEqual(errors,[]);
      }finally{await page.close();}
    });
    await t.test('invalid and missing explanations fall back without losing the old map',async()=>{
      for(const scenario of ['invalid','missing']){
        const page=await browser.newPage({viewport:{width:750,height:1000}}),errors=[];
        page.on('pageerror',error=>errors.push(error.message));
        await page.route('**/*',route=>route.abort());
        try{
          const payload=explanationPayload(scenario);
          if(scenario==='invalid')payload.architecture={state:'invalid',message:'讲解文件格式无效、超过限制或路径不安全；已退回基础地图。'};
          else delete payload.architecture;
          await mount(page,payload);
          const banner=await page.locator(`.pm-explain-${scenario}`).innerText();
          if(scenario==='invalid'){assert.match(banner,/讲解文件无法使用/);assert.match(banner,/已退回基础地图/);}
          else assert.match(banner,/暂无项目讲解/);
          assert.equal(await page.locator('.pm-flow-inferred').count(),1);
          assert.equal(await page.locator('.pm-arch-node').count(),0);
          assert.equal(await page.locator('[data-pm-action="toggle-prompt"]').count(),1);
          assert.deepEqual(errors,[]);
        }finally{await page.close();}
      }
    });
    await t.test('generation entry only reveals and copies a prompt without model calls or network',async()=>{
      const page=await browser.newPage({viewport:{width:320,height:1000}}),errors=[];let requests=0;
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('**/*',route=>{requests++;return route.abort();});
      try{
        await mount(page,explanationPayload());
        await page.waitForFunction(()=>document.querySelectorAll('.pm-arch-node').length===2);
        assert.equal(await page.locator('#pm-prompt').count(),0);
        const toggle=page.locator('[data-pm-action="toggle-prompt"]');assert.match(await toggle.innerText(),/生成\/更新讲解/);
        await toggle.click();
        assert.equal(await toggle.getAttribute('aria-expanded'),'true');
        const prompt=await page.locator('#pm-prompt-text').inputValue();
        assert.match(prompt,/docs\/PROJECT_ARCHITECTURE\.md/);assert.match(prompt,/"schema":1/);
        assert.match(prompt,/"confidence":"inferred"/);assert.ok(prompt.includes(snapshot));
        assert.ok(prompt.includes('.local-worker/panel_server.py'));assert.ok(prompt.includes('.local-worker/review_task.py'));
        assert.match(prompt,/不会因为打开地图或点击按钮而自动调用模型/);
        assert.equal(await page.locator('#pm-prompt img').count(),0);
        await page.evaluate(()=>{window.__copied='';try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.__copied=text;}}});}catch{}});
        await page.locator('[data-pm-action="copy-prompt"]').click();
        await page.waitForFunction(()=>typeof window.__copied==='string'&&window.__copied.length>100);
        assert.match(await page.evaluate(()=>window.__copied),/project-architecture\.json/);
        assert.match(await page.locator('#pm-copy-status').innerText(),/已复制/);
        assert.equal(await page.evaluate(()=>window.mapCalls),1);assert.equal(requests,0);
        assert.equal(await page.locator('.pm-arch-node').count(),2);
        const layout=await page.evaluate(()=>[document.querySelector('.pm-dialog'),document.querySelector('#pm-prompt'),document.querySelector('#pm-prompt-text')].filter(Boolean).map(element=>({width:element.clientWidth,scroll:element.scrollWidth,left:element.getBoundingClientRect().left,right:element.getBoundingClientRect().right})));
        for(const box of layout){assert.ok(box.scroll<=box.width+1,JSON.stringify(box));assert.ok(box.left>=0&&box.right<=320+1,JSON.stringify(box));}
        assert.deepEqual(errors,[]);
      }finally{await page.close();}
    });
    await t.test('hostile explanation text cannot inject markup or built-in module classes',async()=>{
      const page=await browser.newPage({viewport:{width:320,height:1000}}),errors=[];
      page.on('pageerror',error=>errors.push(error.message));
      await page.route('**/*',route=>route.abort());
      try{
        const payload=explanationPayload();
        payload.architecture.title='<img src=x onerror="window.__pwn=1">项目';
        payload.architecture.summary='<script>window.__pwn=2</script>';
        payload.architecture.nodes[0].id='result';
        payload.architecture.nodes[0].title='<b onmouseover="window.__pwn=3">需求受理</b>';
        payload.architecture.nodes[0].description='<svg onload="window.__pwn=4">说明</svg>';
        payload.architecture.edges[0].label='<img src=x onerror="window.__pwn=5">';
        await mount(page,payload);
        await page.waitForFunction(()=>document.querySelectorAll('.pm-arch-node').length===2);
        assert.equal(await page.locator('#pm-overview img').count(),0);
        assert.equal(await page.locator('#pm-overview script').count(),0);
        assert.equal(await page.locator('#pm-overview b').count(),0);
        assert.equal(await page.evaluate(()=>window.__pwn),undefined);
        assert.equal(await page.locator('.pm-step-result').count(),0);
        assert.equal(await page.locator('.pm-flow-node').evaluateAll(nodes=>nodes.every(node=>!String(node.className).includes('pm-step-'))),true);
        assert.ok((await page.locator('[data-pm-arch="pa-0"]').innerText()).includes('<b onmouseover='));
        const layout=await page.evaluate(()=>[document.querySelector('.pm-dialog'),document.querySelector('#pm-flow'),...document.querySelectorAll('.pm-flow-node')].map(element=>({width:element.clientWidth,scroll:element.scrollWidth,left:element.getBoundingClientRect().left,right:element.getBoundingClientRect().right})));
        for(const box of layout){assert.ok(box.scroll<=box.width+1,JSON.stringify(box));assert.ok(box.left>=0&&box.right<=321,JSON.stringify(box));}
        assert.deepEqual(errors,[]);
      }finally{await page.close();}
    });
  }finally{await browser.close();}
});
