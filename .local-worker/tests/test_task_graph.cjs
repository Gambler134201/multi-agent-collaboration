'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../panel/task-graph.js')._test;
const project = 'E:\\graph-fixture';
const stage = (id, deps = [], more = {}) => ({id, title:id, depends_on:deps, status:'pending', ...more});
const goal = stages => ({id:'g', project, stages});

test('diamond depends only on explicit dependencies, never ordinal or timestamps', () => {
  const data = goal([stage('end',['a','b']),stage('b',['start']),stage('start'),stage('a',['start'])]);
  const before = JSON.stringify(data), graph = ui.buildGraph(data);
  assert.deepEqual(graph.layers.map(l => l.map(n => n.id)), [['start'],['b','a'],['end']]);
  assert.equal(graph.edges.length,4); assert.equal(JSON.stringify(data),before);
  assert.equal(ui.buildGraph(goal([stage('x',[],{ordinal:1}),stage('y',[],{ordinal:2})])).edges.length,0);
});
test('multiple roots and no invented integration or acceptance nodes', () => {
  const graph = ui.buildGraph(goal([stage('a'),stage('b'),stage('c')]));
  assert.equal(graph.nodes.length,3); assert.equal(graph.layers.length,1); assert.deepEqual(graph.edges,[]);
});
test('missing, cross-goal and omitted dependencies are not assumed satisfied', () => {
  const graph = ui.buildGraph(goal([stage('a',['outside','outside'])]));
  assert.deepEqual(graph.nodes[0].missing,['outside']); assert.equal(graph.edges.length,0);
  assert.match(graph.warnings.join(''),/未读取到/);
});
test('cycles, self cycles and downstream affected nodes remain visible without fictitious edges', () => {
  const graph = ui.buildGraph(goal([stage('a',['b']),stage('b',['a']),stage('c',['b']),stage('d')]));
  assert.equal(graph.nodes.filter(n=>n.cyclic).length,3); assert.equal(graph.edges.length,0);
  assert.equal(ui.buildGraph(goal([stage('a',['a'])])).nodes[0].cyclic,true);
});
test('duplicate and malformed ids cannot redirect a dependency', () => {
  const graph = ui.buildGraph(goal([stage('x'),stage('x'),stage('y',['x']),null,{},stage('')]));
  assert.deepEqual(graph.nodes.map(n=>n.id),['y']); assert.deepEqual(graph.nodes[0].missing,['x']);
  assert.equal(graph.warnings.length,3);
});
test('bounded graph reports truncation and unresolved references', () => {
  const graph = ui.buildGraph(goal(Array.from({length:90},(_,i)=>stage('s'+i,i===0?['s89']:[]))));
  assert.equal(graph.nodes.length,80); assert.deepEqual(graph.nodes[0].missing,['s89']);
  assert.match(graph.warnings.join(''),/最多展示 80/);
});
test('server-side partial stage list is explicitly marked incomplete', () => {
  const graph=ui.buildGraph({...goal([stage('a')]),stage_count:7});
  assert.match(graph.warnings.join(''),/1 \/ 7 个阶段/);
});
test('unbound or all-project views do not expose any goals or tasks', () => {
  const data={tree:{goals:[goal([])]},tasks:[{id:'t',project}]};
  for(const view of [{mode:'unbound'},{mode:'all'},{mode:'project',project:''}]) {
    const result=ui.scopedSnapshot(data,view); assert.equal(result.goals.length,0); assert.equal(result.tasks.length,0); assert(result.error);
  }
});
test('project matching respects Windows case and separators, rejects other or missing identity', () => {
  const data={tree:{goals:[goal([]),{id:'unknown'},{...goal([]),project:'E:\\other'}]},tasks:[{id:'own',project},{id:'foreign',project:'E:\\other'},{id:'missing'}]};
  const result=ui.scopedSnapshot(data,{mode:'project',project:'e:/GRAPH-fixture/'});
  assert.equal(result.goals.length,1);assert.deepEqual(result.tasks.map(t=>t.id),['own']);assert(result.note);
  assert.equal(ui.sameProject('/tmp/A','/tmp/a'),false);
});
test('read failures differ from confirmed empty and stale snapshots keep an explicit error', () => {
  assert.match(ui.scopedSnapshot({tree:null},{mode:'project',project}).error,/尚未读取成功/);
  assert.equal(ui.scopedSnapshot({tree:{goals:[]}},{mode:'project',project}).error,'');
  const r=ui.scopedSnapshot({tree:{goals:[goal([])]},error:'offline'},{mode:'project',project});
  assert.equal(r.error,'offline');assert.equal(r.goals.length,1);
});
test('status comes from shared projection, never the free-text phase or model answer', () => {
  const s=stage('a',[],{status:'executor_completed',phase:'已验收',answer:'成功'});
  assert.equal(ui.summary(s).label,'等待独立验收');
  s.summary={schema:1,source:'controller_projection',label:'结果未知，等待核对',code:'outcome_unknown'};
  assert.equal(ui.summary(s).label,'结果未知，等待核对');
});
test('active task model is factual; fixed_model without execution is labelled as a plan', () => {
  const s=stage('a',[],{active_task_id:'new',task_ids:['old','new'],fixed_model:'plan'});
  assert.equal(ui.identity(s,[{id:'old',engine:'pi',model_name:'old'},{id:'new',engine:'zcode',model_name:'actual'}]),'ZCode · actual');
  assert.equal(ui.identity({...s,active_task_id:''},[]),'计划模型：plan');
  assert.equal(ui.identity(stage('empty'),[]),'尚无当前执行任务');
});
test('attempt association includes rework entries but does not invent tasks', () => {
  const s=stage('a',[],{task_ids:['t','t'],active_task_id:'u',tasks:[{task_id:'t',review_label:'已退回'}]});
  assert.deepEqual(ui.attempts(s,[{id:'t',engine:'pi'}]).map(t=>t.id),['t','u']);
  assert.equal(ui.attempts(s,[{id:'foreign'}]).some(t=>t.id==='foreign'),false);
});
test('hostile ids, titles, status labels, models, files and evidence are escaped', () => {
  const bad='<img src=x onerror="globalThis.pwned=1">';
  const s=stage(bad,[],{title:bad,objective:bad,files:[bad],acceptance:[bad],active_task_id:'t',
    summary:{schema:1,source:'controller_projection',label:bad,tone:bad,detail:bad,next_step:bad},
    task_ids:['t'],delivery:{deliveries:[{task_id:'t',summary:bad,files:[{path:bad}],fresh:{valid:false}}]}});
  const graph=ui.buildGraph(goal([s])), tasks=[{id:'t',engine:'zcode',model_name:bad,title:bad}];
  const html=ui.nodeMarkup(graph.nodes[0],bad,tasks)+ui.detailMarkup(graph.nodes[0],graph,tasks);
  assert(!html.includes('<img'));assert(html.includes('&lt;img'));assert(!html.includes('tg-tone-<'));
});
test('handover validity and expected files remain distinct', () => {
  const graph=ui.buildGraph(goal([stage('a',[],{files:['expected.js'],delivery:{deliveries:[]}})]));
  assert.match(ui.detailMarkup(graph.nodes[0],graph,[]),/预期文件不是实际交付证明/);
  graph.nodes[0].stage.delivery.deliveries=[{files:[{path:'actual.js'}],fresh:{stale:true},task_id:'t'}];
  const html=ui.detailMarkup(graph.nodes[0],graph,[]);assert.match(html,/交接依据已过期/);assert.match(html,/actual.js/);
});
test('read-only stages label files as context, never promised changed files', () => {
  const graph=ui.buildGraph(goal([stage('a',[],{kind:'chat',files:['read-only.js']})]));
  const html=ui.detailMarkup(graph.nodes[0],graph,[]);
  assert.match(html,/只读上下文/);assert(!html.includes('<h4>预期交付</h4>'));
});
