import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../panel/app.js',import.meta.url),'utf8');
const start=source.indexOf('function completionWaitText(');
const end=source.indexOf('function renderTasks()',start);
assert.ok(start>=0&&end>start);
const label=vm.runInNewContext(source.slice(start,end)+';completionWaitText');
const now=Date.now();
const base={status:'running',completion_wait:{id:'fixture',listener_status:'listening',phase:'waiting_execution',heartbeat_at:new Date(now).toISOString()}};
assert.match(label(base,now),/正在等待/);
assert.match(label({...base,status:'completed'},now),/待 GPT 独立验收/);
assert.doesNotMatch(label({...base,status:'completed'},now),/验收通过/);
assert.match(label({...base,status:'completed',completion_wait:{...base.completion_wait,phase:'reviewing',listener_status:'received'}},now),/正在独立验收/);
for(const state of ['accepted','final_accepted'])assert.match(label({...base,review:{state}},now),/任务已完成.*验收通过/);
assert.match(label({...base,review:{state:'returned'}},now),/退回整改/);
assert.match(label({...base,review:{state:'blocked'}},now),/受阻/);
assert.match(label(base,now+31000),/未确认/);
for(const status of ['failed','cancelled','recovery_pending'])assert.doesNotMatch(label({...base,status},now),/验收通过/);
assert.match(label({...base,completion_wait:{...base.completion_wait,listener_status:'delivery_unknown'}},now),/通知未确认/);
assert.equal(label({status:'running'},now),'');
console.log('PASS completion status: waiting, stale listener, review, accepted, rework, failure and unknown delivery');

const helpersStart=source.indexOf('function taskAccepted(');
const helpersEnd=source.indexOf('function badge(',helpersStart);
const helpers=vm.runInNewContext(source.slice(helpersStart,helpersEnd)+';({taskAccepted,taskDisplayStatus,taskDisplayPhase,duration})');
const interrupted={status:'recovery_pending',phase:'待恢复（面板重启，未自动重发）',
  started_at:'2026-10-16T10:00:00Z',ended_at:null,
  review:{state:'final_accepted',updated_at:'2026-10-16T10:07:00Z',final:{accepted_at:'2026-10-16T10:05:00Z'}}};
const untouched=JSON.stringify(interrupted);
assert.equal(helpers.taskDisplayStatus(interrupted),'completed');
assert.match(helpers.taskDisplayPhase(interrupted),/主脑接管完成.*独立验收通过/);
assert.doesNotMatch(helpers.taskDisplayPhase(interrupted),/待恢复/);
assert.equal(helpers.duration(interrupted),'5 分 0 秒');
assert.equal(JSON.stringify(interrupted),untouched,'UI must preserve original executor evidence');
for(const review of [undefined,{state:'returned'},{state:'blocked'},{error:'unreadable'}]){
  const pending={...interrupted,review,review_state:'final_accepted'};
  assert.equal(helpers.taskDisplayStatus(pending),'recovery_pending','cached acceptance is not authoritative');
  assert.match(helpers.taskDisplayPhase(pending),/待恢复/);
}
assert.equal(helpers.taskDisplayStatus({...interrupted,status:'running'}),'running','do not hide live execution');
assert.equal(helpers.taskDisplayStatus({...interrupted,status:'failed'}),'completed');
assert.equal(helpers.duration({...interrupted,review:{state:'accepted',updated_at:'2026-10-16T10:07:00Z'}}),'7 分 0 秒');
assert.equal(helpers.duration({...interrupted,review:{state:'final_accepted'}}),'用时未记录');
console.log('PASS task presentation: accepted takeover completes UI, freezes duration, preserves raw evidence and leaves real pending tasks visible');
