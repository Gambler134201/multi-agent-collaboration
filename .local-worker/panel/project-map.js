(function (global) {
  'use strict';
  const escapeText = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const sameProject = (a,b) => {
    const normalize = value => {let s=String(value||'').replace(/\\/g,'/').replace(/\/+$/,'');return /^[a-z]:\//i.test(s)||s.startsWith('//')?s.toLowerCase():s;};
    return !!a && !!b && normalize(a)===normalize(b);
  };
  function visibleNodes(data, query='') {
    const needle=query.trim().toLocaleLowerCase();
    return (data?.nodes||[]).filter(node=>!needle||String(node.path).toLocaleLowerCase().includes(needle)||(node.symbols||[]).some(s=>String(s.name).toLocaleLowerCase().includes(needle)));
  }
  function graphSlice(data, selected, all, query='', cap=40) {
    const matching=visibleNodes(data,query), neighbors=new Set([selected]);
    for(const edge of data?.edges||[]){if(edge.source===selected||edge.target===selected){neighbors.add(edge.source);neighbors.add(edge.target);}}
    let nodes=all?matching:(data?.nodes||[]).filter(n=>neighbors.has(n.id));
    nodes=[...nodes].sort((a,b)=>(a.id===selected?-1:b.id===selected?1:0));
    const total=nodes.length;nodes=nodes.slice(0,cap);
    const ids=new Set(nodes.map(n=>n.id));
    return {nodes,edges:(data?.edges||[]).filter(e=>ids.has(e.source)&&ids.has(e.target)),total,truncated:total>cap};
  }
  const areas=[
    {name:'测试与检查',hint:'验证代码行为的检查文件',pattern:/(^|\/)(tests?|specs?)(\/|$)|(^|\/)test[_-]/i},
    {name:'界面与交互',hint:'用户看到和操作的页面',pattern:/(^|\/)(panel|ui|web|frontend|client|components?)(\/|$)|(?:^|\/)(?:app|view|page)[-_]/i},
    {name:'协作与任务',hint:'接收需求、安排任务并跟踪进度',pattern:/(brain|workflow|dispatch|route|stage|task|collaboration|orchestrat)/i},
    {name:'执行与工具',hint:'调用执行端并处理工具结果',pattern:/(worker|executor|execution|engine|agent|tool)/i},
    {name:'数据与服务',hint:'保存记录或提供接口',pattern:/(record|store|database|server|api|service|model)/i}
  ];
  function overview(data,tasks=[]) {
    const groups=areas.map(area=>({...area,files:[]}));
    groups.push({name:'其他代码',hint:'暂未从路径识别用途，需查看文件确认',files:[]});
    for(const node of data?.nodes||[]){
      const path=String(node.path||'').replace(/\\/g,'/').replace(/^\.local-worker\//,'');
      const match=groups.find(group=>group.pattern?.test(path))||groups[groups.length-1];
      match.files.push(node);
    }
    const projectTasks=tasks.filter(task=>sameProject(task.project,data?.project));
    const active=projectTasks.filter(task=>['queued','running','recovery_pending'].includes(task.status)).length;
    const links=[];
    for(const edge of data?.edges||[]){
      const from=groups.find(group=>group.files.some(file=>file.id===edge.source));
      const to=groups.find(group=>group.files.some(file=>file.id===edge.target));
      if(from&&to&&from!==to&&!links.some(link=>link.from===from.name&&link.to===to.name))links.push({from:from.name,to:to.name});
    }
    return {groups:groups.filter(group=>group.files.length),tasks:projectTasks.length,active,links:links.slice(0,4)};
  }
  function architecture(data,summary) {
    const files=data?.nodes||[];
    const find=name=>files.find(node=>String(node.path).replace(/\\/g,'/')==='.local-worker/'+name);
    const required=['panel_server.py','brain_gateway.py','dispatch_task.py','execution_engines.py','review_task.py'];
    if(required.every(name=>find(name))){
      const steps=[
        {id:'entry',name:'提出需求',role:'用户与主脑',hint:'你提出目标，Codex 或 DSH 主脑澄清需求、决定如何拆分，并给出验收标准。',output:'目标、操作范围、验收条件',names:['brain_gateway.py','panel_server.py']},
        {id:'plan',name:'安排任务',role:'协作控制器',hint:'按开关、执行端、模型范围和预算检查候选，将任务交给选定的执行端。',output:'带有模型、Agent 和文件范围的任务',names:['model_router.py','dispatch_task.py']},
        {id:'execute',name:'执行与自测',role:'Pi / DSH / WorkBuddy',hint:'执行 Agent 调查、修改文件或生成答复，并提交自测结果。执行结束后仍需主脑验收。',output:'交付文件或答复，以及执行证据',names:['execution_engines.py','workbuddy_worker.py']},
        {id:'review',name:'独立验收',role:'主脑负责判断',hint:'主脑检查真实交付并独立验证。通过后记录验收；发现缺陷则退回整改，必要时接手处理。',output:'通过、退回整改或需要处理',names:['review_task.py']},
        {id:'result',name:'工作台与任务记录',role:'全程可查看',hint:'从派发开始就能看进度，完成后可看交付、自测与验收结论。任务记录按项目保存，执行完成与验收通过分开显示。',output:'当前项目的状态与可追踪记录',names:['execution_result.py','panel_server.py']}
      ].map(step=>({...step,files:step.names.map(find).filter(Boolean)}));
      return {kind:'explained',steps,links:[
        {source:'entry',target:'plan',label:'目标与验收条件'},
        {source:'plan',target:'execute',label:'派发任务'},
        {source:'execute',target:'review',label:'交付后独立检查'},
        {source:'review',target:'execute',label:'不通过：退回整改',kind:'rework'},
        {source:'plan',target:'result',label:'派发状态',kind:'record'},
        {source:'execute',target:'result',label:'执行进度与交付',kind:'record'},
        {source:'review',target:'result',label:'验收结论',kind:'record'}
      ]};
    }
    const steps=summary.groups.map((group,index)=>({id:'area-'+index,name:group.name,role:group.files.length+' 个已收录文件',hint:group.hint,files:group.files}));
    return {kind:'inferred',steps,links:summary.links.map(link=>({source:steps.find(step=>step.name===link.from).id,target:steps.find(step=>step.name===link.to).id,label:'源码静态引用'}))};
  }
  function createSession({api,getView,changed=()=>{}}) {
    let serial=0, state={open:false,loading:false,data:null,error:'',project:null};
    const emit=()=>changed({...state});
    async function load(refresh=false) {
      if(!state.open)return false;
      const view={...getView()}, id=++serial;
      if(view.mode!=='project'||!view.project||!sameProject(view.project,state.project)){
        state={...state,data:null,loading:false,error:'请先选择一个明确项目。'};emit();return false;
      }
      state={...state,data:null,loading:true,error:''};emit();
      try{
        const data=await api('/api/project-map',{refresh},view);
        if(id!==serial||!state.open)return false;
        const current=getView();
        if(current.mode!=='project'||!sameProject(current.project,view.project))return false;
        if(!sameProject(data?.project,view.project)||!Array.isArray(data?.nodes)||!Array.isArray(data?.edges))throw new Error('返回的项目或地图结构不匹配，请重新选择项目。');
        state={...state,data,loading:false};emit();return true;
      }catch(error){
        if(id!==serial||!state.open)return false;
        state={...state,data:null,loading:false,error:String(error?.message||'项目地图暂不可读。')};emit();return false;
      }
    }
    return {
      async open(){const view=getView();if(view?.mode!=='project'||!view.project)return false;if(state.open)return true;state={open:true,loading:false,data:null,error:'',project:view.project};return load(false);},
      load,
      close(){++serial;state={open:false,loading:false,data:null,error:'',project:null};emit();},
      snapshot(){return {...state};}
    };
  }
  let singleton;
  function create({api,getView,getTasks=()=>[],toast=()=>{}}) {
    if(singleton)return singleton;
    const doc=global.document;
    const dialog=doc.createElement('dialog');dialog.id='project-map-dialog';dialog.className='pm-dialog';
    dialog.setAttribute('aria-labelledby','pm-title');
    dialog.innerHTML=`<header class="pm-heading"><div><h2 id="pm-title">读懂这个项目 <span class="pm-tag">只读</span></h2><p id="pm-project" class="pm-path"></p></div><button type="button" class="pm-close" aria-label="关闭项目地图">×</button></header>
      <div class="pm-tools"><button type="button" id="pm-overview-tab" aria-pressed="true">项目概览</button><button type="button" id="pm-code-tab" aria-pressed="false">代码细节</button><input type="search" id="pm-search" placeholder="搜索文件或符号" aria-label="搜索文件或符号"><button type="button" id="pm-mode">显示全部关系</button><button type="button" id="pm-refresh">刷新</button></div>
      <p id="pm-status" class="pm-status" role="status" aria-live="polite"></p><p id="pm-warning" class="pm-warning" hidden></p>
      <section id="pm-overview" class="pm-overview" aria-label="项目概览"></section>
      <div id="pm-code" class="pm-layout" hidden><nav id="pm-list" class="pm-list" aria-label="代码文件"></nav><section class="pm-center" aria-label="模块静态导入关系"><div id="pm-graph" class="pm-graph"></div><p id="pm-graph-note" class="pm-note"></p></section><aside id="pm-detail" class="pm-detail" aria-label="选中文件详情"></aside></div>
      <footer class="pm-footer">架构讲解与源码静态关系分别标明依据。索引可能不完整；相关测试不代表已通过。</footer>`;
    doc.body.append(dialog);
    const find=id=>dialog.querySelector('#'+id);
    let selected=null, selectedStep=null, all=false, query='', focusBefore=null, previousData=null, tab='overview', currentDiagram=null;
    const archObserver=global.ResizeObserver?new global.ResizeObserver(()=>drawArchitecture()):null;
    const session=createSession({api,getView,changed:render});
    function render(state) {
      find('pm-project').textContent=state.project||'';
      find('pm-refresh').disabled=state.loading;
      find('pm-overview-tab').setAttribute('aria-pressed',String(tab==='overview'));
      find('pm-code-tab').setAttribute('aria-pressed',String(tab==='code'));
      find('pm-overview').hidden=tab!=='overview';find('pm-code').hidden=tab!=='code';
      find('pm-search').hidden=tab!=='code';find('pm-mode').hidden=tab!=='code';
      if(state.data!==previousData){selected=null;selectedStep=null;previousData=state.data;}
      if(!state.data){
        archObserver?.disconnect();currentDiagram=null;
        find('pm-status').textContent=state.loading?'正在生成受限代码索引…':state.error||'请选择项目并打开地图。';
        find('pm-overview').replaceChildren();find('pm-list').replaceChildren();find('pm-graph').replaceChildren();find('pm-detail').replaceChildren();find('pm-warning').hidden=true;find('pm-graph-note').textContent='';return;
      }
      const data=state.data, matching=visibleNodes(data,query);
      if(!matching.some(n=>n.id===selected))selected=matching[0]?.id||null;
      const node=(data.nodes||[]).find(n=>n.id===selected), limits=data.limits||{};
      const partial=Object.values(limits).some(Boolean)||data.selection?.truncated;
      find('pm-status').textContent=`${data.summary?.files??data.nodes.length} 个代码文件 · ${data.summary?.edges??data.edges.length} 条静态关系 · ${data.summary?.unresolved??0} 项未解析 · ${data.index?.reused?'已核对内容哈希，复用解析':'本次扫描'}${data.index?.generated_at?' · 快照 '+data.index.generated_at.replace('T',' ').slice(0,19)+' UTC':''}`;
      find('pm-warning').hidden=!partial;find('pm-warning').textContent='本次索引存在扫描上限、符号截取、跳过文件或返回数量限制，只代表已扫描部分。可搜索已收录文件/符号；未收录不表示不存在。';
      const summary=overview(data,getTasks()||[]);
      const diagram=architecture(data,summary);
      currentDiagram=diagram;
      if(!diagram.steps.some(step=>step.id===selectedStep))selectedStep=diagram.steps[0]?.id||null;
      const activeStep=diagram.steps.find(step=>step.id===selectedStep);
      find('pm-overview').innerHTML=`<div class="pm-intro"><div><span class="pm-eyebrow">先看全貌</span><h3>${diagram.kind==='explained'?'一张图，看懂协作如何运转':'先看模块，再看它们怎样联系'}</h3><p>点击图中的模块，查看它负责什么、与谁相连。需要核对实现时，再点对应代码。</p></div><div class="pm-health"><strong>${partial?'部分已收录':'索引已核对'}</strong><span>已看到 ${data.nodes.length} 个代码文件${partial?'；还有文件可能未收录':''}</span></div></div>
        <div class="pm-statline"><span>本页可见任务 <strong>${summary.tasks}</strong></span><span>待执行或进行中 <strong>${summary.active}</strong></span><span>已归类区域 <strong>${summary.groups.length}</strong></span></div>
        <h3 class="pm-section-title">${diagram.kind==='explained'?'需求 → 执行 → 验收':'项目模块关系'} <small>${diagram.kind==='explained'?'按本项目职责整理的讲解图':'模块用途按路径推断；连线来自静态导入'}</small></h3>
        <div class="pm-architecture" role="group" aria-label="可交互项目架构图"><div id="pm-flow" class="pm-flow pm-flow-${diagram.kind}"><svg id="pm-arch-lines" class="pm-arch-lines" aria-hidden="true"></svg>${diagram.steps.map((step,index)=>`<button type="button" class="pm-flow-node pm-step-${escapeText(step.id)}${step.id===selectedStep?' is-active':''}" data-pm-step="${escapeText(step.id)}" aria-pressed="${step.id===selectedStep}" aria-controls="pm-arch-detail"><span class="pm-flow-index">${diagram.kind==='explained'&&step.id==='result'?'全程':String(index+1).padStart(2,'0')}</span><strong>${escapeText(step.name)}</strong><small>${escapeText(step.role)}</small></button>`).join('')}${!diagram.steps.length?'<p class="pm-note">暂未收录可组成架构图的代码文件。</p>':''}</div>
        <p class="pm-arch-legend">${diagram.kind==='explained'?'<span><i></i>任务流转</span><span><i class="pm-key-rework"></i>退回整改</span><span><i class="pm-key-record"></i>状态汇入</span>':'箭头表示已识别的源码引用，不代表执行先后。没有连线的模块可能仍有运行时联系。'}<span>点击节点，下方说明随之更新</span></p>
        ${activeStep?`<div id="pm-arch-detail" class="pm-arch-detail"><div><span class="pm-eyebrow">已选模块 · ${escapeText(activeStep.role)}</span><h4 aria-live="polite">${escapeText(activeStep.name)}</h4><p>${escapeText(activeStep.hint)}</p>${activeStep.output?`<p class="pm-arch-output"><strong>产出</strong> ${escapeText(activeStep.output)}</p>`:''}<ul class="pm-arch-links">${diagram.links.filter(link=>link.source===activeStep.id||link.target===activeStep.id).map(link=>`<li>${escapeText(diagram.steps.find(step=>step.id===link.source).name)} → ${escapeText(diagram.steps.find(step=>step.id===link.target).name)}<small>${escapeText(link.label)}</small></li>`).join('')}</ul></div><div class="pm-arch-files"><strong>对应代码 · 点击查看细节</strong>${activeStep.files.slice(0,6).map(file=>`<button type="button" data-pm-id="${escapeText(file.id)}">${escapeText(file.path)}</button>`).join('')}${activeStep.files.length>6?`<small>另有 ${activeStep.files.length-6} 个文件</small>`:''}</div></div>`:''}</div>
        <p class="pm-arch-basis">${diagram.kind==='explained'?'本图概括本项目的职责与任务流程；连线不等于文件之间逐步调用。':'模块名称是路径推断；连线仅涵盖本次收录的部分 Python 静态导入，暂未自动理解业务逻辑。'}</p>`;
      archObserver?.disconnect();
      if(tab==='overview'){drawArchitecture();archObserver?.observe(find('pm-flow'));}
      find('pm-list').innerHTML=matching.length?matching.map(n=>`<button type="button" class="pm-file${n.id===selected?' pm-selected':''}" data-pm-id="${escapeText(n.id)}" aria-pressed="${n.id===selected}"><strong>${escapeText(String(n.path).split('/').pop())}</strong><span>${escapeText(n.path)}</span></button>`).join(''):'<p class="pm-note">没有匹配的代码文件。空项目或不支持的文件类型可能没有节点。</p>';
      drawGraph(data);
      if(!node){find('pm-detail').textContent='选择一个文件查看符号和关系。';return;}
      const incoming=data.edges.filter(e=>e.target===selected), outgoing=data.edges.filter(e=>e.source===selected);
      const relations=(rows,key)=>rows.length?rows.map(e=>`<button type="button" class="pm-relation" data-pm-id="${escapeText(e[key])}">${escapeText(e[key])}<small>L${Number(e.line)||1} · ${escapeText(e.resolution||e.evidence||'static')}</small></button>`).join(''):'<p class="pm-note">未识别到直接关系。</p>';
      const unresolved=(data.unresolved||[]).filter(item=>item.source===selected);
      find('pm-detail').innerHTML=`<span class="pm-tag">${escapeText(node.language||'unknown')} · ${escapeText(node.extraction||'file_index')}</span><h3>${escapeText(node.path)}</h3><p class="pm-hash">SHA-256 ${escapeText(node.sha256||'未知')}</p>${node.degraded?`<p class="pm-warning">解析已降级：${escapeText(node.reason||'未知')}</p>`:''}
        <h4>关键符号${node.symbols_truncated?'（已截取）':''}</h4>${(node.symbols||[]).length?(node.symbols||[]).map(s=>`<div class="pm-symbol"><code>${escapeText(s.signature||s.name)}</code><small>L${Number(s.line)||1}</small></div>`).join(''):'<p class="pm-note">未提取符号，仅保留文件索引。</p>'}
        <h4>导入</h4>${relations(outgoing,'target')}<h4>被导入</h4>${relations(incoming,'source')}
        <h4>相关测试 · 未运行</h4>${(node.tests||[]).length?(node.tests||[]).map(path=>`<div class="pm-test">${escapeText(path)}</div>`).join(''):'<p class="pm-note">未识别到直接导入该模块的测试。</p>'}
        <h4>未解析项</h4>${unresolved.length?unresolved.map(item=>`<div class="pm-test">L${Number(item.line)||1} · ${escapeText(item.module)}<small>${escapeText(item.reason)}</small></div>`).join(''):'<p class="pm-note">当前文件无已记录的未解析项；这不证明关系完整。</p>'}`;
    }
    function drawArchitecture() {
      const flow=find('pm-flow'), svg=find('pm-arch-lines');
      if(!currentDiagram||tab!=='overview'||!flow?.getBoundingClientRect)return;
      const bounds=flow.getBoundingClientRect();
      if(!bounds.width||!bounds.height)return;
      const boxes=new Map(Array.from(flow.querySelectorAll('[data-pm-step]'),button=>{
        const rect=button.getBoundingClientRect();
        return [button.dataset.pmStep,{left:rect.left-bounds.left,right:rect.right-bounds.left,top:rect.top-bounds.top,bottom:rect.bottom-bounds.top,x:rect.left-bounds.left+rect.width/2,y:rect.top-bounds.top+rect.height/2}];
      }));
      svg.setAttribute('viewBox',`0 0 ${bounds.width} ${bounds.height}`);
      svg.innerHTML='<defs><marker id="pm-arch-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7 Z"/></marker></defs>'+currentDiagram.links.map(link=>{
        const a=boxes.get(link.source),b=boxes.get(link.target);if(!a||!b)return '';
        const active=link.source===selectedStep||link.target===selectedStep;
        if(link.kind==='record'&&!active)return '';
        let path, label='';
        if(link.kind==='record'){
          const right=a.x>bounds.width/2, side=right?bounds.width-8:8;
          const end=right?b.right-14:b.left+14;
          path=`M${right?a.right:a.left} ${a.y} L${side} ${a.y} L${side} ${b.top-14} L${end} ${b.top-14} L${end} ${b.top-3}`;
        }else if(link.kind==='rework'&&Math.abs(a.y-b.y)<12){
          const y=Math.min(a.top,b.top)-18;
          path=`M${a.x} ${a.top} C${a.x} ${y} ${a.x} ${y} ${(a.x+b.x)/2} ${y} S${b.x} ${y} ${b.x} ${b.top-3}`;
          label=`<text class="pm-arch-edge-label" x="${(a.x+b.x)/2}" y="${y-6}" text-anchor="middle">退回整改</text>`;
        }else if(link.kind==='rework'){
          const side=bounds.width-8;
          path=`M${a.right} ${a.y+12} L${side} ${a.y+12} L${side} ${b.y+12} L${b.right+3} ${b.y+12}`;
        }else if(Math.abs(a.y-b.y)<12){
          const right=b.x>a.x;
          path=`M${right?a.right:a.left} ${a.y} L${right?b.left-3:b.right+3} ${b.y}`;
        }else if(Math.abs(a.x-b.x)<12){
          const down=b.y>a.y;
          path=`M${a.x} ${down?a.bottom:a.top} L${b.x} ${down?b.top-3:b.bottom+3}`;
        }else{
          path=`M${a.x} ${a.bottom} C${a.x} ${(a.bottom+b.top)/2} ${b.x} ${(a.bottom+b.top)/2} ${b.x} ${b.top-3}`;
        }
        return `<path class="pm-arch-edge${link.kind?' pm-edge-'+escapeText(link.kind):''}${active?' is-active':''}" d="${path}" marker-end="url(#pm-arch-arrow)"><title>${escapeText(link.label)}</title></path>${label}`;
      }).join('');
    }
    function drawGraph(data) {
      const graph=graphSlice(data,selected,all,query), count=graph.nodes.length;
      if(!count){find('pm-graph').textContent='暂无可展示的静态关系。';find('pm-graph-note').textContent='';return;}
      const columns=Math.min(4,Math.max(1,Math.ceil(Math.sqrt(count)))), rows=Math.ceil(count/columns), width=760, height=Math.max(200,rows*82+30);
      const positions=new Map(graph.nodes.map((n,i)=>[n.id,{x:20+(i%columns)*((width-40)/columns),y:20+Math.floor(i/columns)*82}]));
      const nodeWidth=(width-55)/columns-12;
      const edges=graph.edges.map(e=>{const a=positions.get(e.source),b=positions.get(e.target);return `<path class="pm-edge" d="M${a.x+nodeWidth/2} ${a.y+30} L${b.x+nodeWidth/2} ${b.y+30}" marker-end="url(#pm-arrow)"/>`;}).join('');
      find('pm-graph').innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="模块静态导入图"><defs><marker id="pm-arrow" markerWidth="7" markerHeight="7" refX="10" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7"/></marker></defs>${edges}${graph.nodes.map(n=>{const p=positions.get(n.id),label=String(n.path).split('/').pop();return `<g class="pm-node${n.id===selected?' pm-node-selected':''}" role="button" tabindex="0" data-pm-id="${escapeText(n.id)}" aria-label="${escapeText(n.path)}"><title>${escapeText(n.path)}</title><rect x="${p.x}" y="${p.y}" width="${nodeWidth}" height="58" rx="8"/><text x="${p.x+10}" y="${p.y+24}">${escapeText(label.length>23?label.slice(0,21)+'…':label)}</text><text class="pm-node-language" x="${p.x+10}" y="${p.y+43}">${escapeText(n.language||'file')}</text></g>`;}).join('')}</svg>`;
      find('pm-graph-note').textContent=`${all?'全部关系':'所选文件及直接邻居'} · 图中 ${count}/${graph.total} 个节点${graph.truncated?'；图形已限量，左侧列表可继续搜索和选择':''}。`;
    }
    function restoreFocus(attribute,value,scope=dialog){Array.from(scope.querySelectorAll?.('['+attribute+']')||[]).find(element=>element.getAttribute(attribute)===value)?.focus();}
    function select(event){
      const step=event.target.closest?.('[data-pm-step]');
      if(step){selectedStep=step.dataset.pmStep;render(session.snapshot());restoreFocus('data-pm-step',selectedStep);return;}
      const target=event.target.closest?.('[data-pm-id]');if(!target)return;
      selected=target.dataset.pmId;query='';tab='code';find('pm-search').value='';render(session.snapshot());restoreFocus('data-pm-id',selected,find('pm-list'));
    }
    dialog.addEventListener('click',select);
    dialog.addEventListener('keydown',event=>{if((event.key==='Enter'||event.key===' ')&&event.target.matches?.('g[data-pm-id]')){event.preventDefault();select(event);}});
    dialog.querySelector('.pm-close').onclick=()=>dialog.close();
    dialog.addEventListener('close',()=>{session.close();try{if(focusBefore?.isConnected)focusBefore.focus();}catch{}});
    find('pm-search').oninput=event=>{query=event.target.value;render(session.snapshot());};
    find('pm-overview-tab').onclick=()=>{tab='overview';render(session.snapshot());};
    find('pm-code-tab').onclick=()=>{tab='code';render(session.snapshot());};
    find('pm-mode').onclick=()=>{all=!all;find('pm-mode').textContent=all?'只看直接关联':'显示全部关系';render(session.snapshot());};
    find('pm-refresh').onclick=()=>session.load(true);
    singleton={
      open(){const view=getView();if(view?.mode!=='project'||!view.project){toast('请先选择一个明确项目，再打开项目地图。',true);return Promise.resolve(false);}if(!dialog.open){focusBefore=doc.activeElement;query='';all=false;tab='overview';find('pm-search').value='';find('pm-mode').textContent='显示全部关系';dialog.showModal();}return session.open();},
      onProjectChange(){session.close();if(dialog.open)dialog.close();}
    };
    return singleton;
  }
  const exported={create,_test:{escapeText,sameProject,visibleNodes,graphSlice,overview,architecture,createSession}};
  global.ModelDeskProjectMap=exported;
  if(typeof module!=='undefined'&&module.exports)module.exports=exported;
})(typeof window!=='undefined'?window:globalThis);
