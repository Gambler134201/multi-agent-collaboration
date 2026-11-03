'use strict';
const test=require('node:test');const assert=require('node:assert/strict');
const component=require('../panel/integration-queue.js');const {detailMarkup}=component._test;
const row={name:'queued',project:'E:\\fixture',task_ids:['one'],commits:{one:'a'.repeat(40)},accepted:false,
  queue:{schema:1,state:'queued',check_commands:[['node','--test']],requires_authority:false},checks:[]};
test('a queued record has no successful check or acceptance claims',()=>{
 const html=detailMarkup({integration:row,state:'queued'});
 assert.match(html,/尚无检查结果/);assert.match(html,/尚未接受该集成/);
 assert.match(html,/继续原队列/);assert(!html.includes('已有独立验收记录'));
});
test('owned tasks cannot be adopted by ordinary panel controls',()=>{
 const html=detailMarkup({integration:{...row,queue:{...row.queue,requires_authority:true}},state:'queued'});
 assert.match(html,/原主脑重连.*原入队请求/);assert(!html.includes('data-iq-action'));
});
test('unknown execution offers reconciliation, never retry or acceptance',()=>{
 const html=detailMarkup({integration:row,state:'recovery_required'});
 assert.match(html,/核对原作业/);assert(!html.includes('data-iq-action="resume"'));assert(!html.includes('data-iq-action="cancel"'));
});
test('failed checks retain real exit, command and output',()=>{
 const html=detailMarkup({integration:{...row,checks:[{command:['node','--test'],exit_code:3,ok:false,output_tail:'test failed'}]},state:'check_failed'});
 assert.match(html,/退出码：3/);assert.match(html,/test failed/);assert(!html.includes('data-iq-action'));
});
test('arbitrary command, file, label and error text is escaped',()=>{
 const bad='<img src=x onerror="window.pwn=1">';
 const html=detailMarkup({state:'conflict',integration:{...row,name:bad,task_ids:[bad],commits:{[bad]:bad},queue:{...row.queue,detail:bad},checks:[{command:[bad],output_tail:bad}],merges:[{conflicts:[bad]}],integration_copy:{path:bad}}});
 assert(!html.includes('<img'));assert(html.includes('&lt;img'));assert(!html.includes('href='));
});
test('server-confirmed status is separate from a contradictory accepted flag',()=>{
 const html=detailMarkup({integration:{...row,accepted:true},state:'queued'});
 assert(!html.includes('已有独立验收记录'));
});
test('new widget shares state labels and canonical project matching',()=>{
 assert.equal(component.labels.checking,'正在联合检查');
 assert(component._test.sameProject('E:\\One','e:/one/'));
 assert(!component._test.sameProject('/tmp/One','/tmp/one'));
});
test('complete but held results offer only targeted reconciliation',()=>{
 const html=detailMarkup({integration:row,state:'awaiting_review',recovery:{state:'ready',can_settle:true,detail:'完整结果已保存，原进程已退出。'}});
 assert.match(html,/收尾与占用/);assert.match(html,/完整结果已保存/);
 assert.match(html,/data-iq-action="reconcile"/);assert(!html.includes('data-iq-action="resume"'));
 assert(!html.includes('已有独立验收记录'));
});
test('owner-bound recovery directs to the formal tool without panel adoption',()=>{
 const html=detailMarkup({integration:{...row,queue:{...row.queue,requires_authority:true}},state:'awaiting_review',recovery:{can_settle:true,detail:'原作业可核对'}});
 assert.match(html,/brain_reconcile_integration/);assert(!html.includes('data-iq-action'));
});
test('settlement does not turn failed checks into accepted delivery',()=>{
 const html=detailMarkup({integration:row,state:'check_failed',recovery:{state:'settled',can_settle:false,detail:'原作业已不持有占用'}});
 assert.match(html,/原作业已不持有占用/);assert.match(html,/尚未接受该集成/);
 assert(!html.includes('data-iq-action'));assert(!html.includes('已有独立验收记录'));
});
