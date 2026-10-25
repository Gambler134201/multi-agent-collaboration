/* Shared by browser, Codex and DSH. Rendering and searching never query providers. */
(()=>{
  let dialog,installed=false,version=0,anchor,data,draft,context=null,choiceSequence=0;
  const clients=[{id:'dsh',label:'DeepSeek Harness'},{id:'pi',label:'Pi Agent'},{id:'workbuddy',label:'WorkBuddy'},{id:'zcode',label:'ZCode'},{id:'opencode',label:'OpenCode'},{id:'commandcode',label:'CommandCode'}];
  const request=op=>api('/api/workflow',{endpoint_settings:{id:data.endpoint,values:{model_picker:op}}});
  function paint(focus){
    const active=document.activeElement,queryFocus=active?.hasAttribute('data-picker-query');
    const cursor=queryFocus?active.selectionStart:null;
    const scroll=dialog.querySelector('.model-picker__list')?.scrollTop||0;
    dialog.innerHTML=ModelDeskModelPickerView.render(context?{...data,priority_context:{slot:context.slot,clients}}:data,{...draft,client_id:data.endpoint});
    const list=dialog.querySelector('.model-picker__list');if(list)list.scrollTop=scroll;
    if(focus||queryFocus){const input=dialog.querySelector('[data-picker-query]');input.focus();if(cursor!==null)input.setSelectionRange(cursor,cursor);}
  }
  function close(){version++;dialog.close();(anchor?.isConnected?anchor:context?document.querySelector(`[data-priority-pick="${context.slot-1}"]`):document.querySelector(`[data-open-model-picker="${data.endpoint}"]`))?.focus();}
  async function operation(op){
    if(draft.busy||draft.loading)return;
    const ticket=version;draft.error='';draft.loading=op.action!=='select';draft.busy=op.action==='select';paint();
    try{
      const result=await request(op);if(ticket!==version)return;
      if(result.ok===false)throw new Error(result.message||'操作未完成');
      if(result.catalog){data=result.catalog;
        if(!data.models.some(m=>m.id===draft.selected_id)){draft.selected_id=data.current_model;draft.reasoning=data.current_reasoning||'default';}
        const selected=data.models.find(m=>m.id===draft.selected_id);
        if(selected&&!selected.reasoning?.some(r=>r.value===draft.reasoning))draft.reasoning='default';
      }
      if(op.action==='select'){close();toast(result.message||'模型已保存');await refresh();return;}
    }catch(error){if(ticket===version)draft.error=error.message;}
    finally{if(ticket===version){draft.loading=false;draft.busy=false;paint();}}
  }
  function source(){
    const selected=data.models.find(m=>m.id===draft.selected_id),current=data.models.find(m=>m.id===data.current_model);
    return data.sources.find(s=>s.id===draft.source_id&&s.id!=='saved')||
      data.sources.find(s=>s.id===selected?.source_id&&s.id!=='saved')||
      data.sources.find(s=>s.source_ref&&s.source_ref===current?.registered_id)||
      data.sources.find(s=>s.id!=='saved');
  }
  async function load(endpoint){
    const ticket=++version;
    data={endpoint,label:endpoint,sources:[],models:[]};
    draft={query:'',source_id:'all',selected_id:'',reasoning:'default',pin:true,loading:false,busy:false,error:''};
    paint(true);if(!endpoint)return;
    await operation({action:'catalog'});if(ticket!==version||draft.error)return;
    draft.selected_id=data.current_model;draft.reasoning=data.current_reasoning||'default';paint(true);
    // Opening explicitly requests metadata for the current account; refresh only
    // when this source has no usable cached directory. No prompts are sent.
    const s=source();if(s&&!data.models.some(m=>m.source_id===s.id&&m.selectable))await operation({action:'refresh',source_id:s.id});
    const initial=context?.initial;
    if(ticket===version&&initial?.endpoint===endpoint){
      const selected=data.models.find(m=>m.id===(initial.choice_id||'saved:'+initial.model_id));
      if(selected){draft.selected_id=selected.id;draft.reasoning=initial.reasoning||selected.current_reasoning||'default';paint();}
    }
    if(ticket===version)dialog.querySelector('[data-picker-query]')?.focus();
  }
  async function open(endpoint,button,priority=null){
    if(dialog?.open)return;
    anchor=button;context=priority;dialog.showModal();await load(endpoint);
  }
  function usePriority(){
    const selected=data.models.find(m=>m.id===draft.selected_id);
    if(!clients.some(c=>c.id===data.endpoint)||!selected||selected.selectable===false||!selected.reasoning.some(r=>r.value===draft.reasoning))return;
    const saved=selected.registered_id&&draft.reasoning===(selected.current_reasoning||'default');
    if(!saved&&!selected.choice_revision){draft.error='模型目录已变化，请刷新后重新选择。';paint();return;}
    const id=saved?selected.registered_id:'pending:'+(globalThis.crypto?.randomUUID?.()||(Date.now().toString(36)+'-'+(++choiceSequence)));
    const result={model:{id,name:data.label+' · '+selected.name,model:selected.model,enabled:true,default_agent:data.endpoint,
      thinking_level:draft.reasoning,reasoning_label:selected.reasoning.find(r=>r.value===draft.reasoning)?.label||draft.reasoning},
      selection:saved?null:{id,endpoint:data.endpoint,choice_id:selected.id,reasoning:draft.reasoning,revision:selected.choice_revision}};
    try{context.onPick(result);close();toast('已加入顺序草稿；点击排序栏“保存”后生效。');}
    catch(error){draft.error=error.message;paint();}
  }
  function setup(){
    if(installed)return;installed=true;
    dialog=document.createElement('dialog');dialog.id='model-picker-dialog';dialog.className='model-picker-dialog';
    dialog.setAttribute('aria-label','选择模型与思考强度');document.body.append(dialog);
    document.addEventListener('click',event=>{const b=event.target.closest('[data-open-model-picker]');if(b)open(b.dataset.openModelPicker,b);});
    dialog.addEventListener('cancel',event=>{event.preventDefault();if(!draft.busy)close();});
    dialog.addEventListener('click',async event=>{
      if(event.target===dialog){const r=dialog.getBoundingClientRect();if((event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)&&!draft.busy)close();return;}
      const b=event.target.closest('button');if(!b||b.disabled)return;
      if(b.hasAttribute('data-picker-close')){if(!draft.busy)close();return;}
      if(b.hasAttribute('data-picker-choice')){
        draft.selected_id=b.dataset.pickerChoice;const selected=data.models.find(m=>m.id===draft.selected_id);
        draft.reasoning=selected.current_reasoning||'default';draft.error='';paint();
        if(['opencode','workbuddy'].includes(data.endpoint)&&selected.source_id!=='saved')await operation({action:'details',choice_id:selected.id});
      }
      if(b.hasAttribute('data-picker-refresh')){const s=source();if(s)await operation({action:'refresh',source_id:s.id});else{draft.error='请先管理连接，添加 API 或配置原生执行端。';paint();}}
      if(b.hasAttribute('data-picker-apply')){if(context)usePriority();else await operation({action:'select',selection:{choice_id:draft.selected_id,reasoning:draft.reasoning,revision:data.revision,pin:draft.pin}});}
      if(b.hasAttribute('data-picker-manage')){const s=source(),id=data.endpoint;close();if(s?.source_ref)openModel(s.source_ref,id);else{openEndpointDialog(id);setEndpointTab('models');}}
    });
    dialog.addEventListener('input',event=>{if(event.target.hasAttribute('data-picker-query')){draft.query=event.target.value;paint();}});
    dialog.addEventListener('change',async event=>{
      const el=event.target;
      if(el.hasAttribute('data-picker-client')){if(!draft.busy&&!draft.loading&&(!el.value||clients.some(c=>c.id===el.value)))await load(el.value);return;}
      if(el.hasAttribute('data-picker-pin'))draft.pin=el.checked;
      if(el.hasAttribute('data-picker-reasoning')){draft.reasoning=el.value;paint();}
      if(el.hasAttribute('data-picker-source')){draft.source_id=el.value;draft.error='';paint();const s=source();if(el.value!=='all'&&el.value!=='saved'&&s&&!data.models.some(m=>m.source_id===s.id&&m.selectable))await operation({action:'refresh',source_id:s.id});}
    });
    dialog.addEventListener('keydown',event=>{
      if(event.key==='Escape'){event.preventDefault();event.stopPropagation();if(!draft.busy)close();return;}
      if(!['ArrowDown','ArrowUp'].includes(event.key))return;
      if(event.target.tagName==='SELECT'||event.target.type==='checkbox')return;
      const rows=[...dialog.querySelectorAll('[data-picker-choice]:not(:disabled)')];if(!rows.length)return;
      event.preventDefault();const at=rows.indexOf(document.activeElement),next=at<0?0:(at+(event.key==='ArrowDown'?1:-1)+rows.length)%rows.length;rows[next].focus();
    });
  }
  globalThis.ModelDeskModelPicker={setup,open,openForPriority:(initial,button,onPick,slot)=>open(initial.endpoint||'',button,{initial,onPick,slot})};
})();
