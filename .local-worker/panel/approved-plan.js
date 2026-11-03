(function(global){
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels={approved:'已批准',paused:'已暂停',revoked:'已撤销',completed:'阶段全部验收通过',needs_approval:'计划变化，需重新批准'};
  const mergeLabels={reserved:'已保存原意图',unknown:'结果未知',queued:'等待集成',preparing:'准备集成副本',merging:'合并成果中',checking:'联合检查中',awaiting_review:'等待独立验收',accepted:'集成已验收',conflict:'合并冲突',check_failed:'联合检查失败',blocked:'需要主脑处理',recovery_required:'需要核对收尾',cancelled:'已取消'};
  const observations={accepted:'已独立验收',queued:'原任务已排队',running:'原任务正在执行',awaiting_review:'等待独立验收',original_attempt_only:'保留原任务，不重复派发',manual_attention:'需要主脑处理',waiting_prerequisites:'等待前置验收',waiting_handover:'等待有效的已验收成果交接',waiting_integration:'等待包含前置成果的已验收集成基线',waiting_handover_or_integration:'等待有效交接或已验收集成',created:'已派发原任务',unknown:'派发结果未知，禁止重发',not_submitted:'已拒绝派发，未领取任务',integration_running:'正在排队或执行联合检查',integration_awaiting_review:'联合检查通过，等待主脑独立验收',integration_accepted:'集成已验收，等待采用基线',integration_unknown:'原集成结果未知，不重建作业',integration_needs_attention:'集成需要主脑处理，不自动重跑'};
  function create({api,getView,toast}){
    const dialog=document.createElement('dialog');dialog.id='approved-plan-dialog';
    dialog.innerHTML='<div class="records-panel"><div class="dialog-heading"><h2>批准计划与自动推进</h2><button type="button" class="icon-button" data-plan-close aria-label="关闭">×</button></div><div class="records-scroll" data-plan-body></div><div class="dialog-footer" data-plan-actions></div></div>';
    document.body.append(dialog);
    let generation=0,goal=null,bound=null,stored=null,preview=null,busy=false,checksDirty=false;
    const body=dialog.querySelector('[data-plan-body]'),actions=dialog.querySelector('[data-plan-actions]');
    const current=()=>getView()?.mode==='project'&&getView().project===bound;
    const call=(action,payload)=>api('/api/goals/'+encodeURIComponent(goal)+'/plan/'+action,payload);
    function render(error=''){
      const p=stored?.plan,doc=preview?.document||p?.document,brain=doc?.definition?.responsibility?.mode==='brain';
      body.innerHTML='<p class="field-hint">'+esc(bound)+'</p><p>'+esc(p?labels[p.status]||'状态待核对':'尚未批准')+(p?' · 版本 '+esc(p.revision):'')+'</p>'+
        (p?.last_problem?'<p class="form-error">'+esc(p.last_problem)+'</p>':'')+
        (brain?'<p class="field-hint">责任主脑：'+esc(doc.definition.responsibility.host)+' / '+esc(doc.definition.responsibility.owner)+'。来自受信启动声明，批准后需由这个原会话调用 brain_plan resume；普通面板不代派。</p>':'')+
        (doc?.context?.policy?'<p class="field-hint">调用边界：仅已列模型 · 最大并发 '+esc(doc.context.policy.max_parallel_tasks??'按现有设置')+' · 金额硬上限 '+esc(doc.context.policy.enforce_paid_budget===true?'开启':'关闭')+' · 次数硬上限 '+esc(doc.context.policy.enforce_model_call_limit===true?'开启':'关闭')+'。执行前仍检查当前开关、资源和授权。</p>':'')+
        (p&&preview&&p.digest!==preview.digest?'<p class="form-error">当前预览已变化，旧批准不覆盖这些内容；需要重新批准新版本。</p>':'')+
        (p&&!stored.attached&&p.status==='approved'?'<p class="form-error">当前进程未附着此计划；需明确续接，读取不会自动重发。</p>':'')+
        (doc?'<h3>'+esc(doc.definition.goal.title)+'</h3><p>'+esc(doc.definition.goal.objective)+'</p>'+doc.definition.stages.map(s=>{const c=doc.context.models[s.id];return '<section class="plan-stage" data-plan-stage><strong>'+esc(s.title)+'</strong><p>'+esc(s.objective)+'</p><p>'+esc(c.engine)+' · '+esc(c.model_name)+' · '+esc(c.access_mode==='full'?'完全访问（保留敏感位置保护）':'项目范围')+'</p><p>前置：'+esc(s.depends_on.map(id=>doc.definition.stages.find(x=>x.id===id)?.title||id).join('、')||'无，可与其他独立阶段并行')+'</p><ul>'+s.acceptance.map(t=>'<li>'+esc(t)+'</li>').join('')+'</ul><p>'+esc(observations[p?.observations?.[s.id]]||'等待计划推进')+'</p></section>';}).join(''):'')+
        (doc?'<section class="plan-stage"><h3>前置分支联合检查</h3><p>独立分支需要合并时，在隔离副本运行下列检查；检查通过仍等待主脑独立验收，下游才会继续。不会合并到 main。</p><p>适用阶段：'+esc(doc.definition.stages.filter(s=>s.depends_on.length>1).map(s=>s.title).join('、')||'当前无多前置汇合阶段')+'</p><pre>'+esc(JSON.stringify(doc.integration_checks||[],null,2))+'</pre><p class="field-hint">空列表表示不自动集成。不包含项目末尾独立分支的自动最终合并。</p><details><summary>调整联合检查（高级）</summary><label>命令参数数组（JSON，不经 Shell）<textarea data-plan-checks rows="5">'+esc(JSON.stringify(doc.integration_checks||[],null,2))+'</textarea></label><button type="button" class="button secondary" data-plan-action="preview-checks">更新预览，重新确认</button></details></section>':'')+
        (p?.integrations&&Object.keys(p.integrations).length?'<section class="plan-stage"><h3>原集成请求</h3>'+Object.entries(p.integrations).map(([sid,i])=>'<p>'+esc(doc.definition.stages.find(s=>s.id===sid)?.title||sid)+' · '+esc(i.name)+' · '+esc(mergeLabels[i.state]||'状态待核对')+'<br>原请求：'+esc(i.request_id)+'</p>').join('')+'</section>':'')+
        '<p class="field-hint">只推进固定模型的新会话阶段；执行结束仍需主脑独立验收和有效交接。未知请求保留原意图；暂停/撤销不取消已运行任务。不会部署、推送或更新 main。</p>'+
        (error?'<p class="form-error" role="alert">'+esc(error)+'</p>':'')+
        (preview?'<label><input type="checkbox" data-plan-consent> 我确认以上阶段、模型、访问权限和交付边界，允许按此版本推进。</label>':'');
      actions.innerHTML=(preview?'<button type="button" class="button" data-plan-action="approve">'+(brain?'批准，等待原主脑续接':'批准并启动')+'</button>':'')+
        (p&&['approved','paused'].includes(p.status)&&(!brain||p.status==='paused')?'<button type="button" class="button secondary" data-plan-action="resume">'+(brain?'解除暂停，等待原主脑续接':'续接原计划')+'</button>':'')+
        (p&&p.status==='approved'?'<button type="button" class="button secondary" data-plan-action="pause">暂停</button>':'')+
        (p&&!['revoked','completed'].includes(p.status)?'<button type="button" class="button secondary" data-plan-action="revoke">撤销批准</button>':'')+
        '<button type="button" class="button secondary" data-plan-action="reload">重新读取</button>';
      dialog.querySelectorAll('[data-plan-action]').forEach(b=>b.disabled=busy||(checksDirty&&['approve','resume'].includes(b.dataset.planAction))||(b.dataset.planAction==='resume'&&preview&&p&&preview.digest!==p.digest));
    }
    async function load(){
      const epoch=++generation;busy=true;preview=null;checksDirty=false;render();
      try{
        const data=await call('view',{});if(epoch!==generation||!current())return;stored=data;
        try{const next=await call('preview',{});if(epoch!==generation||!current())return;preview=next;}
        catch(e){if(epoch===generation&&current()){busy=false;render(e.message);return;}}
        if(epoch===generation&&current()){busy=false;render();}
      }catch(e){if(epoch===generation&&current()){busy=false;render(e.message);}}
    }
    body.addEventListener('input',event=>{
      if(!event.target.matches('[data-plan-checks]'))return;
      checksDirty=true;
      const consent=body.querySelector('[data-plan-consent]');if(consent)consent.checked=false;
      actions.querySelectorAll('[data-plan-action="approve"],[data-plan-action="resume"]').forEach(b=>b.disabled=true);
    });
    dialog.addEventListener('click',async event=>{
      if(event.target.closest('[data-plan-close]')){generation++;dialog.close();return;}
      const button=event.target.closest('[data-plan-action]');if(!button||busy||!current())return;
      const action=button.dataset.planAction;
      if(action==='reload'){await load();return;}
      if(action==='preview-checks'){
        const epoch=generation;
        try{
          const checks=JSON.parse(body.querySelector('[data-plan-checks]').value);
          busy=true;render();const next=await call('preview',{checks});
          if(epoch!==generation||!current())return;
          preview=next;checksDirty=false;busy=false;render();
        }catch(e){if(epoch===generation&&current()){busy=false;render(e.message);}}
        return;
      }
      if(checksDirty&&['approve','resume'].includes(action)){toast('请先更新联合检查预览',true);return;}
      if(['approve','resume'].includes(action)&&!body.querySelector('[data-plan-consent]')?.checked){toast('请先确认计划与授权边界',true);return;}
      const original=stored?.plan,d=action==='approve'?preview?.digest:original?.digest;
      const epoch=generation;busy=true;render();
      try{
        await call(action,{digest:d,checks:action==='approve'?preview.document.integration_checks:original?.document?.integration_checks||[],confirm:true});
        if(epoch!==generation||!current())return;
        toast('计划操作已记录；不自动验收或重发未知任务');await load();
      }catch(e){if(epoch===generation&&current()){busy=false;render(e.message);}}
    });
    dialog.addEventListener('close',()=>{generation++;});
    return {open(id){if(getView()?.mode!=='project'){toast('请先选择明确项目',true);return;}goal=id;bound=getView().project;stored=null;preview=null;dialog.showModal();load();},
      scopeChanged(){if(dialog.open&&!current()){generation++;dialog.close();}}};
  }
  global.ModelDeskApprovedPlan={create};
})(window);
