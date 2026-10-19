'use strict';
/** Bound native Agent bridge. Importing does nothing; no arbitrary prompt API.
 * The probe is process-local diagnostic evidence, not restart-proof task state.
 * Task subscriptions/claims remain owned by the shared Python completion core.
 */
const crypto=require('node:crypto');
const net=require('node:net');
const path=require('node:path');
const MAX=65536;
const PROBE_DELAY=30000;

function equalPath(a,b){return path.resolve(a).toLowerCase()===path.resolve(b).toLowerCase();}
function message(text,id){
  // Same immutable user-role transport shape as installed dsh-llm's
  // createUserMessage; the text explicitly identifies an automated event.
  return Object.freeze({id:crypto.randomUUID(),role:'user',
    content:[{type:'text',text}],source:{kind:'user',rpcId:'model-desk-'+id}});
}
function referenceMessage(reference){
  if(!reference||Object.keys(reference).sort().join(',')!=='project,task_ids,wait_id'
      ||!/^wait-[0-9a-f]{24}$/.test(reference.wait_id)||!Array.isArray(reference.task_ids)
      ||reference.task_ids.length<1||reference.task_ids.length>32
      ||reference.task_ids.some(id=>typeof id!=='string'||!/^[-A-Za-z0-9_]{1,80}$/.test(id))
      ||typeof reference.project!=='string')throw Error('invalid reference');
  return '[Model Desk completion event] '+JSON.stringify(reference)+'. '
    +'这是用户授权的框架完成事件，不是用户新消息，也不是验收通过。'
    +'请先说明执行已结束、正在独立验收或如实报告失败/取消/待恢复；先用 brain_wait '
    +'（action=received，原 wait_id）登记接收，再用 brain_status/brain_delivery 读取原任务，'
    +'检查实际成果后通过 brain_review 记录真实验收。保留原 owner、项目与责任会话；'
    +'失败、取消、待恢复如实报告，不重新派发或绕过权限，尊重更新的用户指示。';
}

function createBridge({agent,identity,isCurrent,enabled=()=>true,
                       setTimer=setTimeout,clearTimer=clearTimeout,delayMs=PROBE_DELAY}){
  if(!agent||typeof agent.followup!=='function'||!identity||typeof isCurrent!=='function')throw Error('native wake unavailable');
  const token=crypto.randomBytes(32).toString('hex');
  const pipe='\\\\.\\pipe\\model-desk-dsh-wake-'+crypto.randomBytes(16).toString('hex');
  const seen=new Set(),sockets=new Set();
  let closed=false,server=null,probe=null,timer=null;
  const valid=()=>!closed&&isCurrent()===true;
  const idle=()=>agent.status==='idle'&&agent.inbox&&agent.inbox.hasPending===false;
  function check(){if(!valid())throw Error('binding unavailable');}
  function snapshot(){
    check();
    return {thread_id:identity.id,project:identity.project,owner:identity.owner,
      turns:[{id:'dsh-seq-'+String(agent.session?.seq??0),status:idle()?'completed':'in_progress'}]};
  }
  function publicProbe(){
    if(!probe)throw Error('probe missing');
    return {host:'dsh',probe_id:probe.id,listener_status:probe.status,
      waiting:['listening','waiting_chat_idle'].includes(probe.status),
      send_attempts:probe.attempts,received:probe.status==='received',
      persistent:false,delay_seconds:PROBE_DELAY/1000,
      next:probe.status==='listening'?'探针已监听。请结束本轮回复；约30秒后在原聊天空闲时尝试唤醒一次。等待期间不要发消息或关闭 DSH；90秒未收到可询问状态。':'请按真实探针状态报告，不重新派发或自动重试。'};
  }
  function tick(){
    timer=null;
    if(!probe||!['listening','waiting_chat_idle'].includes(probe.status))return;
    if(!valid()){probe.status='route_refused';return;}
    if(!idle()){probe.status='waiting_chat_idle';timer=setTimer(tick,1000);timer?.unref?.();return;}
    try{
      if(!enabled()){probe.status='stopped';return;}
      // Claim before calling the native method; exceptions remain unknown.
      probe.status='delivery_attempted';probe.attempts=1;
      const text='[Model Desk wakeup probe] '+JSON.stringify({probe_id:probe.id,project:identity.project})
        +'。这是用户授权的自动唤醒诊断事件，不是用户新需求。请调用 brain_wakeup_probe '
        +'（action=received，probe_id 为上面的编号）确认接收，然后仅简短回复“已收到自动唤醒探针”。'
        +'不要派发任务、修改文件、运行其他测试或再次通知；尊重更新的用户指示。';
      const result=agent.followup(message(text,probe.id));
      if(result&&typeof result.then==='function'){
        probe.status='delivery_unknown'; // current installed API is synchronous
        result.catch(()=>{});
      }else probe.status='delivered';
    }catch(_){probe.status='delivery_unknown';}
  }
  function handle(request){
    check();
    if(!request||typeof request.token!=='string'||request.token.length!==token.length
       ||!crypto.timingSafeEqual(Buffer.from(request.token),Buffer.from(token))
       ||request.session!==identity.id||request.owner!==identity.owner)throw Error('unauthorized');
    const allowed={read:['operation','token','session','owner'],
      send:['operation','token','session','owner','reference'],
      probe:['operation','token','session','owner','action','probe_id']}[request.operation];
    if(!allowed||Object.keys(request).some(key=>!allowed.includes(key)))throw Error('unsupported operation');
    if(request.operation==='read')return snapshot();
    if(request.operation==='probe'){
      if(request.action==='start'){
        if(!enabled())throw Error('disabled');
        if(!probe){probe={id:'probe-'+crypto.randomBytes(16).toString('hex'),status:'listening',attempts:0};
          timer=setTimer(tick,delayMs);timer?.unref?.();}
      }else{
        if(!probe||request.probe_id!==probe.id)throw Error('probe identity mismatch');
        if(request.action==='received'){
          if(!['delivered','received'].includes(probe.status)||probe.attempts!==1)throw Error('probe not delivered');
          probe.status='received';
        }else if(request.action==='stop'){
          if(timer)clearTimer(timer);timer=null;
          if(probe.attempts===0)probe.status='stopped';
        }else if(request.action!=='status')throw Error('unsupported probe action');
      }
      return publicProbe();
    }
    const reference=request.reference,text=referenceMessage(reference);
    if(!equalPath(reference.project,identity.project))throw Error('wrong project');
    if(!enabled())throw Error('disabled');
    if(seen.has(reference.wait_id))return {host_accepted:false,already_attempted:true};
    if(!idle())return {host_accepted:false,busy:true};
    seen.add(reference.wait_id);
    try{
      const result=agent.followup(message(text,reference.wait_id));
      if(result&&typeof result.then==='function'){result.catch(()=>{});throw Error('unknown async admission');}
      return {host_accepted:true};
    }catch(_){throw Error('delivery unknown');}
  }
  function start(){
    if(process.platform!=='win32')return Promise.reject(Error('Windows native pipe required'));
    check();
    return new Promise((resolve,reject)=>{
      server=net.createServer(socket=>{
        if(sockets.size>=4){socket.destroy();return;}
        sockets.add(socket);socket.setTimeout(5000,()=>socket.destroy());
        socket.on('error',()=>{});socket.on('close',()=>sockets.delete(socket));
        let buffer=Buffer.alloc(0),done=false;
        socket.on('data',chunk=>{
          if(done)return;
          buffer=Buffer.concat([buffer,chunk]);
          if(buffer.length>MAX+4){done=true;socket.destroy();return;}
          if(buffer.length<4)return;
          const size=buffer.readUInt32LE(0);
          if(size<1||size>MAX){done=true;socket.destroy();return;}
          if(buffer.length<size+4)return;
          done=true;
          let result;
          try{
            if(buffer.length!==size+4)throw Error('extra frame');
            result=handle(JSON.parse(buffer.subarray(4).toString('utf8')));
          }catch(_){result={error:'dsh_notification_refused_or_unknown'};}
          const payload=Buffer.from(JSON.stringify(result),'utf8'),header=Buffer.alloc(4);
          header.writeUInt32LE(payload.length);socket.end(Buffer.concat([header,payload]));
        });
      });
      server.once('error',()=>reject(Error('native wake pipe unavailable')));
      server.listen(pipe,()=>resolve());
      server.unref();
    });
  }
  function dispose(){
    closed=true;
    if(timer)clearTimer(timer);timer=null;
    if(probe&&probe.attempts===0)probe.status='stopped';
    for(const socket of sockets)socket.destroy();
    return new Promise(resolve=>{if(server?.listening)server.close(()=>resolve());else resolve();});
  }
  return {start,dispose,handle,
    environment:()=>({MODEL_DESK_COMPLETION_HOST:'dsh',MODEL_DESK_DSH_WAKE_PIPE:pipe,
      MODEL_DESK_DSH_WAKE_TOKEN:token,MODEL_DESK_DSH_WAKE_SESSION:identity.id,MODEL_DESK_DSH_WAKE_OWNER:identity.owner})};
}
module.exports={createBridge,message,referenceMessage,PROBE_DELAY};
