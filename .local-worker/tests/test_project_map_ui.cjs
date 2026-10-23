'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const ui=require('../panel/project-map.js')._test;
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const data=project=>({schema:1,project,nodes:[{id:'a.py',path:'a.py',symbols:[{name:'run'}]},{id:'b.py',path:'b.py',symbols:[]},{id:'c.py',path:'c.py',symbols:[]}],edges:[{source:'a.py',target:'b.py'}]});

test('unbound and all views make no request',async()=>{
  for(const mode of ['unbound','all']){
    let calls=0;const s=ui.createSession({api:async()=>{calls++;},getView:()=>({mode,project:null})});
    assert.equal(await s.open(),false);assert.equal(calls,0);
  }
});
test('project snapshot is passed explicitly and repeated open does not duplicate',async()=>{
  let calls=0;const s=ui.createSession({api:async(p,body,view)=>{calls++;assert.equal(p,'/api/project-map');assert.equal(view.project,'E:/a');assert.equal(body.refresh,calls>1);return data('E:/a');},getView:()=>({mode:'project',project:'E:/a'})});
  await s.open();await s.open();assert.equal(calls,1);await s.load(true);assert.equal(calls,2);
});
test('closing invalidates a late response',async()=>{
  const pending=deferred(),s=ui.createSession({api:()=>pending.promise,getView:()=>({mode:'project',project:'E:/a'})});
  const loading=s.open();s.close();pending.resolve(data('E:/a'));assert.equal(await loading,false);assert.equal(s.snapshot().data,null);
});
test('project switch never displays an old response, next open uses new view',async()=>{
  let view={mode:'project',project:'E:/a'};const pending=deferred();let calls=0;
  const s=ui.createSession({api:()=>++calls===1?pending.promise:Promise.resolve(data('E:/b')),getView:()=>view});
  const loading=s.open();view={mode:'project',project:'E:/b'};s.close();await s.open();pending.resolve(data('E:/a'));await loading;
  assert.equal(s.snapshot().data.project,'E:/b');
});
test('refresh responses are applied only in latest generation order',async()=>{
  const first=deferred(),second=deferred();let calls=0;
  const s=ui.createSession({api:()=>++calls===1?first.promise:second.promise,getView:()=>({mode:'project',project:'E:/a'})});
  const open=s.open(),refresh=s.load(true);second.resolve({...data('E:/a'),marker:'new'});await refresh;
  first.resolve({...data('E:/a'),marker:'old'});await open;assert.equal(s.snapshot().data.marker,'new');
});
test('wrong response project is rejected as an error, no stale data',async()=>{
  const s=ui.createSession({api:async()=>data('E:/b'),getView:()=>({mode:'project',project:'E:/a'})});
  assert.equal(await s.open(),false);assert.equal(s.snapshot().data,null);assert.match(s.snapshot().error,/不匹配/);
});
test('search and graph selection use real node identities with limits',()=>{
  const d=data('E:/a');assert.deepEqual(ui.visibleNodes(d,'run').map(n=>n.id),['a.py']);
  assert.deepEqual(ui.graphSlice(d,'a.py',false).nodes.map(n=>n.id),['a.py','b.py']);
  const limited=ui.graphSlice(d,'c.py',true,'',1);assert.equal(limited.nodes[0].id,'c.py');assert.equal(limited.truncated,true);
});
test('overview explains inferred areas and counts only current-project tasks',()=>{
  const d={...data('E:/a'),nodes:[
    {id:'ui/app.js',path:'ui/app.js'},
    {id:'worker.py',path:'worker.py'},
    {id:'tests/test_worker.py',path:'tests/test_worker.py'}
  ],edges:[{source:'ui/app.js',target:'worker.py'}]};
  const result=ui.overview(d,[
    {project:'E:/a',status:'running'},
    {project:'E:/a',status:'completed'},
    {project:'E:/b',status:'queued'}
  ]);
  assert.equal(result.tasks,2);assert.equal(result.active,1);
  assert.deepEqual(result.groups.map(group=>group.name),['测试与检查','界面与交互','执行与工具']);
  assert.deepEqual(result.links,[{from:'界面与交互',to:'执行与工具'}]);
});
test('markup values are escaped, module uses no network/storage or code execution',()=>{
  assert.equal(ui.escapeText('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
  const code=fs.readFileSync(path.join(__dirname,'../panel/project-map.js'),'utf8');
  assert.doesNotMatch(code,/\bfetch\s*\(|localStorage|sessionStorage|eval\s*\(|new Function|setInterval/);
  assert.doesNotMatch(code,/file:\/\/|javascript:/);
});
test('runtime base directory does not classify all code as execution tools',()=>{
  const paths=['.local-worker/panel_server.py','.local-worker/review_task.py','.local-worker/execution_engines.py'];
  const result=ui.overview({nodes:paths.map(path=>({id:path,path})),edges:[]});
  assert.deepEqual(result.groups.map(group=>group.name),['协作与任务','执行与工具','数据与服务']);
});
test('project walkthrough requires root implementation, includes rework and ongoing records',()=>{
  const paths=['panel_server.py','brain_gateway.py','dispatch_task.py','execution_engines.py','review_task.py'];
  const make=prefix=>({nodes:paths.map(name=>({id:prefix+name,path:prefix+name})),edges:[]});
  for(const prefix of ['release/.local-worker/','src/']){
    const payload=make(prefix);assert.equal(ui.architecture(payload,ui.overview(payload)).kind,'inferred');
  }
  const payload=make('.local-worker/'),diagram=ui.architecture(payload,ui.overview(payload));
  assert.equal(diagram.kind,'explained');assert.equal(diagram.steps.length,5);
  assert.ok(diagram.links.some(link=>link.source==='review'&&link.target==='execute'&&link.kind==='rework'));
  assert.deepEqual(diagram.links.filter(link=>link.target==='result').map(link=>link.source),['plan','execute','review']);
  assert.ok(diagram.steps.every(step=>step.files.every(file=>payload.nodes.includes(file))));
});
test('generic module arrows come only from observed static links',()=>{
  const payload={nodes:[{id:'ui/app.js',path:'ui/app.js'},{id:'api/server.py',path:'api/server.py'}],edges:[]};
  assert.equal(ui.architecture(payload,ui.overview(payload)).links.length,0);
  payload.edges=[{source:'ui/app.js',target:'api/server.py'}];
  const diagram=ui.architecture(payload,ui.overview(payload));
  assert.equal(diagram.kind,'inferred');assert.equal(diagram.links.length,1);
  assert.equal(diagram.links[0].label,'源码静态引用');
});
test('project-authored explanation takes priority over the built-in walkthrough',()=>{
  const hash='a'.repeat(64);
  const payload={schema:1,project:'E:/a',index:{hash},nodes:[
    {id:'.local-worker/panel_server.py',path:'.local-worker/panel_server.py',sha256:'b'.repeat(64)},
    {id:'.local-worker/brain_gateway.py',path:'.local-worker/brain_gateway.py'},
    {id:'.local-worker/dispatch_task.py',path:'.local-worker/dispatch_task.py'},
    {id:'.local-worker/execution_engines.py',path:'.local-worker/execution_engines.py'},
    {id:'.local-worker/review_task.py',path:'.local-worker/review_task.py'}
  ],edges:[],architecture:{state:'matched',source:'project-architecture.json',provenance:'project_authored',title:'订单项目',summary:'说明',snapshot_hash:hash,nodes:[
    {id:'result',title:'业务模块',role:'职责',description:'说明',output:'产出',state:'matched',evidence:[{path:'.local-worker/panel_server.py',sha256:'b'.repeat(64),state:'unchanged'}]},
    {id:'other',title:'关联模块',role:'',description:'另一模块',output:'',state:'unknown',evidence:[{path:'missing.py',sha256:'c'.repeat(64),state:'unindexed'}]}
  ],edges:[{source:'result',target:'other',label:'业务关系',confidence:'documented'},{source:'result',target:'other',label:'推断关系',confidence:'inferred'}]}};
  const explanation=ui.projectExplanation(payload);
  assert.equal(explanation.available,true);assert.equal(explanation.diagram.kind,'project');
  assert.deepEqual(explanation.diagram.steps.map(step=>step.name),['业务模块','关联模块']);
  assert.ok(explanation.diagram.steps.every(step=>step.id.startsWith('pa-')));
  assert.equal(explanation.diagram.links[0].confidence,'documented');assert.equal(explanation.diagram.links[1].confidence,'inferred');
  assert.equal(explanation.diagram.steps[0].evidence[0].nodeId,'.local-worker/panel_server.py');
  assert.equal(explanation.diagram.steps[1].evidence[0].nodeId,null);
  assert.equal(ui.projectExplanation({...payload,architecture:{state:'missing'}}).available,false);
  assert.equal(ui.projectExplanation({...payload,architecture:{state:'invalid',message:'安全提示'}}).message,'安全提示');
});
test('explanation states stay accurate and never promise business verification',()=>{
  const base={nodes:[{id:'a.py',path:'a.py'}],architecture:{state:'matched',title:'项目',summary:'摘要',snapshot_hash:'',nodes:[{id:'x',title:'模块',description:'说明',state:'matched',evidence:[{path:'a.py',sha256:'d'.repeat(64),state:'unchanged'}]}],edges:[]}};
  assert.equal(ui.projectExplanation(base).state,'matched');
  const stale=ui.projectExplanation({...base,architecture:{...base.architecture,state:'stale'}});
  assert.equal(stale.state,'stale');assert.equal(stale.needsUpdate,true);
  const unknown=ui.projectExplanation({...base,architecture:{...base.architecture,state:'unknown',nodes:[{...base.architecture.nodes[0],state:'unknown',evidence:[{path:'gone.py',sha256:'d'.repeat(64),state:'unindexed'}]}]}});
  assert.equal(unknown.state,'unknown');assert.equal(unknown.diagram.steps[0].evidence[0].state,'unindexed');
  const mismatch=ui.projectExplanation({...base,index:{hash:'e'.repeat(64)},architecture:{...base.architecture,snapshot_hash:'f'.repeat(64)}});
  assert.equal(mismatch.state,'stale');
  const invalid=ui.projectExplanation({...base,architecture:{state:'invalid',message:'格式无效'}});
  assert.equal(invalid.state,'invalid');assert.equal(invalid.available,false);
});
test('generation prompt is bounded, points at the convention, and is explicitly non-executing',()=>{
  const nodes=Array.from({length:40},(_,index)=>({path:'src/f'+index+'.py'}));
  const prompt=ui.architecturePrompt({index:{hash:'a'.repeat(64)},nodes,architecture:{state:'missing'}});
  assert.match(prompt,/docs\/PROJECT_ARCHITECTURE\.md/);
  assert.match(prompt,/"schema":1/);
  assert.match(prompt,/"confidence":"inferred"/);
  assert.ok(prompt.includes('- src/f0.py'));
  assert.equal(prompt.split('\n').filter(line=>line.startsWith('- src/')).length,30);
  assert.ok(!prompt.includes('src/f38.py')&&!prompt.includes('src/f39.py'));
  assert.match(prompt,/另有 10 个未列出/);
  assert.match(prompt,/不会因为打开地图或点击按钮而自动调用模型/);
  assert.match(prompt,/SHA-256/);
  assert.ok(!prompt.includes('def run('));
});
test('dialog is created once, renders escaped files, and clears on project change',async()=>{
  const saved=global.document;
  class Element {
    constructor(){this.children=[];this.elements=new Map();this.listeners={};this.innerHTML='';this.open=false;this.isConnected=true;}
    setAttribute(){}
    append(value){this.children.push(value);}
    querySelector(key){if(!this.elements.has(key))this.elements.set(key,new Element());return this.elements.get(key);}
    addEventListener(name,fn){this.listeners[name]=fn;}
    replaceChildren(){this.innerHTML='';}
    showModal(){this.open=true;}
    close(){this.open=false;this.listeners.close?.();}
    focus(){this.focused=true;}
  }
  const prior=new Element(),body=new Element();global.document={body,activeElement:prior,createElement:()=>new Element()};
  let view={mode:'project',project:'E:/a'},calls=0;
  const malicious='src/<img onerror="x">.py',payload=data('E:/a');payload.nodes[0]={id:malicious,path:malicious,symbols:[],tests:[]};
  const api=async()=>{calls++;return payload;};
  try{
    const instance=require('../panel/project-map.js').create({api,getView:()=>view});
    assert.equal(require('../panel/project-map.js').create({api,getView:()=>view}),instance);
    await instance.open();await instance.open();assert.equal(calls,1);assert.equal(body.children.length,1);
    const dialog=body.children[0];assert.equal(dialog.open,true);
    assert.match(dialog.innerHTML,/读懂这个项目/);
    assert.match(dialog.querySelector('#pm-overview').innerHTML,/可交互项目架构图/);
    assert.match(dialog.querySelector('#pm-overview').innerHTML,/项目模块关系/);
    assert.ok(!dialog.querySelector('#pm-overview').innerHTML.includes('<img'));
    assert.equal(dialog.querySelector('#pm-code').hidden,true);
    const list=dialog.querySelector('#pm-list').innerHTML;
    assert.ok(list.includes('&lt;img'));assert.ok(!list.includes('<img'));
    view={mode:'project',project:'E:/b'};instance.onProjectChange();assert.equal(dialog.open,false);
    assert.equal(dialog.querySelector('#pm-list').innerHTML,'');assert.equal(prior.focused,true);
  }finally{global.document=saved;}
});
