'use strict';
// Loaded only into an isolated ACP test profile, never a user's desktop.
const fs=require('node:fs');
const {createBridge}=require('../../integrations/dsh-brain/wakeup.cjs');
function apply(ctx,config){
  const bridges=new Map();
  ctx.on('agent/created',async({agent})=>{
    if(!ctx.agents.roots().includes(agent))return;
    const identity={id:agent.session.header.id,owner:'native-wakeup-fixture',project:agent.session.header.cwd};
    const bridge=createBridge({agent,identity,delayMs:100,
      isCurrent:()=>ctx.agents.roots().includes(agent)});
    bridges.set(agent,bridge);await bridge.start();
    const env=bridge.environment();
    const request={operation:'probe',action:'start',token:env.MODEL_DESK_DSH_WAKE_TOKEN,
      session:identity.id,owner:identity.owner};
    let running=false,armed=false;
    agent.ctx.on('agent/status',({status})=>{
      if(status==='running')running=true;
      if(running&&status==='idle'&&!armed){
        armed=true;
        const result=bridge.handle(request);
        fs.writeFileSync(config.proof,JSON.stringify({armed_after_idle:true,probe_id:result.probe_id}));
      }
    });
  });
  ctx.on('agent/disposed',({agent})=>{const bridge=bridges.get(agent);if(bridge)void bridge.dispose();});
  ctx.effect(function*(){yield async()=>{await Promise.all([...bridges.values()].map(bridge=>bridge.dispose()));};});
}
apply.inject=['agents'];module.exports=apply;
