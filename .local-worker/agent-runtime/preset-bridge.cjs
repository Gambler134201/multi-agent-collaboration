// ACP does not expose Agent-preset selection in this DSH version. Bind the real
// registry declaration at its awaited agent/created edge, before the first turn.
const fs=require('fs');
function apply(ctx,config){
  ctx.on('agent/created',async ({agent})=>{
    const identity=await ctx.agentPresets.mount(agent.ctx,config.preset);
    const actual=ctx.agentPresets.composedPreset(agent.ctx);
    if(identity.id!==config.preset||actual!==config.preset)throw new Error('Model Desk Agent preset binding mismatch');
    agent.session.append('agent-preset/selected',{agentPreset:actual});
    const file=process.env.MODEL_DESK_PRESET_PROOF;
    if(!file)throw new Error('Model Desk preset proof path is missing');
    // Each run has a fresh proof path. The awaited parent-created edge writes
    // first; a later isolated child must not overwrite the parent's proof.
    if(!fs.existsSync(file))fs.writeFileSync(file,JSON.stringify({preset:actual,revision:config.revision,session_id:agent.id,source:config.source,policy_overrides:config.policyOverrides}));
  });
}
apply.inject=['agentPresets','agents'];
module.exports=apply;
