// Read the running native account service. No auth/profile file is opened.
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const job=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const output=v=>process.stdout.write(JSON.stringify(v)+'\n');
let ws;
try{
  const pages=await(await fetch(`http://127.0.0.1:${job.port}/json/list`,{signal:AbortSignal.timeout(3000),redirect:'error'})).json();
  const root=pathToFileURL(path.join(job.home,'resources/app.asar')).href.toLowerCase();
  const match=pages.filter(p=>p.type==='page'&&p.url?.split('?')[0].toLowerCase().startsWith(root+'/renderer/'));
  if(match.length!==1)throw Error('Native main window not uniquely identified');
  const url=new URL(match[0].webSocketDebuggerUrl);
  if(url.protocol!=='ws:'||url.hostname!=='127.0.0.1'||Number(url.port)!==job.port)throw Error('Unexpected bridge endpoint');
  ws=new WebSocket(url.href);
  await new Promise((resolve,reject)=>{const t=setTimeout(()=>reject(Error('Connect timeout')),3000);ws.onopen=()=>{clearTimeout(t);resolve();};ws.onerror=()=>{clearTimeout(t);reject(Error('Connect failed'));};});
  const module=pathToFileURL(path.join(job.home,'resources/app.asar/renderer/assets',job.usageModule)).href;
  const environment=pathToFileURL(path.join(job.home,'resources/app.asar/renderer/assets',job.environmentModule)).href;
  const expression=`(async()=>{
    let adapter;const candidates=[globalThis.wb?.adapter,globalThis.wb?.provider];
    const queue=[],seen=new Set();
    for(const el of Array.from(document.querySelectorAll('body,#root,#app')).slice(0,20)){
      for(const k of Object.keys(el).filter(k=>k.startsWith('__reactContainer$')||k.startsWith('__reactFiber$'))){let f=el[k];while(f?.return)f=f.return;queue.push(f);}
    }
    while(queue.length&&seen.size<30000){const f=queue.pop();if(!f||seen.has(f))continue;seen.add(f);const p=f.memoizedProps;
      candidates.push(p?.value,p?.adapter,p?.provider,p?.value?.adapter);queue.push(f.child,f.sibling);
    }
    adapter=candidates.find(c=>c&&typeof c.getAccountUsage==='function'&&typeof c.getAccount==='function');
    if(!adapter)throw Error('Native account adapter not ready');
    const value=await adapter.getAccountUsage();if(!value)throw Error('Native account usage unavailable');
    const num=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0?v:null;
    const groups=(await import(${JSON.stringify(module)})).nt;
    const env=(await import(${JSON.stringify(environment)})).v();
    const labels={planBase:'套餐基础积分',planBonus:'套餐奖励积分',addon:'加量包积分',benefit:'活动积分'};
    const limits=typeof groups==='function'?groups(value.resources,env).map(g=>({label:labels[g.kind]||'账户积分',unit:'积分',remaining:num(g.left),total:num(g.total),used:num(g.used),reset_at:num(g.refreshAt??g.nearestExpireAt)})):[];
    if(!limits.length&&num(value.usageLeft)!==null)limits.push({label:'账户剩余积分',unit:'积分',remaining:num(value.usageLeft)});
    if(!limits.length)throw Error('Native account returned no quota numbers');
    return {limits};
  })()`;
  const response=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error('Native quota timeout')),20000);
    ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id!==1)return;clearTimeout(timer);m.error||m.result?.exceptionDetails?reject(Error('Native account query failed')):resolve(m.result?.result?.value);};
    ws.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression,awaitPromise:true,returnByValue:true}}));
  });
  if(!response?.limits?.length)throw Error('No native quota');output(response);
}catch{output({error:'WorkBuddy 原生额度桥接未就绪，请从额度连接入口重开客户端并确认已登录。'});process.exitCode=2;}
finally{ws?.close();}
