// Real pinned ACPX prompt timing, fake transport only: no DSH or model inference.
import assert from 'node:assert/strict';
import {l as runPromptTurn, dt as TimeoutError} from 'acpx/dist/queue-owner-runtime-DoijBCvt.js';

function parameters(timeoutMs, signal) {
  return {
    sessionId:'offline', prompt:'fixture', timeoutMs,
    authority:signal?{signal}:undefined,
    client:{
      prompt:(_session,_prompt,_written,_elicitation,_permission,authority)=>new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>resolve({stopReason:'end_turn'}),80);
        authority.signal.addEventListener('abort',()=>{clearTimeout(timer);reject(new Error('fixture cancelled'));},{once:true});
      }),
      waitForSessionUpdatesIdle:async()=>{},
    },
  };
}
const slow = await runPromptTurn(parameters(undefined));
assert.equal(slow.stopReason,'end_turn');
await assert.rejects(runPromptTurn(parameters(5)),TimeoutError);
const controller=new AbortController();
const stopped=runPromptTurn(parameters(undefined,controller.signal));
setTimeout(()=>controller.abort(),5);
await assert.rejects(stopped,/fixture cancelled/);
console.log('Pinned ACPX: unlimited slow prompt completes; explicit timeout and cancellation work.');
