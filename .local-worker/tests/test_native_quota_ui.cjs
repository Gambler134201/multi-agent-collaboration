const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../panel/native-quota.js'),'utf8');
const context=vm.createContext({URL,Date,Intl,fetch(){throw Error('Rendering must not request data');}});
vm.runInContext(source,context);const view=context.ModelDeskNativeQuota;
for(const id of ['workbuddy','zcode','opencode','commandcode']){
  const endpoint={id,name:id,quota:{capability:'native',can_refresh:true,status:'ready',fetched_at:1792934000,
    source_label:'Native account',sync:{enabled:true,interval_seconds:180},limits:[{label:'5 小时',remaining:0,total:10,unit:'credits'}]}};
  let html=view.render(endpoint);assert.match(html,/value="0"/);assert.match(html,/已同步/);assert.match(html,new RegExp('data-quota-refresh="'+id+'"'));
  const settings=view.renderSettings(endpoint);assert.match(settings,new RegExp('data-quota-auto="'+id+'"'));assert.match(settings,/180 秒/);
  endpoint.quota.status='error';assert.match(view.render(endpoint),/同步失败/);
  endpoint.quota.status='stale';assert.match(view.render(endpoint),/已过期/);
}
for(const value of [null,true,false,'',NaN,Infinity]){
  const normalized=view.normalize({id:'zcode',quota:{limits:[{remaining:value,total:100}]}});
  assert.equal(normalized.quota.limits[0].percent,null);
}
let endpoint={id:'zcode',quota:{capability:'native',can_refresh:true,status:'never',limits:[{label:'<img src=x onerror=alert(1)>',remaining:10,total:0}],source_url:'https://opencode.ai.evil.invalid/?secret=1'}};
let html=view.render(endpoint);assert.doesNotMatch(html,/<img|href=/);assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<progress[^>]+value="100"/);
assert.doesNotMatch(source,/\bfetch\s*\(|XMLHttpRequest|localStorage|setInterval\s*\(|setTimeout\s*\(/);
console.log('ok - native quota: four endpoints, zero vs unknown, stale/error, XSS/URL safety, pure rendering, setting hooks');
