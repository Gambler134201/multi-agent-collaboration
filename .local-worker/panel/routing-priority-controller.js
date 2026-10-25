/* Explicit draft/save only. Shared by browser, Codex and DSH. */
(()=>{
  let root,installed=false,data,baseline,draft,lastMarkup='',known=[];
  const pending=new Map();
  const clone=value=>JSON.parse(JSON.stringify(value));
  const policy=value=>({enabled:value?.enabled===true,slots:clone(value?.slots||[])});
  const equal=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
  function reset(){baseline=policy(data.policy);draft={...clone(baseline),dirty:false,busy:false,error:'',notice:'',conflict:false};pending.clear();}
  function paint(focus){
    if(!root||!draft||!globalThis.ModelDeskPriorityView)return;
    data.models=[...known,...[...pending.values()].map(p=>p.model)];
    const html=ModelDeskPriorityView.render(data,draft);
    if(html!==lastMarkup){root.innerHTML=html;lastMarkup=html;}
    if(focus)root.querySelector(focus)?.focus();
  }
  function render(current,models){
    if(!current||!globalThis.ModelDeskPriorityView)return;
    setup();known=(models||[]).map(m=>({id:m.id,name:m.name,model:m.model,enabled:m.enabled,default_agent:m.default_agent,
      thinking_level:m.native_reasoning?.value||m.native_selection?.options?.reasoningLevel||m.thinking_level||'default'}));data={policy:current,models:known};
    if(!draft||!draft.dirty&&!draft.busy)reset();
    else if(!draft.busy&&!equal(baseline,policy(current)))draft.conflict=true;
    paint();
  }
  function changed(focus){for(const id of pending.keys())if(!draft.slots.some(s=>s.model_id===id))pending.delete(id);draft.dirty=!equal(baseline,policy(draft));draft.error='';draft.notice='';paint(focus);}
  function changeModel(index,value){
    const old=clone(draft.slots[index]);if(!old)return;
    if(value){
      const other=draft.slots.findIndex((s,i)=>i!==index&&s.model_id===value);
      if(other>=0){draft.slots[index]=draft.slots[other];draft.slots[other]=old;draft.notice='已交换两个模型的顺位；点击保存后生效。';draft.dirty=!equal(baseline,policy(draft));draft.error='';paint(`[data-priority-select="${index}"]`);return;}
      const m=data.models.find(m=>m.id===value);if(!m)return;
      draft.slots[index]={label:m.name||m.model||m.id,model_id:value,unavailable_reason:''};
    }else if(old.model_id!==null)draft.slots[index]={label:old.label,model_id:null,unavailable_reason:'用户保留空位，自动任务跳过此顺位'};
    changed(`[data-priority-select="${index}"]`);
  }
  async function save(){
    if(!draft.dirty||draft.busy||draft.conflict||data.policy.readable===false)return;
    if(draft.enabled&&!draft.slots.length){draft.error='启用指定顺序时请至少保留一个顺位，或先关闭指定顺序。';paint();return;}
    draft.busy=true;draft.error='';paint();
    try{
      const selections=[...pending.values()].filter(p=>draft.slots.some(s=>s.model_id===p.model.id)).map(p=>p.selection);
      const result=await api('/api/workflow',{routing_priority:{...policy(draft),expected:clone(baseline),...(selections.length?{selections}:{})}});
      if(result.ok===false)throw new Error(result.error||'排序未保存');
      if(result.saved_models)known=[...known,...result.saved_models.map(m=>({...m,enabled:true,thinking_level:m.native_reasoning?.value||m.native_selection?.options?.reasoningLevel||m.thinking_level||'default'}))];
      data.policy=result;reset();draft.notice='模型使用顺序已保存，仅影响后续自动任务。';paint();toast(draft.notice);await refresh();
    }catch(error){draft.error=error.message;if(/模型使用顺序已经变化|原始快照/.test(error.message))draft.conflict=true;}
    finally{draft.busy=false;paint();}
  }
  function setup(){
    if(installed)return;installed=true;
    root=document.createElement('section');root.id='routing-priority-editor';root.setAttribute('aria-label','模型使用顺序设置');
    document.getElementById('models').before(root);
    root.addEventListener('change',event=>{
      if(!draft||draft.busy||data.policy.readable===false)return;
      const el=event.target;
      if(el.hasAttribute('data-priority-enabled')){draft.enabled=el.checked;changed('[data-priority-enabled]');}
      if(el.hasAttribute('data-priority-select'))changeModel(Number(el.dataset.prioritySelect),el.value);
    });
    root.addEventListener('click',event=>{
      const b=event.target.closest('button');if(!b||b.disabled||!draft||draft.busy)return;
      if(b.hasAttribute('data-priority-save'))return save();
      if(b.hasAttribute('data-priority-reset')){reset();paint('[data-priority-enabled]');return;}
      if(data.policy.readable===false||draft.conflict)return;
      if(b.hasAttribute('data-priority-pick')){
        const index=Number(b.dataset.priorityPick),slot=draft.slots[index];if(!slot)return;
        const stamp=JSON.stringify(slot),expected=JSON.stringify(baseline),model=data.models.find(m=>m.id===slot.model_id),entry=pending.get(slot.model_id);
        const initial=entry?{endpoint:entry.selection.endpoint,choice_id:entry.selection.choice_id,reasoning:entry.selection.reasoning}:
          {endpoint:model?(model.default_agent==='follow_global'?state.workflow?.execution_agent:model.default_agent):'',model_id:slot.model_id,reasoning:model?.thinking_level||'default'};
        ModelDeskModelPicker.openForPriority(initial,b,result=>{
          if(draft.busy||draft.conflict||JSON.stringify(baseline)!==expected||JSON.stringify(draft.slots[index])!==stamp)throw new Error('排序草稿已经变化，请关闭窗口后重新选择该顺位。');
          if(result.selection){
            const existing=[...pending.values()].find(p=>['endpoint','choice_id','reasoning','revision'].every(k=>p.selection[k]===result.selection[k]));
            if(existing)result=existing;else pending.set(result.model.id,result);
          }
          if(!result.selection&&!known.some(m=>m.id===result.model.id))known.push(result.model);
          data.models=[...known,...[...pending.values()].map(p=>p.model)];changeModel(index,result.model.id);
        },index+1);return;
      }
      if(b.hasAttribute('data-priority-add')&&draft.slots.length<20){draft.slots.push({label:'第 '+(draft.slots.length+1)+' 顺位',model_id:null,unavailable_reason:'用户保留空位，自动任务跳过此顺位'});changed(`[data-priority-select="${draft.slots.length-1}"]`);return;}
      for(const action of ['up','down','remove']){
        const key='priority'+action[0].toUpperCase()+action.slice(1);if(!(key in b.dataset))continue;
        const index=Number(b.dataset[key]);if(!Number.isInteger(index)||!draft.slots[index])return;
        let next=index;
        if(action==='remove'){draft.slots.splice(index,1);next=Math.max(0,index-1);}
        else{next=index+(action==='up'?-1:1);if(next<0||next>=draft.slots.length)return;[draft.slots[index],draft.slots[next]]=[draft.slots[next],draft.slots[index]];}
        changed(`[data-priority-select="${next}"]`);return;
      }
    });
  }
  globalThis.ModelDeskPriorityController={setup,render};
})();
