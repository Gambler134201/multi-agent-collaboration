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
  function create({api,getView,toast=()=>{}}) {
    if(singleton)return singleton;
    const doc=global.document;
    const dialog=doc.createElement('dialog');dialog.id='project-map-dialog';dialog.className='pm-dialog';
    dialog.setAttribute('aria-labelledby','pm-title');
    dialog.innerHTML=`<header class="pm-heading"><div><h2 id="pm-title">项目地图 <span class="pm-tag">只读</span></h2><p id="pm-project" class="pm-path"></p></div><button type="button" class="pm-close" aria-label="关闭项目地图">×</button></header>
      <div class="pm-tools"><input type="search" id="pm-search" placeholder="搜索已索引文件或符号" aria-label="搜索已索引文件或符号"><button type="button" id="pm-mode">显示全部关系</button><button type="button" id="pm-refresh">刷新索引</button></div>
      <p id="pm-status" class="pm-status" role="status" aria-live="polite"></p><p id="pm-warning" class="pm-warning" hidden></p>
      <div class="pm-layout"><nav id="pm-list" class="pm-list" aria-label="代码文件"></nav><section class="pm-center" aria-label="模块静态导入关系"><div id="pm-graph" class="pm-graph"></div><p id="pm-graph-note" class="pm-note"></p></section><aside id="pm-detail" class="pm-detail" aria-label="选中文件详情"></aside></div>
      <footer class="pm-footer">箭头表示静态导入，含路径约定候选，不是完整调用链。测试关联不代表通过；任务与验收记录尚未接入。</footer>`;
    doc.body.append(dialog);
    const find=id=>dialog.querySelector('#'+id);
    let selected=null, all=false, query='', focusBefore=null, previousData=null;
    const session=createSession({api,getView,changed:render});
    function render(state) {
      find('pm-project').textContent=state.project||'';
      find('pm-refresh').disabled=state.loading;
      if(state.data!==previousData){selected=null;previousData=state.data;}
      if(!state.data){
        find('pm-status').textContent=state.loading?'正在生成受限代码索引…':state.error||'请选择项目并打开地图。';
        find('pm-list').replaceChildren();find('pm-graph').replaceChildren();find('pm-detail').replaceChildren();find('pm-warning').hidden=true;find('pm-graph-note').textContent='';return;
      }
      const data=state.data, matching=visibleNodes(data,query);
      if(!matching.some(n=>n.id===selected))selected=matching[0]?.id||null;
      const node=(data.nodes||[]).find(n=>n.id===selected), limits=data.limits||{};
      const partial=Object.values(limits).some(Boolean)||data.selection?.truncated;
      find('pm-status').textContent=`${data.summary?.files??data.nodes.length} 个代码文件 · ${data.summary?.edges??data.edges.length} 条静态关系 · ${data.summary?.unresolved??0} 项未解析 · ${data.index?.reused?'已核对内容哈希，复用解析':'本次扫描'}${data.index?.generated_at?' · 快照 '+data.index.generated_at.replace('T',' ').slice(0,19)+' UTC':''}`;
      find('pm-warning').hidden=!partial;find('pm-warning').textContent='本次索引存在扫描上限、符号截取、跳过文件或返回数量限制，只代表已扫描部分。可搜索已收录文件/符号；未收录不表示不存在。';
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
    function select(event){const target=event.target.closest?.('[data-pm-id]');if(!target)return;selected=target.dataset.pmId;query='';find('pm-search').value='';render(session.snapshot());}
    dialog.addEventListener('click',select);
    dialog.addEventListener('keydown',event=>{if((event.key==='Enter'||event.key===' ')&&event.target.matches?.('g[data-pm-id]')){event.preventDefault();select(event);}});
    dialog.querySelector('.pm-close').onclick=()=>dialog.close();
    dialog.addEventListener('close',()=>{session.close();try{if(focusBefore?.isConnected)focusBefore.focus();}catch{}});
    find('pm-search').oninput=event=>{query=event.target.value;render(session.snapshot());};
    find('pm-mode').onclick=()=>{all=!all;find('pm-mode').textContent=all?'只看直接关联':'显示全部关系';render(session.snapshot());};
    find('pm-refresh').onclick=()=>session.load(true);
    singleton={
      open(){const view=getView();if(view?.mode!=='project'||!view.project){toast('请先选择一个明确项目，再打开项目地图。',true);return Promise.resolve(false);}if(!dialog.open){focusBefore=doc.activeElement;query='';all=false;find('pm-search').value='';find('pm-mode').textContent='显示全部关系';dialog.showModal();}return session.open();},
      onProjectChange(){session.close();if(dialog.open)dialog.close();}
    };
    return singleton;
  }
  const exported={create,_test:{escapeText,sameProject,visibleNodes,graphSlice,createSession}};
  global.ModelDeskProjectMap=exported;
  if(typeof module!=='undefined'&&module.exports)module.exports=exported;
})(typeof window!=='undefined'?window:globalThis);
