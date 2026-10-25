// Load the pinned client's own account helpers, with CLI startup removed.
// No prompt, agent loop, tools, telemetry startup, or credential output.
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const SHA='7bbf22027fe15097223a1ea8d9597e52f89a7ee57e485a7c6fee5fe3cbe965a2';
export async function load(url,context,nextLoad){
  if(url.startsWith('file:')&&path.resolve(fileURLToPath(url))===path.resolve(process.env.MODEL_DESK_QUOTA_CLI||'')){
    const data=await readFile(fileURLToPath(url));
    if(createHash('sha256').update(data).digest('hex')!==SHA)throw Error('Unverified Command Code quota adapter version');
    const source=data.toString('utf8'),marker='// src/index.ts\ndistrustCompromisedRoots();',cut=source.lastIndexOf(marker);
    if(cut<0||source.indexOf(marker)!==cut)throw Error('Command Code startup boundary changed');
    const entry=`
      const output = value => process.stdout.write('MODEL_DESK_QUOTA:'+JSON.stringify(value)+'\\n');
      try {
        const baseUrl=getApiBaseUrl();
        if(new URL(baseUrl).origin!=='https://api.commandcode.ai')throw Error('Non-official account endpoint');
        const request=createApiClient({baseUrl});
        const identity=await fetchUsageWhoami({request});
        if(!identity.data)throw Error('Account is not authenticated');
        const orgId=identity.data.org?.id??null;
        const [c,s]=await Promise.all([fetchUsageCredits({request,orgId}),fetchUsageSubscription({request,orgId})]);
        if(!c.data?.credits)throw Error('Account credits unavailable');
        const number=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0?v:null;
        const credits=c.data.credits;
        const plan=s.data?.data?.status==='active'?getPlanInfo({planId:s.data.data.planId??''}):null;
        const limits=[{label:'订阅积分',unit:'credits',remaining:number(credits.monthlyCredits),total:number(plan?.monthlyCredits),reset_at:s.data?.data?.currentPeriodEnd??null},
          {label:'额外购买积分',unit:'credits',remaining:number(credits.purchasedCredits)},
          {label:'赠送积分',unit:'credits',remaining:number(credits.freeCredits)}];
        for(const [key,label] of [['fiveHour','5 小时窗口'],['weekly','每周窗口']]){
          const v=c.data.windowLimits?.[key];
          if(v)limits.push({label,unit:'credits',used:number(v.used),total:number(v.cap),remaining:number(v.used)!==null&&number(v.cap)!==null?Math.max(0,v.cap-v.used):null,reset_at:v.resetAt??null});
        }
        output({limits});process.exit(0);
      }catch{output({error:'CommandCode 原生账户额度查询失败，请确认客户端已登录且当前账号可查看额度。'});process.exit(2);}
    `;
    return {format:'module',source:source.slice(0,cut)+entry,shortCircuit:true};
  }
  return nextLoad(url,context);
}
