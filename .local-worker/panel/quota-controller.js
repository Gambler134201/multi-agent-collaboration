/* Shared native-account controls. No provider calls during view rendering. */
(()=>{
  const ids=['workbuddy','zcode','opencode','commandcode'],busy=new Set(),saving=new Set(),drafts=new Map();
  let installed=false,autoRunning=false;
  const row=id=>state.execution_endpoints?.rows?.find(e=>e.id===id);
  const escaped=v=>escapeText(String(v??''));
  function wallet(endpoint){
    return ModelDeskNativeQuota.render(endpoint,{busy:busy.has(endpoint.id)||saving.has(endpoint.id)})+`<button type="button" class="text-button native-quota-open" data-quota-open="${escaped(endpoint.id)}">额度与同步设置</button>`;
  }
  function settings(endpoint){
    if(!ids.includes(endpoint.id)||!globalThis.ModelDeskNativeQuota)return '';
    const q=endpoint.quota||{},s={...q.sync,...drafts.get(endpoint.id)},view={...endpoint,quota:{...q,sync:s}};
    let connection='';
    if(endpoint.id==='opencode')connection=`<label>额度来源账户<select data-quota-model="opencode"><option value="">自动匹配唯一 Go 账户</option>${(q.account_models||[]).map(m=>`<option value="${escaped(m.id)}" ${s.model_id===m.id?'selected':''}>${escaped(m.name)}</option>`).join('')}</select></label><p class="field-hint">只复用已有 OpenCode Go 模型的账户连接；密钥不会传到页面。免费模型和 Go 额度分开显示。</p>`;
    if(endpoint.id==='workbuddy')connection=`<label>本机额度桥接端口<input type="number" min="1024" max="65535" value="${Number(s.port)||19766}" data-quota-port="workbuddy"></label><button type="button" class="button secondary" data-quota-connect="workbuddy">连接 WorkBuddy 原生额度</button><p class="field-hint">仅连接本机客户端。若客户端已运行且没有桥接，请保存工作并退出后再连接；不会自动关闭窗口。</p>`;
    if(endpoint.id==='zcode')connection=`<label>额度账户渠道<select data-quota-provider="zcode"><option value="account:zai-start-plan" ${s.provider==='account:zai-start-plan'?'selected':''}>Z.ai Start Plan</option><option value="account:bigmodel-start-plan" ${s.provider==='account:bigmodel-start-plan'?'selected':''}>智谱 Start Plan</option></select></label><button type="button" class="button secondary" data-quota-connect="zcode">连接 ZCode 原生额度</button><p class="field-hint">使用下方已有的 ZCode 安装目录与桥接端口；已选择模型时优先查询该模型所属账户。</p>`;
    return `<div class="native-quota-config">${ModelDeskNativeQuota.renderSettings(view,{busy:busy.has(endpoint.id)||saving.has(endpoint.id)})}<div class="native-quota-connection">${connection}</div><p class="native-quota-inline-error" data-quota-error="${escaped(endpoint.id)}" role="status"></p></div>`;
  }
  function showMessage(id,message,error=false){
    const target=document.querySelector(`[data-quota-error="${id}"]`);
    if(target)target.textContent=message;
    if(error||message)toast(message,error);
  }
  async function update(id,values,automatic=false){
    if(!ids.includes(id)||busy.has(id)||saving.has(id))return;
    const changing=!!values.quota_sync;
    (changing?saving:busy).add(id);
    if(changing)drafts.set(id,values.quota_sync);
    renderModels();
    try{
      const result=await api('/api/workflow',{endpoint_settings:{id,values}});
      if(result?.ok===false&&!result?.busy&&!result?.cooldown){
        if(!automatic)showMessage(id,result.message||result.quota?.error||'额度查询未完成，请检查登录与连接。',true);
      }else if(!automatic)showMessage(id,result.message||(changing?'额度同步设置已保存。':'额度已同步。'));
    }catch(error){if(!automatic)showMessage(id,error.message,true);}
    finally{busy.delete(id);saving.delete(id);drafts.delete(id);await refresh();}
  }
  function setup(){
    if(installed)return;installed=true;
    document.addEventListener('click',event=>{
      const button=event.target.closest('[data-quota-refresh],[data-quota-connect],[data-quota-open]');
      if(!button||button.disabled)return;
      if(button.dataset.quotaOpen){openEndpointDialog(button.dataset.quotaOpen);setEndpointTab('models');return;}
      if(button.dataset.quotaRefresh)update(button.dataset.quotaRefresh,{refresh_quota:true});
      else update(button.dataset.quotaConnect,{connect_quota:true});
    });
    document.addEventListener('input',event=>{
      const id=event.target.dataset.quotaPort;if(id)drafts.set(id,{...row(id)?.quota?.sync,port:Number(event.target.value)});
    });
    document.addEventListener('change',event=>{
      const e=event.target,id=e.dataset.quotaAuto||e.dataset.quotaInterval||e.dataset.quotaModel||e.dataset.quotaPort||e.dataset.quotaProvider;
      if(!id)return;
      const s={...row(id)?.quota?.sync,...drafts.get(id)};
      if(e.dataset.quotaAuto)s.enabled=e.checked;
      if(e.dataset.quotaInterval)s.interval_seconds=Number(e.value);
      if(e.dataset.quotaModel)s.model_id=e.value;
      if(e.dataset.quotaPort)s.port=Number(e.value);
      if(e.dataset.quotaProvider)s.provider=e.value;
      update(id,{quota_sync:s});
    });
  }
  async function maybeSync(){
    if(!panelVisible()||autoRunning)return;
    autoRunning=true;
    try{
    for(const id of ids){
      const q=row(id)?.quota,s=q?.sync;
      if(!s?.enabled||!q.can_refresh||busy.has(id)||saving.has(id))continue;
      const delay=q.status==='error'?Math.max(180,s.interval_seconds*2):s.interval_seconds;
      if(Date.now()/1000-(q.last_attempt_at||0)<delay)continue;
      // Sequential reads; the backend independently enforces settings and due time.
      await update(id,{refresh_quota:'auto'},true);
      if(!panelVisible())break;
    }
    }finally{autoRunning=false;}
  }
  globalThis.ModelDeskQuotaController={wallet,settings,setup,maybeSync,isBusy:id=>busy.has(id)||saving.has(id)};
})();
