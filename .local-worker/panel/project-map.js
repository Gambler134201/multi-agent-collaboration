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
      const path=String(node.path||'');
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
      <footer class="pm-footer">概览按路径名称辅助归类，不能代替实际业务文档。关系仅来自部分静态导入；测试关联不代表通过。</footer>`;
    doc.body.append(dialog);
    const find=id=>dialog.querySelector('#'+id);
    let selected=null, all=false, query='', focusBefore=null, previousData=null, tab='overview';
    const session=createSession({api,getView,changed:render});
    function render(state) {
      find('pm-project').textContent=state.project||'';
      find('pm-refresh').disabled=state.loading;
      find('pm-overview-tab').setAttribute('aria-pressed',String(tab==='overview'));
      find('pm-code-tab').setAttribute('aria-pressed',String(tab==='code'));
      find('pm-overview').hidden=tab!=='overview';find('pm-code').hidden=tab!=='code';
      find('pm-search').hidden=tab!=='code';find('pm-mode').hidden=tab!=='code';
      if(state.data!==previousData){selected=null;previousData=state.data;}
      if(!state.data){
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
      find('pm-overview').innerHTML=`<div class="pm-intro"><div><span class="pm-eyebrow">先看全貌</span><h3>这个项目由哪些部分组成？</h3><p>根据文件路径把代码整理成几个区域。点开区域看代表文件；需要核对实现时再进入“代码细节”。</p></div><div class="pm-health"><strong>${partial?'部分已收录':'索引已核对'}</strong><span>已看到 ${data.nodes.length} 个代码文件${partial?'；还有文件可能未收录':''}</span></div></div>
        <div class="pm-statline"><span>本页可见任务 <strong>${summary.tasks}</strong></span><span>待执行或进行中 <strong>${summary.active}</strong></span><span>已归类区域 <strong>${summary.groups.length}</strong></span></div>
        <h3 class="pm-section-title">代码大致分为这些部分 <small>按文件名/目录推断</small></h3><div class="pm-areas">${summary.groups.map(group=>`<details class="pm-area"><summary><strong>${escapeText(group.name)}</strong><span>${group.files.length} 个文件</span></summary><p>${escapeText(group.hint)}</p><div>${group.files.slice(0,8).map(file=>`<button type="button" class="pm-area-file" data-pm-id="${escapeText(file.id)}">${escapeText(file.path)}</button>`).join('')}${group.files.length>8?`<small>另有 ${group.files.length-8} 个文件，请到代码细节搜索。</small>`:''}</div></details>`).join('')}</div>
        <div class="pm-explainer"><h3>这些部分怎样联系？</h3>${summary.links.length?`<p>已找到的部分代码引用：${summary.links.map(link=>`${escapeText(link.from)} → ${escapeText(link.to)}`).join('；')}。</p>`:'<p>目前没有足够的静态关系可概括，不能据此判断它们没有联系。</p>'}<small>箭头只表示索引中识别的 Python 导入，并非完整运行流程。要理解业务逻辑，还需结合项目说明与实际运行。</small></div>`;
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
    function select(event){const target=event.target.closest?.('[data-pm-id]');if(!target)return;selected=target.dataset.pmId;query='';tab='code';find('pm-search').value='';render(session.snapshot());}
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
  const exported={create,_test:{escapeText,sameProject,visibleNodes,graphSlice,overview,createSession}};
  global.ModelDeskProjectMap=exported;
  if(typeof module!=='undefined'&&module.exports)module.exports=exported;
})(typeof window!=='undefined'?window:globalThis);
