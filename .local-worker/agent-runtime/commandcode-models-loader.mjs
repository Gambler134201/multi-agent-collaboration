// Read the installed client's own catalog without starting CLI/agent execution.
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const SHA='7bbf22027fe15097223a1ea8d9597e52f89a7ee57e485a7c6fee5fe3cbe965a2';
export async function load(url,context,nextLoad){
 if(url.startsWith('file:')&&path.resolve(fileURLToPath(url))===path.resolve(process.env.MODEL_DESK_CATALOG_CLI||'')){
  const data=await readFile(fileURLToPath(url));
  if(createHash('sha256').update(data).digest('hex')!==SHA)throw Error('CommandCode catalog version changed');
  const source=data.toString('utf8'),marker='// src/index.ts\ndistrustCompromisedRoots();',cut=source.lastIndexOf(marker);
  if(cut<0||source.indexOf(marker)!==cut)throw Error('Native CLI startup boundary changed');
  return {format:'module',shortCircuit:true,source:source.slice(0,cut)+`
    try {
      const effective=getCurrentEffectiveCatalog();
      const models=effective.catalog.models.filter(m=>!m.hidden).map(m=>{
        const status=resolveCatalogModelStatus({effective,modelId:m.id});
        return {model:m.id,name:m.name,reasoning:Array.isArray(m.reasoningEfforts)?m.reasoningEfforts:[],
          selectable:status.state==='selectable',unavailable_reason:status.state==='selectable'?'':'当前客户端需升级或目录不支持',
          contextWindow:m.contextWindow,maxOutputTokens:m.maxOutputTokens,minimumPlan:m.minimumPlan??null};
      });
      process.stdout.write('MODEL_DESK_CATALOG:'+JSON.stringify({models,source:effective.source})+'\\n');process.exit(0);
    }catch{process.stdout.write('MODEL_DESK_CATALOG:'+JSON.stringify({error:'原生模型目录读取失败'})+'\\n');process.exit(2);}
  `};
 }
 return nextLoad(url,context);
}
