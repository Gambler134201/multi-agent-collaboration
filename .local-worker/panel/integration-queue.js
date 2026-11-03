(function(global){
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const list=v=>Array.isArray(v)?v:[];
  const labels={queued:'等待集成',preparing:'准备集成副本',merging:'正在合并成果',checking:'正在联合检查',
    running:'正在集成',awaiting_review:'等待集成验收',accepted:'集成已验收',conflict:'集成存在冲突',
    check_failed:'集成检查未通过',blocked:'集成准备受阻',recovery_required:'集成结果待核对',cancelled:'集成已取消'};
  const sameProject=(a,b)=>{const norm=v=>{const s=String(v||'').replace(/\\/g,'/').replace(/\/+$/,'');return /^[a-z]:\//i.test(s)||s.startsWith('//')?s.toLowerCase():s;};return !!a&&!!b&&norm(a)===norm(b);};
  function detailMarkup(data){
    const r=data?.integration||{},job=r.queue||{},code=data?.state,known=labels[code];
    const block=(title,body)=>'<section class="iq-section"><h4>'+title+'</h4>'+body+'</section>';
    const p=v=>'<p>'+esc(v)+'</p>';
    const checks=list(r.checks),plan=list(job.check_commands);
    const commands=checks.map((c,i)=>'<details class="iq-check"><summary>检查 '+(i+1)+' · '+esc(c.ok===true?'通过':c.exit_code===null?'未取得完整结果':'未通过')+'</summary><pre>'+esc(JSON.stringify(c.command))+'</pre>'+p('退出码：'+(c.exit_code??'未知'))+'<pre>'+esc(c.output_tail||'未记录输出')+'</pre></details>').join('');
    const inputs=list(r.task_ids).map(id=>'<li><span>'+esc(id)+'</span><code>'+esc(r.commits?.[id]||'未取得冻结提交')+'</code></li>').join('');
    const conflicts=list(r.merges).flatMap(x=>list(x.conflicts));
    const reason=data.detail||job.detail||({awaiting_review:'合并与检查已结束，仍需主脑独立验收。',accepted:'集成已独立验收，原项目不会自动更新。',conflict:'合并存在冲突，成果仍在隔离副本中。',check_failed:'检查未通过，不能据此接受集成。'}[code])||'查看原记录后再决定下一步。';
    const recovery=data.recovery||{};
    const action=code==='queued'&&!job.requires_authority?'<button type="button" class="button secondary" data-iq-action="resume">继续原队列</button><button type="button" class="text-button" data-iq-action="cancel">取消排队</button>':
      (['preparing','merging','checking','recovery_required'].includes(code)||recovery.can_settle===true)&&!job.requires_authority?'<button type="button" class="button secondary" data-iq-action="reconcile">核对原作业（不重跑）</button>':'';
    return '<h3>'+esc(r.name||'集成详情')+'</h3><p class="iq-status">'+esc(known||'状态待核对')+'</p>'+p(reason)+
      '<div class="iq-actions">'+action+'</div>'+(recovery.detail?block('收尾与占用',p(recovery.detail)):'')+
      (job.requires_authority?p('继续需原主脑重连并明确续接原入队请求；收尾核对可由原主脑调用 brain_reconcile_integration，普通面板不接管归属。'):'')+
      block('输入成果',inputs?'<ul class="iq-inputs">'+inputs+'</ul>':p('旧记录未提供任务清单。'))+
      block('联合检查',p(checks.length+' 项已记录结果'+(plan.length?' / '+plan.length+' 项计划':''))+commands+(!checks.length?p('尚无检查结果，不表示通过。'):'')+(plan.length?'<details><summary>查看原检查计划</summary><pre>'+esc(JSON.stringify(plan,null,2))+'</pre></details>':''))+
      (conflicts.length?block('冲突文件','<ul>'+conflicts.map(c=>'<li>'+esc(c)+'</li>').join('')+'</ul>'):'')+
      block('成果位置与基线',p('原项目没有被自动覆盖。')+p('基线：'+(r.baseline||'未记录'))+p('集成提交：'+(r.integrated_commit||'尚未生成或未核实'))+p('保留副本：'+(r.integration_copy?.path||'未记录')))+
      block('独立验收',p(code==='accepted'?'已有独立验收记录；后续采用仍按实际冻结基线核对。':'尚未接受该集成。执行检查通过也不会自动验收。'));
  }
  let singleton;
  function create({api,getView,getData,toast=()=>{}}){
    if(singleton)return singleton;
    const doc=global.document;if(!doc?.body)return null;
    const dialog=doc.createElement('dialog');dialog.className='iq-dialog';dialog.id='integration-queue-dialog';dialog.setAttribute('aria-labelledby','iq-title');
    dialog.innerHTML='<div class="dialog-heading"><div><span class="eyebrow">已验收成果 · 受控集成</span><h2 id="iq-title">集成队列</h2></div><button type="button" class="icon-button iq-close" aria-label="关闭集成队列">×</button></div><p class="iq-scope"></p><p class="iq-error" role="alert" hidden></p><div class="iq-layout"><div class="iq-list" aria-label="当前项目集成作业"></div><aside class="iq-detail" aria-label="集成作业详情" aria-live="polite"></aside></div><p class="iq-footnote">由主脑通过正式集成入口或控制器提交已批准计划。查看不派发模型、不启动集成；未知结果不自动重跑。</p>';
    doc.body.appendChild(dialog);
    const find=s=>dialog.querySelector(s);
    let selected='',project='',epoch=0,version='',loadedVersion='',focusBefore=null,loading='',signature='';
    function error(message){find('.iq-error').textContent=message||'';find('.iq-error').hidden=!message;}
    async function load(){
      if(!selected||!dialog.open)return;
      const name=selected,bound=project,key=name+'|'+version;if(loading===key)return;
      const generation=++epoch;loading=key;
      find('.iq-detail').textContent='正在读取原集成记录…';
      try{
        const data=await api('/api/integrations/'+encodeURIComponent(name));
        if(generation!==epoch||!dialog.open||selected!==name||!sameProject(getView()?.project,bound))return;
        if(!data?.read_only||!data.integration||data.integration.name!==name||!sameProject(data.integration.project,bound))throw Error('集成详情的项目或作业标识不匹配');
        find('.iq-detail').innerHTML=detailMarkup(data);loadedVersion=key;
      }catch(e){if(generation!==epoch||!dialog.open)return;find('.iq-detail').textContent='集成详情暂时无法确认：'+e.message;loadedVersion=key;}
      finally{if(generation===epoch)loading='';}
    }
    function render(){
      if(!dialog.open)return;
      const view=getView();if(view?.mode!=='project'||!sameProject(view.project,project)){singleton.onProjectChange();return;}
      const data=getData()||{},rows=list(data.records);
      error(data.available===true?'':data.error||'服务未提供可确认的集成队列，请核对连接。');
      find('.iq-scope').textContent=project;
      if(!selected&&!loading){selected=rows[0]?.name||'';loadedVersion='';}
      const sig=JSON.stringify([rows,selected]);
      if(signature!==sig){
        find('.iq-list').innerHTML=rows.length?rows.map(r=>'<button type="button" class="iq-row" data-iq-name="'+esc(r.name)+'" aria-pressed="'+(r.name===selected)+'"><span>'+esc(labels[r.state]||'状态待核对')+(r.recovery?.can_settle===true?' · 收尾待核对':'')+'</span><strong>'+esc(list(r.task_ids).length+' 项成果的集成')+'</strong><small>'+esc(r.name)+'<br>'+esc(r.updated_at||'更新时间未知')+'</small></button>').join(''):'<div class="iq-empty">'+(data.available===true?'当前项目没有集成记录。已有任务不等于已提交集成。':'集成记录暂时无法确认，不当作空队列。')+'</div>';
        signature=sig;
      }
      const row=rows.find(r=>r.name===selected);version=row?JSON.stringify([row.updated_at,row.state,row.check_count,row.recovery]):'';
      if(selected&&loadedVersion!==selected+'|'+version)load();
      if(!selected)find('.iq-detail').textContent='提交集成计划后，在这里查看输入、检查结果和下一步。';
    }
    async function action(button){
      if(!selected||!dialog.open)return;
      const name=selected,kind=button.dataset.iqAction,bound=project;
      const messages={resume:'继续这个尚未开始的原集成作业？不会创建新作业或调用模型。',cancel:'取消尚未开始的排队集成？原任务成果保留。',reconcile:'核对原作业。只有完整结束回执和原进程退出均有证据时，才释放它自己的占用；其余保留现场。不重新合并、不运行检查、不自动验收。'};
      if(!global.confirm(messages[kind]))return;
      button.disabled=true;
      try{
        await api('/api/integrations/'+encodeURIComponent(name)+'/'+kind,{});
        if(!dialog.open||!sameProject(getView()?.project,bound))return;
        loadedVersion='';await load();toast('已处理原作业，请以最新记录为准。');
      }catch(e){if(dialog.open&&sameProject(getView()?.project,bound))error(e.message);}
      finally{if(button.isConnected)button.disabled=false;}
    }
    dialog.addEventListener('click',event=>{
      if(event.target.closest('.iq-close')){dialog.close();return;}
      const row=event.target.closest('[data-iq-name]');
      if(row){selected=row.dataset.iqName;loadedVersion='';render();return;}
      const button=event.target.closest('[data-iq-action]');if(button)action(button);
    });
    dialog.addEventListener('close',()=>{epoch++;loading='';signature='';try{if(focusBefore?.isConnected)focusBefore.focus();}catch{}});
    singleton={
      open({name}={}){
        const view=getView();if(view?.mode!=='project'||!view.project){toast('请先选择明确项目再查看集成队列。',true);return false;}
        if(!dialog.open){project=view.project;focusBefore=doc.activeElement;dialog.showModal();}
        if(name)selected=name;loadedVersion='';signature='';render();return true;
      },
      update:render,
      onProjectChange(){epoch++;loading='';selected='';project='';loadedVersion='';signature='';if(dialog.open)dialog.close();}
    };
    return singleton;
  }
  const exported={create,labels,_test:{detailMarkup,sameProject,esc}};
  global.ModelDeskIntegrationQueue=exported;
  if(typeof module!=='undefined'&&module.exports)module.exports=exported;
})(typeof window!=='undefined'?window:globalThis);
