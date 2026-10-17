const $ = id => document.getElementById(id);
// Appearance stays in this browser; it never changes model or workflow settings.
const themeMedia = window.matchMedia('(prefers-color-scheme: dark)');
const themeStorageKey = 'model-desk.theme';
const themeModes = new Set(['system', 'light', 'dark']);
let themeMode = 'system';
try { const savedTheme = localStorage.getItem(themeStorageKey); if (themeModes.has(savedTheme)) themeMode = savedTheme; } catch {}
function applyTheme() {
  document.documentElement.dataset.theme = themeMode === 'system' ? (themeMedia.matches ? 'dark' : 'light') : themeMode;
  $('theme-mode').value = themeMode;
}
$('theme-mode').addEventListener('change', event => {
  themeMode = themeModes.has(event.target.value) ? event.target.value : 'system';
  try { localStorage.setItem(themeStorageKey, themeMode); } catch {}
  applyTheme();
});
themeMedia.addEventListener('change', () => { if (themeMode === 'system') applyTheme(); });
window.addEventListener('storage', event => {
  if (event.key !== themeStorageKey && event.key !== null) return;
  themeMode = themeModes.has(event.newValue) ? event.newValue : 'system';
  applyTheme();
});
applyTheme();
// Font size is a local appearance preference as well; it never changes workflow or model settings.
const fontScaleStorageKey = 'model-desk.font-scale';
const fontScales = new Set(['small', 'standard', 'large', 'xlarge']);
let fontScale = 'standard';
try { const savedFontScale = localStorage.getItem(fontScaleStorageKey); if (fontScales.has(savedFontScale)) fontScale = savedFontScale; } catch {}
function applyFontScale() {
  document.documentElement.dataset.fontScale = fontScale;
  $('font-scale').value = fontScale;
}
$('font-scale').addEventListener('change', event => {
  fontScale = fontScales.has(event.target.value) ? event.target.value : 'standard';
  try { localStorage.setItem(fontScaleStorageKey, fontScale); } catch {}
  applyFontScale();
});
window.addEventListener('storage', event => {
  if (event.key !== fontScaleStorageKey && event.key !== null) return;
  fontScale = fontScales.has(event.newValue) ? event.newValue : 'standard';
  applyFontScale();
});
applyFontScale();
const escapeText = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
// Steady-state cost control: identical markup is never written twice, so an
// unchanged panel keeps its existing DOM nodes instead of rebuilding them on a
// poll.  Any visible change (model, task, acceptance, goal/stage) still renders.
const renderedMarkup = new WeakMap();
function setMarkup(node, markup) {
  if (!node) return false;
  if (renderedMarkup.get(node) === markup) return false;
  renderedMarkup.set(node, markup);
  node.innerHTML = markup;
  return true;
}
const statusNames = {queued:'等待执行',running:'执行中',completed:'执行完成',failed:'失败',cancelled:'已停止',recovery_pending:'待恢复'};
const providerNames = {local:'本地 / 自建服务',deepseek:'DeepSeek',glm:'GLM / 智谱',custom:'兼容接口'};
let state = {models:[],tasks:[],default_model:null,observability:{},tree:null}, token = '', selectedTask = null, taskFilter = 'all', detailTab = 'result';
let detailPrompt = '', promptTask = null, pendingDelete = null, toastTimer;
let continuingTask=null;
const treeOpen = new Set(); const observabilityLoading = new Set(); let pendingAction=null, stageGoal=null;
let modelThinkingProfile=null;
// Task history management state. Records, selection and the preview token always belong to one bound project.
let historyRecords=[], historySelected=new Set(), historyStatus='all', historyKeyword='';
let historyProject=null, historyToken=null, historyPreview=null, historyExpiresAt=0;
let historyLoading=false, historySubmitting=false,historyGeneration=0;
// Per-document context. Never inherit a different tab's last selected project.
const viewQuery=new URLSearchParams(window.location.search);
let rememberedPanelView=null;
// Session storage belongs to this HTTP panel tab, never a global last-project.
// A new MCP entry without trustworthy context stays unbound and offers explicit recovery.
if(!window.modelDeskHost&&!viewQuery.has('project')&&!viewQuery.has('view')){
  try{const saved=JSON.parse(sessionStorage.getItem('model-desk.panel-view'));if(saved?.mode==='project'&&typeof saved.project==='string')rememberedPanelView=saved;}catch{}
}
let projectView=window.modelDeskInitialView|| (viewQuery.get('project')?{mode:'project',project:viewQuery.get('project')}:viewQuery.get('view')==='all'?{mode:'all',project:null}:rememberedPanelView||{mode:'unbound',project:null});
let viewEpoch=0;
let recentProjects=[];
let knownProjects=[];
const bookmarkedProjects=new Set();
try{const saved=JSON.parse(localStorage.getItem('model-desk.recent-projects'));if(Array.isArray(saved))recentProjects=saved.filter(p=>typeof p==='string'&&p.length<2000).slice(0,8);}catch{}
// Real local storage state for the bound project. It is read from /api/db/space, never guessed.
let projectStorage=null, projectStorageProject=null, projectStorageError='', legacyStorage=null;
let projectPathPinned=false, projectCatalogAt=0;
function savePanelBinding(){
  if(!window.modelDeskHost){
    try{sessionStorage.setItem('model-desk.panel-view',JSON.stringify(projectView));
      const url=new URL(window.location.href);url.searchParams.delete('project');url.searchParams.delete('view');
      if(projectView.mode==='project')url.searchParams.set('project',projectView.project);
      else if(projectView.mode==='all')url.searchParams.set('view','all');
      history.replaceState(null,'',url);
    }catch{}
  }
  if(projectView.mode==='project'){
    recentProjects=[projectView.project,...recentProjects.filter(p=>p!==projectView.project)].slice(0,8);
    try{localStorage.setItem('model-desk.recent-projects',JSON.stringify(recentProjects));}catch{}
    if(token&&!bookmarkedProjects.has(projectView.project)){
      bookmarkedProjects.add(projectView.project);
      api('/api/projects/remember',{}).catch(()=>{});
    }
  }
}
function projectBaseName(path){
  const text=String(path||'').replace(/[\\/]+$/,'');
  if(!text)return '';
  const parts=text.split(/[\\/]+/);
  return parts[parts.length-1]||text;
}
// Windows paths compare case-insensitively; this is only for display equality, never for authorization.
function sameProjectPath(a,b){
  const norm=value=>String(value||'').replace(/\//g,'\\').replace(/\\+$/,'').toLowerCase();
  return !!a&&!!b&&norm(a)===norm(b);
}
function mergeKnownProjects(catalog){
  const seen=new Map();
  const push=row=>{
    const path=typeof row==='string'?row:row?.path;
    if(typeof path!=='string'||!path||path.length>2000)return;
    const key=path.replace(/\//g,'\\').replace(/\\+$/,'').toLowerCase();
    if(seen.has(key))return;
    seen.set(key,{path,label:projectBaseName(path)||path});
  };
  recentProjects.forEach(push);
  (catalog?.recent_projects||[]).forEach(push);
  (catalog?.projects||[]).forEach(push);
  knownProjects=[...seen.values()].slice(0,12);
}
function renderProjectMenu(){
  const box=$('project-view-known');if(!box)return;
  const current=projectView.mode==='project'?projectView.project:null;
  setMarkup(box,knownProjects.map(row=>{
    const isCurrent=sameProjectPath(row.path,current);
    return `<button type="button" class="project-menu-item" role="menuitem" data-project="${escapeText(row.path)}" title="${escapeText(row.path)}"${isCurrent?' aria-current="true"':''}>
      <span class="project-menu-name">${escapeText(row.label||projectBaseName(row.path)||row.path)}</span>
      <span class="project-menu-path">${escapeText(row.path)}</span>${isCurrent?'<span class="project-menu-current">当前</span>':''}</button>`;
  }).join(''));
  const empty=$('project-menu-empty');if(empty)empty.hidden=knownProjects.length>0;
}
function openProjectMenu(){
  const menu=$('project-view-menu'),sw=$('project-view-switch');if(!menu||!sw)return;
  renderProjectMenu();menu.hidden=false;sw.setAttribute('aria-expanded','true');
  loadProjectCatalog();
}
function closeProjectMenu(){
  const menu=$('project-view-menu'),sw=$('project-view-switch');
  if(!menu||menu.hidden)return;
  menu.hidden=true;if(sw)sw.setAttribute('aria-expanded','false');
}
function toggleProjectMenu(){const menu=$('project-view-menu');if(!menu)return;if(menu.hidden)openProjectMenu();else closeProjectMenu();}
// The catalog aggregates every known project, so cache it briefly instead of re-reading on every menu open.
async function loadProjectCatalog(force=false){
  if(!force&&projectCatalogAt&&Date.now()-projectCatalogAt<30000)return;
  try{const catalog=await api('/api/projects');mergeKnownProjects(catalog);projectCatalogAt=Date.now();}
  catch(error){if(!error.stale)mergeKnownProjects(null);}
  renderProjectMenu();
}
// Explicit choice only: never derives a project from the service cwd or another panel's last project.
async function switchToProject(project){
  if(!project)return;
  closeProjectMenu();
  if(projectView.mode==='project'&&sameProjectPath(projectView.project,project))return;
  try{
    window.modelDeskLockProjectContext?.();
    const data=await api('/api/state',undefined,{mode:'project',project});
    if(!data.view||data.view.mode!=='project')throw new Error('工作台连接需要升级');
    changeProjectView(data.view);
  }catch(error){if(!error.stale)toast(error.message,true);}
}
function formatBytes(value){
  const number=Number(value);
  if(!Number.isFinite(number)||number<0)return '—';
  if(number<1024)return `${number} B`;
  const units=['KB','MB','GB'];
  let size=number/1024,index=0;
  while(size>=1024&&index<units.length-1){size/=1024;index++;}
  return `${size.toFixed(size>=10?0:1)} ${units[index]}`;
}
function projectStorageText(row){
  if(!row)return '';
  if(row.initialized===false||row.exists===false)return '尚未初始化记录 · 首次创建任务或目标时才写入 .model-desk，空项目只查看不落盘（这不是记录丢失）';
  const parts=[row.writable===false?'记录目录不可写':'记录库就绪'];
  if(Number.isFinite(row.size_bytes))parts.push(formatBytes(row.size_bytes));
  if(Number.isFinite(row.free_bytes)&&row.free_bytes>0)parts.push(`可回收 ${formatBytes(row.free_bytes)}`);
  if(row.linked)parts.push('记录目录是链接');
  if(row.writable===false&&row.reason)parts.push(row.reason);
  return parts.join(' · ');
}
function resetProjectStorage(){projectStorage=null;projectStorageProject=null;projectStorageError='';legacyStorage=null;}
function renderProjectStorage(){
  const box=$('project-storage-state');if(!box)return;
  if(projectView.mode!=='project'||!projectView.project){box.hidden=true;box.textContent='';box.removeAttribute('title');box.removeAttribute('data-state');return;}
  const text=projectStorageError?`存储状态读取失败：${projectStorageError}`:(projectStorageText(projectStorage)||'正在读取项目存储状态…');
  box.textContent=text;box.hidden=false;
  box.title=projectStorage?.path?`记录库：${projectStorage.path}`:'';
  box.dataset.state=projectStorageError?'error':!projectStorage?'loading':(projectStorage.initialized===false||projectStorage.exists===false)?'empty':(projectStorage.writable===false||projectStorage.linked)?'warn':'ok';
}
async function refreshProjectStorage(){
  if(projectView.mode!=='project'||!projectView.project){resetProjectStorage();renderProjectStorage();return;}
  const epoch=viewEpoch,project=projectView.project;
  try{
    const data=await api('/api/db/space');
    if(epoch!==viewEpoch||projectView.mode!=='project'||!sameProjectPath(projectView.project,project))return;
    const rows=Array.isArray(data?.databases)?data.databases:[];
    projectStorage=rows.find(row=>row.scope==='project')||rows[0]||null;
    projectStorageProject=project;projectStorageError='';
  }catch(error){
    if(error.stale||epoch!==viewEpoch)return;
    projectStorage=null;projectStorageProject=project;projectStorageError=error.message||'读取失败';
  }
  renderProjectStorage();
  if(!state.tasks.length)renderTasks();
}
// Honest empty state: an uninitialized project is not a lost record, and an unbound panel never guesses a directory.
function projectEmptyState(){
  if(projectView.mode==='unbound')return {title:'尚未选择项目',body:'点击“选择项目”，在系统文件夹选择器里选择真实的本机项目目录；不会猜测服务安装目录。'};
  if(projectView.mode==='all')return {title:'显式全部项目视图',body:'这是由 URL 或宿主显式打开的全部项目视图；日常按单个项目使用，记录管理仍需要具体项目。'};
  if(projectStorageError)return {title:'项目存储状态暂时不可读',body:`${projectStorageError}；这不代表记录被删除，可稍后刷新或检查目录权限。`};
  if(!projectStorage)return {title:'正在读取项目记录…',body:'空项目只查看不写入 .model-desk；读取完成后会显示真实的初始化状态。'};
  if(projectStorage.initialized===false||projectStorage.exists===false)return {title:'该项目尚未初始化记录',body:'首次创建任务或目标时才会创建 .model-desk；这里没有条目不代表记录丢失。'};
  if(projectStorage.writable===false)return {title:'项目记录目录不可写',body:projectStorage.reason||'请检查目录权限后重试。'};
  return {title:taskFilter==='all'?'准备好分派第一个任务':'这个筛选下暂无任务',body:'任务、进度和执行结果会显示在这里。'};
}
function viewHeader(view=projectView){return btoa(unescape(encodeURIComponent(JSON.stringify(view)))).replace(/\+/g,'-').replace(/\//g,'_');}
function renderProjectView(){
  const bound=projectView.mode==='project'&&!!projectView.project;
  const name=$('project-view-name'),row=$('project-path-row'),path=$('project-view-path'),sw=$('project-view-switch'),status=$('project-view-status');
  if(bound){
    name.textContent=projectBaseName(projectView.project)||projectView.project;
    name.title=projectView.project;
    path.textContent=projectView.project;
    row.hidden=!projectPathPinned;
    sw.textContent='切换项目';
    status.textContent=`当前项目：${projectView.project}`;
  }else{
    name.textContent=projectView.mode==='all'?'全部项目（显式视图）':'尚未选择项目';
    name.title=projectView.mode==='all'?'由 URL 或宿主显式指定的全部项目视图；日常按单个项目使用':'尚未选择项目：点击“选择项目”，不会使用服务安装目录或其它页面的记录';
    path.textContent='';row.hidden=true;
    sw.textContent='选择项目';
    status.textContent=projectView.mode==='all'?'全部项目视图：记录管理需要具体项目':'尚未选择项目';
  }
  name.setAttribute('aria-expanded',row.hidden?'false':'true');
  // Never rebuild an open menu during a poll; that would replace items under the pointer.
  const menu=$('project-view-menu');
  if(!menu||menu.hidden)renderProjectMenu();
  renderProjectStorage();
}
function changeProjectView(view){
  if(JSON.stringify(view)===JSON.stringify(projectView))return;
  projectView=view;viewEpoch++;projectPathPinned=false;closeProjectMenu();resetProjectStorage();savePanelBinding();selectedTask=null;detailPrompt='';promptTask=null;continuingTask=null;
  pendingAction=null;stageGoal=null;detailTab='result';taskFilter='all';treeOpen.clear();observabilityLoading.clear();
  // Project switch invalidates this dialog: no stale preview token, no stale selection, no stale confirmation.
  resetHistoryManagement();
  state={...state,tasks:[],tree:null,observability:{},delegation:null};
  for(const id of ['task-dialog','goal-dialog','stage-dialog','action-dialog','task-records-confirm-dialog','task-records-dialog']){if($(id)?.open)$(id).close();}
  document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter==='all'));
  renderProjectView();renderTasks();renderDetail();renderGoals();refresh();refreshProjectStorage();
  // A project switch owns the chain again: prompt cadence for the new project, and no lost refresh.
  markPanelActivity();startAutoPoll();
}
window.addEventListener('modeldesk-project-context',event=>changeProjectView(event.detail));
const engineNames={dsh:'DeepSeek Harness',pi:'Pi Agent',direct:'直接调用',aider:'Aider'};
const defaultAgent=()=>state.workflow?.execution_agent||'dsh';
const registeredAgents=()=>state.execution_agents||{dsh:{name:'DeepSeek Harness'},pi:{name:'Pi Agent'}};
const agentIds=()=>Object.keys(registeredAgents());
const hasAgentSession=task=>task.engine==='pi'?!!(task.pi_session_id&&task.pi_session_file):task.engine==='dsh'&&!!task.dsh_session_id;
const presetNames={native:'原生模式',standard:'标准模式'};
function thinkingInfo(model,task=null){
  if(task?.thinking_runtime)return {label:task.thinking_runtime.label,note:task.thinking_runtime.note||'此任务发送时记录的思考配置。'};
  const sent=task?.request_thinking||{};
  if(sent.thinking?.type==='disabled'||sent.enable_thinking===false||['none','off'].includes(sent.reasoning_effort))return {label:'关闭',note:'此任务请求明确关闭思考。'};
  if(sent.reasoning_effort){const names={low:'低',medium:'中',high:'高',xhigh:'超高',max:'最高'};return {label:names[sent.reasoning_effort]||sent.reasoning_effort,note:'此任务请求明确指定的思考等级。'};}
  if(task&&task.thinking_policy==='引擎默认')return {label:'引擎默认（历史等级未记录）',note:'此历史任务未记录当时的引擎等级；不使用当前选择替代历史配置。'};
  if(model?.thinking)return {label:model.thinking.label,note:model.thinking.note||'沿用已保存的思考设置。'};
  if(model?.provider==='local')return {label:'引擎默认',note:'具体思考开关和等级由推理服务决定；请读取引擎默认进行确认。'};
  let official=false;try{const url=new URL(model?.api_base||'');official=url.protocol==='https:'&&url.hostname==='api.deepseek.com'&&(!url.port||url.port==='443');}catch{}
  if(official)return {label:'高（官方默认）',note:'工作台未指定等级；DeepSeek 官方默认开启思考并使用 high。此标注来自官方默认规则，不代表逐请求测量。'};
  return {label:model||task?.thinking_policy?'服务商默认':'未记录',note:'工作台未指定思考等级，具体值取决于服务端。'};
}
const testingModels = new Set();
const refreshingBalances = new Set();
const expandedBillingModels = new Set();

const sessionRejected=(response,data)=>response.status===403&&(data?.code==='panel_session_expired'||
  (data?.error==='请刷新面板后重试'&&Object.keys(data).length===1));
let sessionRefresh=null;
async function refreshBrowserSession(rejectedToken){
  if(token&&token!==rejectedToken)return token;
  if(!sessionRefresh)sessionRefresh=(async()=>{
    const response=await fetch('/api/session',{cache:'no-store'}),data=await response.json();
    if(!response.ok||typeof data.token!=='string'||!data.token)throw new Error('工作台认证暂时无法恢复，请重新打开工作台。');
    token=data.token;return token;
  })().finally(()=>{sessionRefresh=null;});
  return sessionRefresh;
}
async function api(path, body, view=projectView) {
  const epoch=viewEpoch,encodedView=viewHeader(view),payload=body===undefined?undefined:JSON.stringify(body);
  const checkView=()=>{if(epoch!==viewEpoch){const error=new Error('项目已切换，忽略旧响应');error.stale=true;throw error;}};
  const send=credential=>fetch(path,{method:body===undefined?'GET':'POST',
    headers:{'X-Model-Desk-View':encodedView,...(body===undefined?{}:{'Content-Type':'application/json','X-Panel-Token':credential})},body:payload});
  const rejectedToken=token;
  let response=await send(rejectedToken),data=await response.json();checkView();
  if(body!==undefined&&sessionRejected(response,data)&&!window.modelDeskHost){
    const credential=await refreshBrowserSession(rejectedToken);checkView();
    response=await send(credential);data=await response.json();checkView();
  }
  if (!response.ok) throw new Error(window.modelDeskHost&&sessionRejected(response,data)?
    '插件连接认证尚未恢复，请重启 Codex 后重新打开模型工作台。':data.error||`请求失败：${response.status}`);
  return data;
}
function toast(message, error=false) {
  clearTimeout(toastTimer); $('toast').textContent = message; $('toast').classList.toggle('error',error); $('toast').hidden=false;
  toastTimer = setTimeout(() => $('toast').hidden=true, error ? 7000 : 3500);
}
function clock(value) { return value ? new Date(value).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—'; }
function taskAccepted(task) {
  return ['accepted','final_accepted'].includes(task.review?.state);
}
function taskDisplayStatus(task) {
  // Acceptance comes from the real review attached by the backend. Preserve
  // live execution visibility and never trust task.json's cached review_state.
  return taskAccepted(task)&&['completed','failed','cancelled','recovery_pending'].includes(task.status)?'completed':task.status;
}
function taskDisplayPhase(task) {
  if(taskAccepted(task)&&taskDisplayStatus(task)==='completed')return task.review.state==='final_accepted'?'主脑接管完成 · 独立验收通过':'任务已完成 · 独立验收通过';
  return task.phase||'';
}
function duration(task) {
  if(task.phase==='历史任务')return '历史记录';
  const begin = task.started_at || task.created_at;
  if (!begin) return '—';
  const end=task.ended_at||(taskAccepted(task)&&taskDisplayStatus(task)==='completed'?(task.review.final?.accepted_at||task.review.updated_at):null);
  if(taskAccepted(task)&&taskDisplayStatus(task)==='completed'&&!Number.isFinite(Date.parse(end||'')))return '用时未记录';
  const seconds = Math.max(0,Math.floor(((end ? new Date(end) : new Date())-new Date(begin))/1000));
  return seconds >= 60 ? `${Math.floor(seconds/60)} 分 ${seconds%60} 秒` : `${seconds} 秒`;
}
function badge(status, text) {return `<span class="badge ${escapeText(status)}">${escapeText(text || statusNames[status] || status)}</span>`;}

function money(value,currency='CNY') {
  const amount=Number(value);
  return Number.isFinite(amount)?`${currency==='USD'?'$':'¥'}${amount.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:6})}`:'—';
}
// Usage-based cost is confirmed only after a usage report settled; never show ¥0 as if it were a verified total.
function settledCost(value,known){return known?money(value):'待核对';}
function taskCostText(task){
  const known=Number.isFinite(task?.cost_settled_cny)&&((task.cost_settled_calls||0)>0||task.cost_settled_cny>0);
  const parts=[`按用量计算费用 ${settledCost(Number(task?.cost_settled_cny)||0,known)}`];
  if(task?.cost_unknown_calls>0)parts.push(`待核对 ${task.cost_unknown_calls} 次`);
  const unsettled=Number(task?.cost_unsettled_cny)||0;
  if(unsettled>1e-9)parts.push(`未结算预留 ${money(unsettled)}`);
  return parts.join(' · ');
}
function beijingClock(value){return value?new Date(value).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit'}):'—';}
function changeTime(value){
  if(!value)return '';
  const target=new Date(value),now=new Date();
  const date=target.toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'});
  const today=now.toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai'});
  return `${date===today?'今天':date} ${beijingClock(value)}`;
}
function renderBilling(model){
  if(!model.billing)return '';
  const {pricing,balance}=model.billing;
  const known=['peak','off_peak'].includes(pricing.period);
  const periodClass=pricing.period==='peak'?'peak':pricing.period==='off_peak'?'off-peak':'unknown';
  const next=pricing.next_change;
  const switching=next?`${changeTime(next.at)} 切换${next.period==='peak'?'峰价':next.period==='off_peak'?'谷价':'待核实时段'}`:known?'':'请查看官方价格说明';
  const rates=pricing.rates;
  const balances=balance.balance_infos||[];
  const pending=balance.refreshing||refreshingBalances.has(model.id);
  const available=balance.is_available;
  const expanded=expandedBillingModels.has(model.id);
  const compactBalance=balances.length?balances.map(row=>`<strong>${escapeText(money(row.total_balance,row.currency))}</strong>`).join(' / '):`<strong class="balance-placeholder" title="${escapeText(balance.error||'')}">${balance.status==='loading'?'读取中…':balance.status==='missing_key'?'未配置密钥':balance.status==='unsupported'?'暂不支持':'查询失败'}</strong>`;
  const balanceContent=balances.length?balances.map(row=>`<div class="balance-amount"><strong>${escapeText(money(row.total_balance,row.currency))}</strong><span>${escapeText(row.currency)} ${balance.stale?'上次余额':'可用余额'}</span></div><div class="balance-breakdown"><span>充值 ${escapeText(money(row.topped_up_balance,row.currency))}</span><span>赠金 ${escapeText(money(row.granted_balance,row.currency))}</span></div>`).join(''):
    `<p class="billing-message ${balance.status==='error'?'error':''}">${escapeText(balance.status==='loading'?'正在读取账户余额…':balance.error||'尚未查询余额')}</p>`;
  return `<section class="billing-panel" aria-label="DeepSeek 定价与余额">
    <div class="billing-overview"><div class="billing-summary"><div class="billing-summary-balance"><span class="label">${balance.stale?'旧余额':'余额'}</span><div class="balance-amount">${compactBalance}</div></div></div>
    <div class="billing-summary-footer"><button class="billing-toggle" data-model-action="details" data-id="${escapeText(model.id)}" aria-expanded="${expanded}" aria-controls="billing-details-${escapeText(model.id)}" aria-label="${expanded?'收起详情':'展开详情'}">${expanded?'收起 ↑':'详情 ↓'}</button>${pricing.top_up_url?`<a class="top-up-link" href="${escapeText(pricing.top_up_url)}" target="_blank" rel="noopener noreferrer" aria-label="官方充值">充值 ↗</a>`:''}</div></div>
    <div id="billing-details-${escapeText(model.id)}" class="billing-details" ${expanded?'':'hidden'}>
    <div class="pricing-heading"><span class="subtle">北京时间 ${beijingClock(pricing.beijing_time)}</span></div>
    <p class="pricing-switch">${escapeText(switching)}${pricing.period==='off_peak'?' · 单价优惠 50%':''}</p>
    ${rates?`<div class="price-grid"><div><span>输入 / 未命中</span><strong>¥${escapeText(rates.cache_miss_input)}</strong></div><div><span>输入 / 命中</span><strong>¥${escapeText(rates.cache_hit_input)}</strong></div><div><span>输出</span><strong>¥${escapeText(rates.output)}</strong></div></div><div class="pricing-caption">每百万 tokens · <a href="${escapeText(pricing.source)}" target="_blank" rel="noopener noreferrer" title="${escapeText(pricing.schedule)} 规则核对：${escapeText(pricing.verified_at)}">官方计价 ↗</a></div>`:`<p class="billing-message">${pricing.period==='third_party'?'计价请以对应服务商为准。':'此模型的单价未收录，'} <a href="${escapeText(pricing.source)}" target="_blank" rel="noopener noreferrer">查看官方说明 ↗</a></p>`}
    <div class="balance-heading"><span>余额明细</span><button class="billing-refresh" data-model-action="balance" data-id="${escapeText(model.id)}" ${pending||['unsupported','missing_key'].includes(balance.status)?'disabled':''}>${pending?'刷新中…':'刷新'}</button></div>
    ${balanceContent}${available===false?'<p class="billing-message error">账户当前无可用余额</p>':''}
    ${balance.stale?`<p class="billing-message error">${escapeText(balance.error)} · 当前显示旧余额</p>`:''}
    <div class="balance-footer"><span>${balance.updated_at?'更新于 '+beijingClock(balance.updated_at):'约每分钟同步'}</span></div>
    </div>
  </section>`;
}

function renderModels() {
  const agents=registeredAgents();
  for(const [id,info] of Object.entries(agents))engineNames[id]=info.name||id;
  const choices=Object.entries(agents).map(([id,info])=>`<option value="${escapeText(id)}">${escapeText(info.name||id)}</option>`).join('');
  if($('execution-agent').innerHTML!==choices)$('execution-agent').innerHTML=choices;
  const agentName=engineNames[defaultAgent()]||'执行 Agent';
  $('execution-agent').value=defaultAgent();
  $('agent-state').textContent=agentName+(state.agent?.version?' '+state.agent.version:'')+(state.agent?.ready?' 就绪':' 未就绪');
  $('model-count').textContent=state.models.length;
  $('default-name').textContent=state.models.find(m=>m.id===state.default_model)?.name || '尚未选择';
  $('stat-models').textContent=state.models.filter(m=>m.enabled).length;
  $('model-caption').textContent=`/ ${state.models.length} 个执行端`;
  setMarkup($('models'),state.models.length ? state.models.map(model=>{
    const activity=state.execution_activity?.scope==='global'?state.execution_activity.tasks:state.tasks;
    const jobs=(activity||[]).filter(t=>t.model_id===model.id && ['running','queued','dispatching'].includes(t.status));
    const working=jobs.some(t=>t.status==='running');
    const check=model.last_check;
    const thinking=thinkingInfo(model);
    const connection=working ? badge('running','正在执行') : !model.enabled ? badge('cancelled','已停用') : !check ? badge('unknown','未测试') : check.ok ? badge('online','连接正常') : badge('failed','连接异常');
    let hostname=model.api_base;
    try {hostname=new URL(model.api_base).host;} catch {}
    return `<article class="model-card ${model.id===state.default_model?'is-default':''} ${model.enabled?'':'disabled'} ${model.billing?'billing-model '+(expandedBillingModels.has(model.id)?'is-expanded':'is-collapsed'):''}">
      <div class="model-head"><div class="model-avatar ${model.provider==='local'?'':'cloud'}">${({local:'Q',deepseek:'D',glm:'G',custom:'M'})[model.provider]||'M'}</div><div class="model-title"><strong>${escapeText(model.name)}</strong><div class="provider-label">${escapeText(providerNames[model.provider])}</div></div>${model.id===state.default_model?'<span class="default-badge">默认</span>':''}${model.billing?`<span class="pricing-badge ${model.billing.pricing.period==='peak'?'peak':model.billing.pricing.period==='off_peak'?'off-peak':'unknown'}" title="${escapeText(model.billing.pricing.label)}">${({peak:'峰价',off_peak:'谷价',unknown:'待核实',third_party:'转发计价'})[model.billing.pricing.period]||'待核实'}</span>`:''}</div>
      <div class="model-info"><div class="model-metadata"><div class="model-code">${escapeText(model.model)}</div><div class="model-endpoint" title="${escapeText(model.api_base)}">${escapeText(hostname)}</div><div class="model-thinking" title="${escapeText(thinking.note)}">思考：${escapeText(thinking.label)}</div></div>${renderBilling(model)}</div>
      <div class="model-status">${connection}<span class="subtle">${jobs.length?`全局 ${jobs.length} 个任务`:check?.ok?`${(check.latency_ms/1000).toFixed(1)}s 测试响应`:'暂无任务'}</span></div>
      <div class="model-actions"><button data-model-action="test" data-id="${escapeText(model.id)}" ${testingModels.has(model.id)?'disabled':''}>${testingModels.has(model.id)?'测试中…':'测试连接'}</button><button data-model-action="edit" data-id="${escapeText(model.id)}">编辑</button>${model.id!==state.default_model&&model.enabled?`<button class="set-default" data-model-action="default" data-id="${escapeText(model.id)}">设为默认</button>`:''}<button class="remove" data-model-action="delete" data-id="${escapeText(model.id)}">删除</button></div></article>`;
  }).join('') : '<div class="empty"><strong>还没有执行模型</strong><p>添加一个本地服务或线上模型。</p></div>');
}

function renderExecutionActivity(){
  const box=$('execution-activity'),activity=state.execution_activity;
  if(!box)return;
  const tasks=activity?.scope==='global'&&Array.isArray(activity.tasks)?activity.tasks:[];
  box.hidden=tasks.length===0&&activity?.complete!==false;
  if(box.hidden){setMarkup(box,'');return;}
  const running=tasks.filter(t=>t.status==='running').length;
  setMarkup(box,`<strong>全局执行：${running} 个运行中 · ${tasks.length-running} 个排队/派发中</strong>`+
    (projectView.mode==='unbound'?'<span>尚未选择项目，历史列表为空；全局任务仍在执行，可点击下方项目定位。</span>':'<span>任务列表按当前项目显示，模型运行状态全局共享。</span>')+
    tasks.map(task=>`<div class="activity-job"><span>${escapeText(task.model_name||'执行模型')} · ${escapeText(task.title||task.id)}</span><button type="button" class="text-button" data-activity-project="${escapeText(task.project)}" ${task.project?'':'disabled'}>查看项目：${escapeText(task.project||'未记录目录')}</button></div>`).join('')+
    (activity?.complete===false?'<span>部分任务状态暂时无法读取，以上为已确认状态。</span>':''));
}

function usageWindowLabel(minutes, slot){
  if(minutes===300)return '5 小时';
  if(minutes===10080)return '每周';
  if(minutes===1440)return '每天';
  if(typeof minutes==='number'&&minutes>0)return minutes%60===0?`${minutes/60} 小时`:`${minutes} 分钟`;
  return slot==='primary'?'主要窗口':'次要窗口';
}
function usageResetTime(seconds){
  if(typeof seconds!=='number'||!Number.isFinite(seconds))return '重置时间未提供';
  const date=new Date(seconds*1000);
  if(Number.isNaN(date.getTime()))return '重置时间未提供';
  if(date.getTime()<=Date.now())return '已到重置时间，等待同步';
  const clock=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}).format(date);
  const day=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit'}).format(date);
  const today=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit'}).format(new Date());
  return `${day===today?'今天':day} ${clock} 重置`;
}
function renderUsage(){
  const usage=state.codex_usage;
  $('usage-refresh').disabled=!!usage?.refreshing;
  $('usage-refresh').textContent=usage?.refreshing?'读取中…':'刷新';
  $('usage-status').textContent=usage?.updated_at?`${usage.stale?'上次数据 · ':''}${beijingClock(usage.updated_at)} 更新${usage.refreshing?' · 同步中':''}`:usage?.status==='error'?'暂不可用':'正在读取…';
  const buckets=usage?.buckets||[];
  const rows=buckets.flatMap(bucket=>(bucket.windows||[]).map(window=>({bucket,window})));
  setMarkup($('usage-windows'),rows.length?rows.map(({bucket,window})=>{
    const label=(buckets.length>1?bucket.name+' · ':'')+usageWindowLabel(window.window_minutes,window.slot);
    const percent=typeof window.remaining_percent==='number'&&Number.isFinite(window.remaining_percent)?Math.max(0,Math.min(100,window.remaining_percent)):null;
    const shown=percent===null?'未提供':new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(percent)+'%';
    return `<div class="usage-item"><div class="usage-values"><span class="usage-label">${escapeText(label)}</span><strong>${percent===null?'剩余未知':'剩余 '+escapeText(shown)}</strong></div>${percent===null?'':`<progress value="${percent}" max="100" aria-label="${escapeText(label)}剩余额度">${escapeText(shown)}</progress>`}<span class="usage-reset" title="北京时间">${escapeText(usageResetTime(window.resets_at))}</span></div>`;
  }).join(''):`<span class="subtle">${usage?.status==='error'?'额度读取失败':usage?.status==='ready'?'账号暂未返回额度窗口':'正在查询账号额度…'}</span>`);
  $('usage-error').hidden=!usage?.error;
  $('usage-error').textContent=usage?.error||'';
}
// Global execution-model scope, mirrored from the backend enum.  Unknown or
// missing values fall back to 'all' so an older panel state can never hide a
// local model or look like a filter the backend did not apply.
const MODEL_SCOPE_VALUES=['all','paid_only','local_only'];
const MODEL_SCOPE_LABELS={all:'全部模型（兼容默认）',paid_only:'仅付费模型',local_only:'仅本地/自建模型'};
function scopeValue(){const value=state.workflow?.model_scope;return MODEL_SCOPE_VALUES.includes(value)?value:'all';}
function scopeLabel(value=scopeValue()){return MODEL_SCOPE_LABELS[value]||MODEL_SCOPE_LABELS.all;}
function modelInScope(model,value=scopeValue()){return value==='all'||(value==='paid_only'?model?.provider!=='local':model?.provider==='local');}
function inScopeModels(models){return (models||[]).filter(model=>modelInScope(model));}
function scopeHintText(value=scopeValue()){
  if(value==='paid_only')return '当前只允许付费模型；不会自动开启付费授权，也不会绕过余额、金额与请求次数上限。';
  if(value==='local_only')return '当前只允许本地/自建模型；不会回退或调用任何付费候选。';
  return '当前允许全部已启用的执行模型；这是不选择新范围时的原有行为。';}
function renderWorkflow(){
  const w=state.workflow||{enabled:false,paid_allowed:false};
  if(!$('workflow-toggle').disabled)$('workflow-toggle').checked=!!w.enabled;
  $('workflow-mode').textContent=w.enabled?'已开启 · 自动路由':'已关闭 · Codex 默认';
  $('workflow-description').textContent=w.enabled?'GPT 定目标与独立验收，执行 Agent 自主调查、设计、实现和测试。':'Codex 按默认方式工作；手动分派仍可使用。';
  const d=w.daily||{};
  const settled=settledCost(d.settled_cny,!!d.has_actual);
  const extra=[];
  const unsettled=Number(d.unsettled_cny)||0;
  if(unsettled>1e-9)extra.push(`未结算预留 ${money(unsettled)}`);
  if(Number(d.unknown_calls)>0)extra.push(`待核对 ${d.unknown_calls} 次`);
  if(d.unresolved&&w.enforce_paid_budget!==false)extra.push('费用待核对 · 暂停付费请求');
  const extraText=extra.length?' · '+extra.join(' · '):'';
  const callText=w.enforce_model_call_limit===false?'请求次数不限':`最多 ${w.max_model_calls} 次请求`;
  $('budget-summary').textContent='全局预算 · '+(w.paid_allowed?(w.enforce_paid_budget===false?`金额上限关闭 · 今日按用量计算费用 ${settled}${extraText} · ${callText}`:`今日按用量计算费用 ${settled} / ${money(w.daily_budget_cny)}${extraText} · 每任务 ${money(w.task_budget_cny)}`):'付费执行关闭 · 本地优先');
  const scopeText='执行范围 · '+scopeLabel();
  const scopeSummary=$('workflow-scope-summary');if(scopeSummary){scopeSummary.textContent=scopeText;scopeSummary.title=`全局执行模型范围：${scopeLabel()}`;}
  const totals=state.delegation;$('delegation-stats').hidden=!totals;$('delegation-stats').textContent=totals?`委派 ${totals.delegated} · 待验收 ${totals.executor_completed} · 通过 ${totals.accepted} · 退回 ${totals.returned}`:'';
  const error=w.error||w.daily?.error||'';$('workflow-error').textContent=error;$('workflow-error').hidden=!error;
  $('task-route').querySelector('[value="auto"]').disabled=!w.enabled;
  if(!continuingTask&&$('task-dialog').open&&!w.enabled&&$('task-route').value==='auto'){$('task-route').value='manual';routeFields();}
}
// ------------------------------------------------ goals / stages / obs tree
const goalStatusClass = status => ({active:'running',completed:'completed',cancelled:'cancelled'}[status]||'queued');
const stageStatusClass = status => ({accepted:'completed',final_accepted:'completed',running:'running',dispatched:'running',ready:'online',pending:'queued',executor_completed:'queued',returned:'failed',blocked:'failed',failed:'failed',cancelled:'cancelled',recovery_pending:'queued'}[status]||'queued');
const taskStatusClass = status => ({completed:'completed',running:'running',queued:'queued',failed:'failed',cancelled:'cancelled'}[status]||'queued');
const reviewStatusClass = state => (['accepted','final_accepted'].includes(state)?'completed':state==='returned'||state==='blocked'?'failed':'queued');
const numberText = value => (value===null||value===undefined||Number.isNaN(Number(value)))?'未取得':Number(value).toLocaleString('zh-CN');
// Local appearance/observation only: no percentage of a "quota saved" is invented.
function renderObservability(taskId){
  const obs=state.observability?.[taskId];
  if(!obs)return '<p class="obs-unknown">正在读取真实观测记录…</p>';
  const cost=obs.cost||{},usage=obs.provider_usage||{},meter=obs.meter||{},context=obs.context||{},failure=obs.failure||{},forecast=obs.forecast||{},boundary=obs.billing_boundary||{};
  const settled=cost.known?money(cost.settled_cny):'待核对';
  const unsettled=(cost.unsettled_cny===null||cost.unsettled_cny===undefined)?'未取得':money(cost.unsettled_cny);
  const usageText=usage.available?`输入 ${numberText(usage.input_tokens)} · 缓存命中 ${numberText(usage.cache_hit_tokens)} · 缓存未命中 ${numberText(usage.cache_miss_tokens)} · 输出 ${numberText(usage.output_tokens)} · 合计 ${numberText(usage.total_tokens)}`:'未取得（服务商未返回 usage）';
  const usageScope=usage.available?(usage.partial?`已返回部分用量：另有 ${numberText(usage.unknown_calls)} 次请求未返回 usage（未知），不是全部调用或整个目标的总 tokens`:(usage.scope||'')):'';
  const costUnknownNote=cost.known?'':'金额未知 · 待核对不是 ¥0.00 · 预留只是审计，不是真实费用';
  const windowVerified=meter.window_verified===true?'是':meter.window_verified===false?'否':'未取得';
  const interval=forecast.retry_interval?`重试区间 ${escapeText(forecast.retry_interval.low)}–${escapeText(forecast.retry_interval.high)}（${escapeText(forecast.retry_interval.method||'方法未标')} · 样本 ${numberText(forecast.retry_samples)} · 置信 ${escapeText(forecast.retry_confidence||'未标')}）`:'未取得';
  const prediction=(forecast.estimated_cost_cny===null||forecast.estimated_cost_cny===undefined)?'未取得':money(forecast.estimated_cost_cny);
  const single=forecast.single_request_output?`任务请求目标 ${numberText(forecast.single_request_output.task_target)} · 模型最大输出 ${numberText(forecast.single_request_output.model_ceiling)}`:'未取得';
  const childAggregate=boundary.children?.aggregate?`聚合已结算 ${money(boundary.children.aggregate.settled_cny)}（${numberText(boundary.children.aggregate.settled_calls)} 次）`:'未取得';
  const scope=obs.file_scope||{};
  const scopeFiles=(scope.readonly_files||scope.files||[]).map(escapeText).join('、')||'未记录';
  const scopeText=scope.mode==='readonly_context'?`只读上下文：${scopeFiles} · 只读权限由 kind/engine 沙箱保证，不是可修改文件`:scope.mode==='modifiable'?`可修改：${scopeFiles}`:'未记录文件范围';
  return `<div class="obs-grid">
    <div><span class="label">已结算费用</span><span class="value">${settled} · ${numberText(cost.settled_calls)} 次</span></div>
    <div><span class="label">未知 / 未结算</span><span class="value">待核对 ${numberText(cost.unknown_calls)} 次 · 未结算预留（审计）${unsettled}${costUnknownNote?` · ${escapeText(costUnknownNote)}`:''}</span></div>
    <div><span class="label">provider 实际用量</span><span class="value">${usageText}${usageScope?` · ${escapeText(usageScope)}`:''}</span></div>
    <div><span class="label">计数来源 / 可信度 / 窗口</span><span class="value">${escapeText(meter.source||'未取得')} · ${escapeText(meter.confidence||'unavailable')} · ${escapeText(meter.window_kind||'窗口未验证')} · 语义已核实：${windowVerified}</span></div>
    <div><span class="label">失败分类</span><span class="value">${escapeText(failure.label||'unknown')} · ${failure.counts_as_model_quality?'计入模型质量':'不计入模型质量'}（来源 ${escapeText(failure.source||'未取得')}）</span></div>
    <div><span class="label">预测区间样本</span><span class="value">${interval} · 预测费用 ${prediction}</span></div>
    <div><span class="label">单请求输出目标</span><span class="value">${single}</span></div>
    <div><span class="label">任务文件范围</span><span class="value">${scopeText}</span></div>
    <div><span class="label">工具字节 / 外置</span><span class="value">before ${numberText(context.bytes_before)} → after ${numberText(context.bytes_after)} · 外置 ${numberText(context.offloaded_count)} 个 / ${numberText(context.offloaded_bytes)} 字节 · 预览 ${numberText(context.preview_bytes)}</span></div>
    <div><span class="label">父子账单边界</span><span class="value">per-child ${escapeText(boundary.children?.per_child||'unavailable')} · ${childAggregate}</span></div>
  </div>
  <p class="field-hint">${escapeText(context.note||'context_* 是字节审计，不等于 token 精确节省')}</p>
  ${(meter.warnings||[]).length?`<p class="obs-unknown">计量提示：${escapeText(meter.warnings.join('；'))}</p>`:''}`;
}
async function loadObservability(id){
  if(!id||state.observability?.[id]||observabilityLoading.has(id))return;
  observabilityLoading.add(id);
  try{state.observability[id]=await api(`/api/tasks/${encodeURIComponent(id)}/observability`);if(selectedTask===id)renderDetail();if(state.tree)renderGoals();}
  catch(error){/* keep it explicitly unknown; never fabricate numbers */}
  finally{observabilityLoading.delete(id);}
}
function renderChildren(task){
  const summary=task.children_state||{},rows=task.children||[],capability=summary.capability||{};
  const reasons=(capability.reasons||[]).filter(Boolean);
  return `<div class="tree-facts"><span>状态：${escapeText(summary.state||'未验证')}</span><span>已核实 ${numberText(summary.verified||0)} · 未核实 ${numberText(summary.unverified||0)}</span><span>per-child 账单：${escapeText(summary.per_child_billing||'unavailable')}</span></div>${reasons.length?`<p class="obs-unknown">${escapeText(reasons.join('；'))}</p>`:''}${rows.length?`<ul class="child-list">${rows.map(child=>`<li class="child-row ${child.verified?'':'unverified'}"><div class="tree-facts"><span>child ${escapeText(child.child_id)}</span><span>状态：${escapeText(child.state||'')}</span><span>provider：${escapeText(child.provider||'—')}</span><span>${escapeText(child.local_flag_note||(child.local===true?'local=true（进程内 provider，非免费本地 API 模型）':`local=${String(child.local)}`))}</span><span>停止：${escapeText(child.stop_reason||'未结束')}</span></div><div class="tree-facts"><span>模型/提供商以真实 registry 路由为准：${escapeText(child.model_name||child.model||child.model_id||'—')}</span><span>费用：${escapeText(child.cost)}</span></div></li>`).join('')}</ul>`:'<p class="obs-unknown">没有真实子代理生命周期记录；未验证时不显示伪造的子代理树或费用。</p>'}`;
}
function renderDelivery(entry){
  const fresh=entry.fresh||{};
  const files=(entry.files||[]).map(file=>`<span>${escapeText(file.path)} · ${escapeText(String(file.sha256||'').slice(0,12)||'无 hash')}</span>`).join('')||'<span class="obs-unknown">未记录文件</span>';
  return `<div class="child-row"><div class="tree-facts"><span>任务 ${escapeText(entry.task_id)}</span><span>交接有效：${fresh.valid?'是':fresh.stale?'已失效':'未知'}</span></div><div>${escapeText(entry.summary||'无摘要（摘要不是验证证据）')}</div><div class="tree-facts">${files}</div><div class="tree-facts"><span>${escapeText(fresh.message||'')}</span></div></div>`;
}
function renderTreeTask(task){
  const reviewState=task.review_state;
  const displayStatus=taskDisplayStatus({...task,review:{state:reviewState}});
  const accepted=displayStatus==='completed'&&['accepted','final_accepted'].includes(reviewState);
  const review=reviewState?`<div class="tree-facts"><span>验收（真实 review.json）：<strong>${escapeText(task.review_label||reviewState)}</strong></span><span>执行器完成不等于验收通过</span></div>`:task.review_error?`<p class="obs-unknown">验收记录无法读取，不能当作已验收：${escapeText(task.review_error)}</p>`:'<p class="obs-unknown">尚未记录独立验收结论；执行器完成不代表验收通过。</p>';
  const treeFiles=(task.files||[]).filter(Boolean);
  const fileScope=treeFiles.length?`<div class="tree-facts"><span>文件范围（${task.files_readonly?'只读上下文 · chat':'可修改 · edit'}）：${treeFiles.map(escapeText).join('、')}</span>${task.files_readonly?'<span>只读权限由 kind/engine 沙箱保证，不是可修改文件</span>':''}</div>`:'';
  return `<details class="task-node" data-task="${escapeText(task.task_id)}"${treeOpen.has('task:'+task.task_id)?' open':''}>
    <summary><span class="task-title">${escapeText(task.title||task.task_id)}</span>${badge(taskStatusClass(displayStatus),accepted?'已完成':task.status)}${reviewState?badge(reviewStatusClass(reviewState),task.review_label||reviewState):''}<span class="task-meta">${escapeText(task.model_name||task.model||task.model_id||'—')} · ${escapeText(task.kind||'—')}</span></summary>
    <div class="task-body">
      <div class="tree-facts"><span>任务 ${escapeText(task.task_id)}</span><span>${escapeText(engineNames[task.engine]||'执行器')} 会话：${escapeText(task.pi_session_id||task.dsh_session_id||'未记录')}</span><span>会话标识：${escapeText(task.dsh_session_key||'—')}</span><span>父任务：${escapeText(task.parent_task_id||'—')}</span>${task.request_output_tokens?`<span>请求输出目标：${escapeText(task.request_output_tokens)} tokens</span>`:''}</div>
      ${review}
      ${fileScope}
      <details class="obs-sub"><summary>可信观测（费用/用量/计量/上下文/失败/预测）</summary>${renderObservability(task.task_id)}</details>
      <details class="obs-sub"><summary>子代理（${escapeText((task.children_state||{}).state||'未验证')}）</summary>${renderChildren(task)}</details>
      <details class="obs-sub"><summary>改动文件与代码 hash（${Object.keys(task.file_hashes||{}).length}）</summary><div class="tree-facts">${Object.entries(task.file_hashes||{}).map(([name,hash])=>`<span>${escapeText(name)} · ${escapeText(String(hash).slice(0,12))}</span>`).join('')||'<span class="obs-unknown">未记录</span>'}</div></details>
    </div></details>`;
}
function renderStage(stage){
  const deps=stage.depends_on||[],readiness=stage.readiness||{},delivery=stage.delivery||{deliveries:[],warnings:[]};
  const dependencyStates=readiness.dependency_states||{};
  const canCancel=!['accepted','final_accepted','cancelled'].includes(stage.status);
  const canRequeue=['failed','blocked','recovery_pending','cancelled','executor_completed'].includes(stage.status);
  return `<details class="stage-node" data-stage="${escapeText(stage.id)}"${treeOpen.has('stage:'+stage.id)?' open':''}>
    <summary><span class="stage-title">${escapeText(String(stage.ordinal||''))}. ${escapeText(stage.title)}</span>${badge(stageStatusClass(stage.status),stage.status_label||stage.status)}<span class="stage-meta">${escapeText(stage.kind_label||stage.kind||'')}</span><span class="stage-meta">依赖 ${deps.length}</span></summary>
    <div class="stage-body">
      <div class="tree-facts"><span>目标：${escapeText(stage.objective||'—')}</span><span>会话策略：${escapeText(stage.session_policy==='reuse_session'?'显式复用会话':'新会话')}</span><span>会话标识：<strong>${escapeText(stage.session_key||'未建立')}</strong></span></div>
      <details class="obs-sub"><summary>前置依赖与就绪（${readiness.ready?'就绪':'未就绪'}）</summary><div class="tree-facts"><span>${escapeText((readiness.reasons||[]).join('；')||'无阻塞')}</span><span>依赖状态：${escapeText(deps.map(dep=>`${dep}=${dependencyStates[dep]||'未知'}`).join('，')||'无')}</span></div></details>
      <details class="obs-sub"><summary>成果交接与代码 hash（${delivery.deliveries.length} 份）</summary>${delivery.deliveries.length?delivery.deliveries.map(renderDelivery).join(''):'<p class="obs-unknown">尚无已独立验收的成果交接。</p>'}${(delivery.warnings||[]).map(item=>`<p class="obs-unknown">${escapeText(JSON.stringify(item))}</p>`).join('')}</details>
      <div class="tree-actions"><button data-stage-action="dispatch" data-id="${escapeText(stage.id)}">派发</button>${canCancel?`<button data-stage-action="cancel" data-id="${escapeText(stage.id)}">取消阶段</button>`:''}${canRequeue?`<button data-stage-action="requeue" data-id="${escapeText(stage.id)}">恢复/重新排队</button>`:''}<button data-stage-action="sync" data-id="${escapeText(stage.id)}">同步</button></div>
      ${(stage.tasks||[]).length?stage.tasks.map(renderTreeTask).join(''):'<p class="goal-meta">还没有关联任务。</p>'}
    </div></details>`;
}
function renderGoal(goal){
  const stages=goal.stages||[];
  const actions=[`<button data-goal-action="stage" data-id="${escapeText(goal.id)}">＋ 新建阶段</button>`];
  if(goal.status==='active')actions.push(`<button data-goal-action="complete" data-id="${escapeText(goal.id)}">标记完成</button>`,`<button data-goal-action="cancel" data-id="${escapeText(goal.id)}">取消目标</button>`);
  return `<details class="goal-node" data-goal="${escapeText(goal.id)}"${treeOpen.has('goal:'+goal.id)?' open':''}>
    <summary><span class="goal-title">${escapeText(goal.title)}</span>${badge(goalStatusClass(goal.status),goal.status_label||goal.status)}<span class="goal-meta">阶段 ${goal.accepted_count||0}/${goal.stage_count||0}</span>${goal.project?`<span class="goal-meta">${escapeText(goal.project)}</span>`:''}</summary>
    <div class="goal-body">
      ${goal.objective?`<p class="goal-meta">${escapeText(goal.objective)}</p>`:''}
      <div class="tree-actions">${actions.join('')}</div>
      ${stages.length?stages.map(renderStage).join(''):'<p class="goal-meta">这个目标还没有阶段。</p>'}
    </div></details>`;
}
function renderGoals(){
  const tree=state.tree;
  if(!tree){setMarkup($('goals-tree'),state.treeError?`<p class="obs-unknown">目标/阶段接口暂不可用：${escapeText(state.treeError)}（面板服务加载新后端后可用；这里不会自动重启服务）。</p>`:'<p class="subtle">正在读取目标与阶段…</p>');return;}
  const stats=tree.stats||{},goals=tree.goals||[];
  $('goal-count').textContent=goals.length;
  $('goals-stats').textContent=`阶段 ${numberText(stats.stages||0)} · 已验收 ${numberText(stats.accepted||0)} · 待恢复 ${numberText(stats.recovery_pending||0)}`;
  $('goals-error').hidden=!tree.error;
  $('goals-error').textContent=tree.error||'';
  const scroll=$('goals-tree').scrollTop;
  setMarkup($('goals-tree'),goals.length?goals.map(renderGoal).join(''):'<div class="empty"><strong>暂无目标</strong><p>创建一个目标，再添加只读调查或实现阶段；全部阶段独立验收通过后才能标记目标完成。</p></div>');
  $('goals-tree').scrollTop=scroll;
}
function openAction(config){
  pendingAction=config;
  $('action-title').textContent=config.title||'确认操作';
  $('action-message').textContent=config.message||'';
  $('action-confirm').textContent=config.confirm||'确认';
  $('action-reason').value='';
  $('action-reason').placeholder=config.placeholder||'';
  $('action-reason-field').hidden=!config.field;
  $('action-reason').required=!!config.required;
  $('action-error').textContent='';
  $('action-dialog').showModal();
}
function openStage(goalId,goalName){
  stageGoal=goalId;
  $('stage-form').reset();
  $('stage-goal').value=goalId;
  $('stage-goal-name').textContent=goalName||goalId;
  $('stage-error').textContent='';
  $('stage-dialog').showModal();
}
function updateTaskOutputHint(){
  const automatic=$('task-route').value==='auto';
  const model=state.models.find(item=>item.id===$('task-model').value);
  if(automatic){$('task-output-hint').textContent='自动路由在派发时选择模型；模型最大输出与上下文窗口以实际选中模型为准，请求输出目标只约束本次请求。';return;}
  if(!model){$('task-output-hint').textContent='选择模型后显示其最大输出与上下文窗口；请求输出目标只约束本次请求。';return;}
  $('task-output-hint').textContent=`${model.name}：模型最大输出 ${numberText(model.max_output_tokens)} tokens · 上下文窗口 ${numberText(model.max_input_tokens)} tokens；请求输出目标只约束本次请求，不修改模型预设。`;
}
function renderSubagentCapability(model){
  const rows=(state.executor_capabilities&&state.executor_capabilities.models)||[];
  const row=model?rows.find(item=>item.model_id===model.id):null;
  const summary=$('model-subagent-summary'),detail=$('model-subagent-detail');
  if(!detail||!summary)return;
  if(!model){summary.textContent='新模型保存后按同身份实测证据显示；未验证时不可允许。';detail.innerHTML='';return;}
  if(!row){summary.textContent='面板尚未返回该模型的实测能力记录，按未验证处理。';detail.innerHTML='';return;}
  const stateNames={supported:'支持（同身份实测）',unsupported:'不支持',unverified:'未验证'};
  const policyNames={allow:'允许派生',deny:'禁止派生',unset:'未设置（默认禁止）'};
  const evidence=row.evidence_detail||{};
  const shownState=stateNames[row.state]||row.state||'未验证';
  const shownPolicy=policyNames[row.policy]||row.policy||'unset';
  summary.textContent=`支持状态：${shownState} · 允许派生策略：${shownPolicy} · 是否可允许：${row.allowable?'可允许':'不可允许'}`;
  if((row.reasons||[]).length)summary.textContent+=` · 原因：${row.reasons.join('；')}`;
  const fields=[
    ['支持状态（实测）',shownState],
    ['允许派生（用户策略）',shownPolicy],
    ['是否可允许（真实后端能力）',row.allowable?'是':'否'],
    ['已证实模式',(row.proven_modes||[]).length?row.proven_modes.join('、'):'未实测'],
    ['执行器链路',row.backend_implemented?'DSH 已实现子代理接口':`执行器 ${row.executor||'—'} 未实现能力接口`],
    ['本地/home',row.local?'禁止派生（仍可作为 worker 执行）':'否'],
    ['作为 worker',row.worker_usable?'启用':'未启用'],
    ['证据实测时间',evidence.tested_at||'未取得'],
    ['证据执行器版本',evidence.version||'未取得'],
    ['证据模型版本',evidence.model_version||'未取得'],
    ['证据来源',evidence.source||'未取得'],
    ['证据编号',evidence.test_id||'未取得'],
    ['策略更新时间',row.evidence_updated_at||row.updated_at||'未取得'],
  ];
  detail.innerHTML=fields.map(([key,value])=>`<div><dt>${escapeText(key)}</dt><dd>${escapeText(value)}</dd></div>`).join('');
}
function completionWaitText(task, nowMs=Date.now()) {
  const wait=task.completion_wait;
  if(!wait)return '';
  const review=task.review?.state;
  if(['accepted','final_accepted'].includes(review))return '任务已完成 · 独立验收通过';
  if(review==='returned')return '已退回整改';
  if(review==='blocked')return '需要处理 · 独立验收受阻';
  if(['failed','cancelled','recovery_pending'].includes(task.status))return ({failed:'执行失败，待处理',cancelled:'执行已取消',recovery_pending:'任务待恢复，未自动重发'})[task.status];
  if(['host_rejected','delivery_unknown','route_refused','version_changed'].includes(wait.listener_status))return '通知未确认 · 请检查原任务';
  if(wait.listener_status==='stopped')return '自动通知已暂停 · 执行任务未被取消';
  const heartbeat=Date.parse(wait.heartbeat_at||'');
  if(['starting','listening','waiting_chat_idle'].includes(wait.listener_status)&&(!Number.isFinite(heartbeat)||nowMs-heartbeat>30000))return task.status==='completed'?'执行已结束 · 通知状态未确认':'等待状态未确认 · 请检查后台监听';
  if(wait.phase==='reviewing')return '执行已结束 · GPT 正在独立验收';
  if(task.status==='completed')return wait.phase==='waiting_chat_idle'?'执行已结束 · 等待通知':'执行已结束 · 待 GPT 独立验收';
  if(wait.phase==='needs_attention')return '需要处理 · 请检查原任务';
  return '正在等待执行模型完成';
}
function renderTasks() {
  $('stat-running').textContent=state.tasks.filter(t=>t.status==='running').length;
  $('stat-queued').textContent=state.tasks.filter(t=>['queued','recovery_pending'].includes(taskDisplayStatus(t))).length;
  $('stat-done').textContent=state.tasks.filter(t=>taskDisplayStatus(t)==='completed').length;
  const tasks=state.tasks.filter(task=>taskFilter==='all'||(taskFilter==='active'?['running','queued','recovery_pending'].includes(taskDisplayStatus(task)):taskFilter==='failed'?['failed','cancelled'].includes(taskDisplayStatus(task)):taskDisplayStatus(task)===taskFilter));
  const scroll=$('task-list').scrollTop;
  setMarkup($('task-list'),tasks.length ? tasks.map(task=>`<button class="task-row ${escapeText(taskDisplayStatus(task))} ${selectedTask===task.id?'selected':''}" data-task-id="${escapeText(task.id)}">
    <span class="task-icon" aria-hidden="true">${({completed:'✓',running:'↗',queued:'◷',failed:'!',cancelled:'−',recovery_pending:'◷'})[taskDisplayStatus(task)]||'·'}</span>
    <div><div class="task-title">${escapeText(task.title)}</div><div class="task-meta"><span>${escapeText(task.model_name||task.model)}</span><span>·</span><span>${task.source==='codex'?'Codex 分派':task.source==='panel'?'手动分派':'命令行'}</span><span>·</span><span>${escapeText(taskDisplayPhase(task))}</span>${task.completion_wait?`<span>· ${escapeText(completionWaitText(task))}</span>`:''}</div></div>
    <span class="task-right">${badge(taskDisplayStatus(task),taskAccepted(task)&&taskDisplayStatus(task)==='completed'?'已完成':undefined)}${task.review?badge(reviewStatusClass(task.review.state),task.review.label):''}<small>${escapeText(duration(task))}</small></span></button>`).join('') : (()=>{const empty=projectEmptyState();return `<div class="empty"><div class="empty-symbol">✧</div><strong>${escapeText(empty.title)}</strong><p>${escapeText(empty.body)}</p></div>`;})());
  $('task-list').scrollTop=scroll;
}
function renderDetail() {
  const task=state.tasks.find(t=>t.id===selectedTask);
  const oldScroll=$('detail-content')?.scrollTop || 0;
  if (!task) { setMarkup($('task-detail'),'<div class="empty"><strong>任务详情</strong><p>选择一个任务，查看回复、文件改动和执行日志。</p></div>'); return; }
  const active=['queued','running'].includes(task.status);
  const independentlyAccepted=taskAccepted(task)&&taskDisplayStatus(task)==='completed';
  const pendingRecovery=task.status==='recovery_pending'&&!independentlyAccepted;
  const displayPhase=taskDisplayPhase(task)||statusNames[task.status];
  const phaseIndex=task.status==='queued'?0:task.status==='running'?1:2;
  const phases=['已分派',task.kind==='chat'?'模型生成':'模型执行',independentlyAccepted?'独立验收通过':task.status==='failed'?'执行失败':task.status==='cancelled'?'已停止':pendingRecovery?'待恢复（未自动重发）':task.kind==='chat'?'回复完成':'结果待验收'];
  const usage=task.usage_live?.usage||task.usage;
  const thinking=thinkingInfo(state.models.find(m=>m.id===task.model_id),task);
  setMarkup($('task-detail'),`<div class="detail-head"><div><h3>${escapeText(task.title)}</h3><p>${escapeText(task.model_name)} · ${escapeText(engineNames[task.engine]||'历史执行器')} · ${task.kind==='chat'?'咨询 / 分析':'修改项目文件'} · ${clock(task.started_at||task.created_at)}</p></div>${active?`<button class="button secondary" id="stop-task">停止任务</button>`:pendingRecovery?'<button class="button primary" id="recover-task">恢复执行（需确认）</button>':hasAgentSession(task)?'<button class="button secondary" id="continue-task">继续会话</button>':badge(taskDisplayStatus(task),independentlyAccepted?'已完成':undefined)}</div>
    <div class="detail-body"><div class="phase-track">${phases.map((phase,i)=>`${i?'<span class="phase-arrow">→</span>':''}<span class="phase-item ${i<phaseIndex?'done':i===phaseIndex?'current':''}">${escapeText(phase)}</span>`).join('')}</div>
    <div class="detail-facts"><div><span class="label">当前状态</span><span class="value">${escapeText(displayPhase)} · ${escapeText(duration(task))}</span></div><div><span class="label">${task.kind==='chat'?'Token 用量':'项目文件夹'}</span><span class="value">${task.kind==='chat'?(usage?.total_tokens?`${usage.total_tokens.toLocaleString()} tokens${task.cost_unknown_calls>0?`（另有 ${task.cost_unknown_calls} 次未返回用量，未知；不是全目标总量）`:''}`:'服务端尚未返回用量'):escapeText(task.project||'—')}</span></div></div>
    ${task.completion_wait?`<p class="session-note" role="status">${escapeText(completionWaitText(task))}</p>`:''}
    ${independentlyAccepted&&task.status==='recovery_pending'?'<p class="session-note">原执行器曾中断，历史记录予以保留；本任务已由主脑接管完成并通过独立验收，无需恢复原执行。</p>':''}
    ${pendingRecovery?`<section class="route-note recovery-note" aria-label="待恢复"><strong>待恢复 · 不会自动重发</strong>${task.recovery?.reason?`<p>原因：${escapeText(task.recovery.reason)}</p>`:''}<p class="field-hint">执行是否完成、工具副作用与实际费用都未知；原任务、模型、项目、文件范围与已记录进度都保留。核对改动影响后，点“恢复执行（需确认）”会用<b>同一个任务记录</b>继续（不创建新记录，也不会自动验收）。</p></section>`:''}
    ${task.review?`<section class="route-note" aria-label="独立验收"><strong>${escapeText(task.review.label)}</strong><small>自动整改 ${task.review.rework_count??'—'} / ${task.review.max_auto_rework??1} 次</small>${task.review.problems?`<p>退回原因：${escapeText(task.review.problems)}</p>`:''}${task.review.blocked_reason?`<p>受阻原因：${escapeText(task.review.blocked_reason)}</p>`:''}${task.review.error?`<p>${escapeText(task.review.error)}</p>`:''}${task.review.reviewer_notes?`<p>${escapeText(task.review.reviewer_notes)}</p>`:''}${task.review.criteria?.length?`<ul>${task.review.criteria.map(c=>`<li>${escapeText(c.text)} · ${escapeText({pass:'通过',fail:'未通过',unknown:'待检查'}[c.status]||c.status)}</li>`).join('')}</ul>`:''}</section>`:!active?'<p class="session-note">尚未记录独立验收结论；执行状态不代表质量验收。</p>':''}
    ${task.kind==='edit'&&task.changed_files?.length?`<div><span class="label">已修改文件</span><div class="chips">${task.changed_files.map(f=>`<span class="chip">${escapeText(f)}</span>`).join('')}</div></div>`:''}
    ${task.kind==='chat'&&task.files?.length?`<div><span class="label">只读上下文文件</span><div class="chips">${task.files.map(f=>`<span class="chip">${escapeText(f)}</span>`).join('')}</div><p class="field-hint">这些文件用于代码索引与交接文件范围；只读权限由 kind/engine 沙箱保证，不是可修改文件。</p></div>`:''}
    ${task.route?`<div class="route-note"><strong>${task.route.mode==='auto'?'自动路由':'手动指定'}</strong> · ${escapeText(task.route.reason)}<small>${task.route.requirements?.assessment?escapeText(task.route.requirements.assessment)+' · ':''}${task.model_calls||0} 次模型请求 · ${taskCostText(task)}${task.route.budget_cny?' / '+money(task.route.budget_cny):''}${task.budget_error?' · '+escapeText(task.budget_error):''}</small></div>`:''}
    ${task.engine==='pi'?`<p class="session-note">Pi Agent ${escapeText(task.executor_version||'')} · 记录目录保护：${task.record_protection?'已验证':'待验证'}</p>`:''}
    ${task.engine==='dsh'?`<p class="session-note">Agent：${escapeText(presetNames[task.agent_preset]||task.agent_preset||'历史配置')} · ${task.preset_verified?'实际绑定已验证':task.agent_preset?'等待执行验证':'预设未记录'} · <span title="${escapeText(thinking.note)}">思考等级：${escapeText(thinking.label)}</span> · 权限：${escapeText({'danger-full-access':'完全权限','workspace-write':'项目内写入','read-only':'只读'}[task.sandbox_mode]||'历史任务未记录')}</p>`:''}
    ${(task.pi_session_id||task.dsh_session_id)?`<p class="session-note">${task.parent_task_id?'已续接原会话':'独立会话'} · ${escapeText(task.pi_session_id||task.dsh_session_id)}</p>`:''}
    ${task.tool_calls?.length?`<div class="tool-timeline" aria-label="模型工具调用">${task.tool_calls.slice(-8).map(tool=>`<div><span class="tool-state ${escapeText(tool.status)}">${({completed:'✓',failed:'!',in_progress:'↻',pending:'·'})[tool.status]||'·'}</span><span>${escapeText(({read:'读取',write:'写入',edit:'修改',grep:'搜索',glob:'查找文件',pwsh:'执行命令'})[tool.title]||tool.title||tool.kind||'工具调用')}${tool.file?' · '+escapeText(tool.file):''}</span><small>${({completed:'已完成',failed:'失败',in_progress:'执行中',pending:'等待'})[tool.status]||tool.status||''}</small></div>`).join('')}</div>`:''}
    <details class="obs-sub" id="detail-observability"${treeOpen.has('detail-obs:'+task.id)?' open':''}><summary>可信观测（费用 / 用量 / 计量 / 上下文 / 失败 / 预测 / 子代理）</summary><div class="obs-body">${renderObservability(task.id)}</div><details class="obs-sub"><summary>子代理（${escapeText(((state.observability?.[task.id]||{}).children||{}).state||'未验证')}）</summary>${renderChildren({children_state:(state.observability?.[task.id]||{}).children,children:(state.observability?.[task.id]||{}).child_rows||[]})}</details></details>
    <div class="detail-tabs"><button class="detail-tab ${detailTab==='result'?'active':''}" data-tab="result">${task.kind==='chat'?'模型回复':'执行结果'}</button><button class="detail-tab ${detailTab==='prompt'?'active':''}" data-tab="prompt">任务内容</button><button class="detail-tab ${detailTab==='log'?'active':''}" data-tab="log">执行日志</button></div>
    <pre id="detail-content" class="detail-content ${detailTab==='log'?'log':''}"></pre>${active?'<div class="waiting-line"><span class="pulse"></span><span>正在同步执行状态，暂不提供完成百分比。</span></div>':''}</div>`);
  let text=detailTab==='prompt'?(promptTask===task.id?detailPrompt:'正在读取任务…'):detailTab==='log'?'正在读取日志…':task.kind==='chat'?(task.answer||task.error||'等待模型返回正文…'):(task.summary||task.error||'等待执行结果…');
  $('detail-content').textContent=text;
  $('detail-content').scrollTop=oldScroll;
  if (detailTab==='log') loadLog(task.id,oldScroll);
  if (promptTask!==task.id) loadPrompt(task.id);
  loadObservability(task.id);
}
async function loadPrompt(id) {
  try { const data=await api(`/api/tasks/${encodeURIComponent(id)}`); if(selectedTask!==id)return; detailPrompt=data.prompt;promptTask=id;if(detailTab==='prompt')$('detail-content').textContent=detailPrompt; } catch(error){if(selectedTask===id&&detailTab==='prompt')$('detail-content').textContent=error.message;}
}
async function loadLog(id,scroll) {
  try {const data=await api(`/api/tasks/${encodeURIComponent(id)}/log`);if(selectedTask!==id||detailTab!=='log')return;$('detail-content').textContent=data.text||'日志尚未产生…';$('detail-content').scrollTop=scroll;}catch(error){if(selectedTask===id&&detailTab==='log')$('detail-content').textContent=error.message;}
}
// ---------- polling lifecycle (one chain, one request per document) ----------
// The panel stays open for a long time, so its steady state must not keep
// fetching and rebuilding markup: a hidden document does not poll at all, a
// settled document polls slowly, and a document with queued or running work
// keeps the previous short interval so execution stays as visible as before.
// The idle interval stays short enough that work started outside the panel is
// still picked up on the next idle tick.
const pollPolicy={liveMs:2000,activeMs:2000,idleMs:10000,idleAfterMs:30000,retryMs:2000};
let pollTimer=null,pollDueAt=0,pollChain=0,pollInFlight=null,pollLastData='',pollLastChange=Date.now(),pollFailure=false;
const panelVisible=()=>!document.hidden;
function hasLiveWork(){
  const rows=[...(state.tasks||[])];
  if(Array.isArray(state.execution_activity?.tasks))rows.push(...state.execution_activity.tasks);
  return rows.some(row=>['queued','running','dispatching'].includes(row?.status));
}
function pollIntervalMs(){
  if(pollFailure)return pollPolicy.retryMs;               // a failed poll retries promptly
  if(hasLiveWork())return pollPolicy.liveMs;              // running work keeps the live cadence
  return Date.now()-pollLastChange>=pollPolicy.idleAfterMs?pollPolicy.idleMs:pollPolicy.activeMs;
}
// `server_time` changes on every read by design, so it never counts as activity.
function pollDataSignature(){const {server_time, ...rest}=state;return JSON.stringify(rest);}
function markPollData(){
  const signature=pollDataSignature();
  if(signature!==pollLastData){pollLastData=signature;pollLastChange=Date.now();}
}
function stopAutoPoll(){pollChain++;if(pollTimer!==null){clearTimeout(pollTimer);pollTimer=null;}pollDueAt=0;}
function startAutoPoll(){
  stopAutoPoll();                                          // at most one auto chain per document
  if(!panelVisible())return;                               // a hidden document keeps no timer
  const delay=pollIntervalMs();
  pollDueAt=Date.now()+delay;
  pollTimer=setTimeout(()=>{pollTimer=null;pollDueAt=0;runAutoPoll();},delay);
}
async function runAutoPoll(){
  if(!panelVisible())return;                               // visibilitychange restarts the chain
  const chain=pollChain;
  await refresh();
  if(chain===pollChain)startAutoPoll();                     // a switch or reopen already owns the chain
}
function markPanelActivity(){
  pollLastChange=Date.now();
  // Bring a long idle tick forward when the user works again; never delay a live one.
  if(panelVisible()&&pollTimer!==null&&pollDueAt-Date.now()>pollPolicy.activeMs)startAutoPoll();
}
document.addEventListener('visibilitychange',()=>{
  if(panelVisible()){pollLastChange=Date.now();refresh();startAutoPoll();}   // resume as soon as it is shown
  else stopAutoPoll();                                      // hidden: no request and no running timer
});
['pointerdown','keydown'].forEach(type=>document.addEventListener(type,markPanelActivity,{passive:true}));
// Closing the page ends the chain; nothing is left scheduled behind it.
window.addEventListener('pagehide',()=>{stopAutoPoll();pollInFlight=null;});
// Manual refreshes, project switches and the auto chain all enter here, so one
// document never holds two overlapping requests and never forks a second chain.
async function refresh(){
  if(pollInFlight)return pollInFlight;
  const request=refreshState().finally(()=>{if(pollInFlight===request)pollInFlight=null;});
  pollInFlight=request;
  return request;
}
async function refreshState() {
  const epoch=viewEpoch;
  try {const tree=state.tree,observability=state.observability||{};
    const fresh=await api('/api/state');if(epoch!==viewEpoch)return;if(!fresh.view||fresh.view.mode!==projectView.mode)throw new Error('服务尚未识别项目视图，请重新打开或升级工作台连接');state=fresh;state.tree=tree;state.observability=observability;
    if(state.view)projectView=state.view;savePanelBinding();renderProjectView();renderExecutionActivity();
    if(!selectedTask&&state.tasks.length)selectedTask=state.tasks[0].id;
    renderModels();renderUsage();renderWorkflow();renderTasks();renderDetail();
    try{state.treeError=null;state.tree=await api('/api/tree');}catch(error){if(error.stale)return;state.treeError=error.message;}
    renderGoals();
    pollFailure=false;markPollData();
    setMarkup($('sync'),'<span class="dot"></span>实时同步');$('sync').classList.remove('offline');$('last-sync').textContent=`最近同步 ${clock(state.server_time)} · 数据保存在本机`;
  } catch(error){if(error.stale)return;
    if(projectView.mode==='project'&&/项目目录不存在|项目路径不是目录|项目目录解析后不是目录/.test(error.message)){
      changeProjectView({mode:'unbound',project:null});toast('原绑定目录已不可用，请重新选择项目目录',true);return;
    }
    pollFailure=true;
    setMarkup($('sync'),'<span class="dot"></span>连接已中断');$('sync').classList.add('offline');$('last-sync').textContent=error.message||'无法连接本地面板，请运行启动脚本。';}finally{if(epoch!==viewEpoch)refresh();}
}
function openModel(id=null) {
  $('model-form').reset();$('model-error').textContent='';$('discovered-models').innerHTML='';$('model-id').value=id||'';
  const model=state.models.find(m=>m.id===id);$('model-dialog-title').textContent=model?'编辑执行模型':'添加执行模型';
  $('model-name').value=model?.name||'';$('model-provider').value=model?.provider||'local';$('model-base').value=model?.api_base||'';$('model-code').value=model?.model||'';
  $('model-input').value=model?.max_input_tokens||32768;$('model-output').value=model?.max_output_tokens||4096;$('model-enabled').checked=model?.enabled??true;$('model-bypass').checked=model?.bypass_proxy??true;$('model-default').checked=!!id&&id===state.default_model;
  $('model-capabilities').value=(model?.preferred_tasks||[]).join('、');$('model-capability-notes').value=model?.capability_notes||'';$('model-cost-preference').value=model?.cost_preference||($('model-provider').value==='local'?'economy':'balanced');
  $('model-complexity').value=model?.max_task_complexity||($('model-provider').value==='local'?'simple':'complex');
  renderThinkingSetting(model||{provider:$('model-provider').value,api_base:$('model-base').value});
  renderSubagentCapability(model||null);
  $('model-price-input').value=model?.pricing_cny?.cache_miss_input??'';$('model-price-cache').value=model?.pricing_cny?.cache_hit_input??'';$('model-price-output').value=model?.pricing_cny?.output??'';
  $('key-hint').textContent=model?.api_key_set?'已有密钥。留空保持原密钥；输入新值可替换。':'';$('clear-key-label').hidden=!model?.api_key_set;$('model-dialog').showModal();
}
function modelBody() {
  const id=$('model-id').value;
  return {name:$('model-name').value.trim(),provider:$('model-provider').value,api_base:$('model-base').value.trim(),model:$('model-code').value.trim(),api_key:$('model-key').value,
    clear_key:$('model-clear-key').checked,api_key_env:state.models.find(m=>m.id===id)?.api_key_env||'',max_input_tokens:Number($('model-input').value),max_output_tokens:Number($('model-output').value),enabled:$('model-enabled').checked,bypass_proxy:$('model-bypass').checked,make_default:$('model-default').checked,
    preferred_tasks:$('model-capabilities').value.split(/[,，、;；\n]/).map(s=>s.trim()).filter(Boolean),capability_notes:$('model-capability-notes').value.trim(),cost_preference:$('model-cost-preference').value,max_task_complexity:$('model-complexity').value,thinking_level:$('model-thinking-level').value,
    pricing_cny:['model-price-input','model-price-cache','model-price-output'].every(key=>$(key).value==='')?null:{cache_miss_input:$('model-price-input').value===''?null:Number($('model-price-input').value),cache_hit_input:$('model-price-cache').value===''?null:Number($('model-price-cache').value),output:$('model-price-output').value===''?null:Number($('model-price-output').value)}};
}
function renderThinkingSetting(model,selected=null){
  let profile=model.thinking;
  if(!profile){let official=false;try{const url=new URL(model.api_base);official=url.protocol==='https:'&&url.hostname==='api.deepseek.com'&&(!url.port||url.port==='443');}catch{}
    profile=official?{supported:['default','off','low','high','max'],default_effort:'high',note:'DeepSeek 官方默认开启思考，使用 high。',selected:'default'}:{supported:['default'],note:'尚未确认此引擎的等级。保存模型后可读取引擎默认。',selected:'default'};
  }
  modelThinkingProfile=profile;
  const names={default:'沿用默认',off:'关闭',low:'低 / low',medium:'中 / medium',high:'高 / high',xhigh:'超高 / xhigh',max:'最高 / max'};
  $('model-thinking-level').innerHTML=profile.supported.map(id=>`<option value="${escapeText(id)}">${escapeText(names[id]||id)}</option>`).join('');
  $('model-thinking-level').value=profile.supported.includes(selected||model.thinking_level||profile.selected||'default')?(selected||model.thinking_level||profile.selected||'default'):'default';
  $('model-thinking-probe').disabled=!$('model-id').value;renderThinkingNote();
}
function renderThinkingNote(){
  const p=modelThinkingProfile||{};const names={off:'关闭',low:'低',medium:'中',high:'高',xhigh:'超高',max:'最高'};
  const known=p.default_effort?`已确认默认：${names[p.default_effort]||p.default_effort} / ${p.default_effort}。 `:'';
  $('model-thinking-note').textContent=known+(p.note||'')+' 选择仅影响工作台的新任务，不修改服务端全局设置；更改后请新建会话。';
}
function taskFields(){
  const editing=$('task-kind').value==='edit',automatic=$('task-route').value==='auto';
  $('edit-fields').hidden=!editing;$('project-field').hidden=false;
  $('project-hint').textContent='必填；任务归属此目录，不会使用工作台安装目录';
  $('task-engine').querySelector('[value="direct"]').disabled=editing||automatic;$('task-engine').querySelector('[value="aider"]').disabled=!editing||automatic;
  if((automatic&&!agentIds().includes($('task-engine').value))||(editing&&$('task-engine').value==='direct')||(!editing&&$('task-engine').value==='aider'))$('task-engine').value=defaultAgent();
  $('task-project').required=true;$('task-files').required=editing;
}
function routeFields(){
  const automatic=$('task-route').value==='auto';$('task-model-field').hidden=automatic;$('task-model').required=!automatic;
  $('task-engine').disabled=!!continuingTask;
  if(automatic&&!agentIds().includes($('task-engine').value))$('task-engine').value=defaultAgent();
  $('route-preview').textContent='预览只用于判断，实际发送前会再次检查。';taskFields();updateTaskOutputHint();
}
function taskBody(){
  return {model_id:$('task-model').value,route_mode:$('task-route').value,kind:$('task-kind').value,title:$('task-title').value.trim(),prompt:$('task-prompt').value,
    project:$('task-project').value.trim(),files:$('task-files').value.split(/\r?\n/).map(f=>f.trim()).filter(Boolean),test_command:$('task-test').value,timeout:Number($('task-timeout').value),engine:$('task-engine').value,continue_task:continuingTask?.id,
    request_output_tokens:$('task-request-output').value===''?null:Number($('task-request-output').value),
    routing:{category:$('task-category').value,complexity:$('task-complexity').value,input_tokens:Number($('task-input-estimate').value),output_tokens:Number($('task-output-estimate').value),expected_calls:Number($('task-calls-estimate').value)}};
}
function openTask(parent=null) {
  if(!parent&&projectView.mode==='unbound'){toast('请先在顶部选择项目目录',true);$('project-view-switch').focus();return;}
  const enabled=state.models.filter(m=>m.enabled);if(!enabled.length){toast('请先添加并启用一个执行模型',true);openModel();return;}
  // The manual picker only offers candidates inside the global scope.  The
  // backend gate stays authoritative; this keeps the UI from proposing a model
  // that would be refused.  A continuing task keeps its original model id so the
  // refusal is explicit instead of a silent model swap.
  const inScope=inScopeModels(enabled);
  if(!inScope.length){toast(`执行模型范围「${scopeLabel()}」下没有已启用的模型；请在模型范围与预算中调整范围或启用相应模型。`,true);return;}
  continuingTask=parent;
  $('task-engine').innerHTML=Object.entries(registeredAgents()).map(([id,info])=>`<option value="${escapeText(id)}">${escapeText(info.name||id)} · 工具与会话</option>`).join('')+'<option value="direct">直接询问模型 · 简单咨询</option><option value="aider">Aider · 文件修改</option>';
  $('task-form').reset();$('task-error').textContent='';$('task-model').innerHTML=enabled.map(m=>`<option value="${escapeText(m.id)}"${modelInScope(m)?'':' disabled'}>${escapeText(m.name)}${modelInScope(m)?'':'（范围外，不可选）'}</option>`).join('');$('task-model').value=parent?.model_id||(inScope.some(m=>m.id===state.default_model)?state.default_model:inScope[0].id);
  $('task-engine').value=parent?.engine||defaultAgent();$('task-kind').value=parent?.kind||'chat';$('task-project').value=parent?.project||(projectView.mode==='project'?projectView.project:'');$('task-files').value=(parent?.files||[]).join('\n');$('task-title').value=parent?'继续 · '+parent.title:'';
  $('task-route').querySelector('[value="auto"]').disabled=!state.workflow?.enabled;
  $('task-route').value=parent?'manual':state.workflow?.enabled?'auto':'manual';$('task-route').disabled=!!parent;
  if(parent?.route?.requirements){const r=parent.route.requirements;$('task-category').value=r.category;$('task-complexity').value=r.complexity;$('task-input-estimate').value=r.input_tokens;$('task-output-estimate').value=r.output_tokens;$('task-calls-estimate').value=r.expected_calls;}
  $('task-request-output').value=parent?.request_output_tokens??'';
  for(const id of ['task-model','task-kind','task-engine','task-project','task-files','task-request-output'])$(id).disabled=!!parent;
  $('task-session-hint').textContent=parent?`继续同一 ${engineNames[parent.engine]||parent.engine} 会话，保留前面的任务上下文。`:'新任务使用所选 Agent 的独立会话；任务结束后可以继续追问。';
  routeFields();updateTaskOutputHint();$('task-dialog').showModal();
}
$('add-model').onclick=()=>openModel();$('add-model-small').onclick=()=>openModel();$('new-task').onclick=()=>openTask();
function renderAgentSettings(){
  const engine=$('agent-settings-agent').value,info=registeredAgents()[engine],schema=info?.configuration;
  const fields=schema?.model_fields||[],model=state.models.find(m=>m.id===$('agent-settings-model').value);
  $('agent-settings-description').textContent=schema?.description||'当前连接尚未提供此 Agent 的额外配置。';
  $('agent-settings-model-field').hidden=!fields.length;
  $('agent-settings-fields').replaceChildren();$('agent-settings-error').textContent='';
  let supported=true;
  for(const field of fields){
    if(field.type!=='select'){supported=false;continue;}
    const label=document.createElement('label');label.textContent=field.label;
    const select=document.createElement('select');select.dataset.agentKey=field.key;select.required=true;
    for(const option of field.options||[])select.add(new Option(option.label,option.value));
    select.value=model?.[field.key]??field.provider_defaults?.[model?.provider]??field.default??'';
    label.append(select);$('agent-settings-fields').append(label);
    if(field.note){const note=document.createElement('p');note.className='field-hint';note.textContent=field.note;$('agent-settings-fields').append(note);}
  }
  if(!supported)$('agent-settings-error').textContent='此 Agent 的配置类型需要更新工作台后使用。';
  $('agent-settings-save').hidden=!fields.length||!supported;
  $('agent-settings-save').disabled=!model;
}
$('agent-settings-open').onclick=()=>{
  $('agent-settings-agent').replaceChildren(...Object.entries(registeredAgents()).map(([id,info])=>new Option(info.name||id,id)));
  $('agent-settings-agent').value=defaultAgent();
  $('agent-settings-model').replaceChildren(...state.models.map(model=>new Option(model.name,model.id)));
  $('agent-settings-model').value=state.default_model||state.models[0]?.id||'';
  renderAgentSettings();$('agent-settings-dialog').showModal();
};
$('agent-settings-agent').onchange=renderAgentSettings;
$('agent-settings-model').onchange=renderAgentSettings;
$('agent-settings-form').onsubmit=async event=>{
  event.preventDefault();const button=$('agent-settings-save'),engine=$('agent-settings-agent').value,model=$('agent-settings-model').value;
  const values=Object.fromEntries([...$('agent-settings-fields').querySelectorAll('[data-agent-key]')].map(field=>[field.dataset.agentKey,field.value]));
  if(!model||!Object.keys(values).length)return;
  button.disabled=true;$('agent-settings-agent').disabled=true;$('agent-settings-model').disabled=true;
  try{await api(`/api/models/${encodeURIComponent(model)}/save`,{agent_settings:{engine,values}});$('agent-settings-dialog').close();await refresh();toast(`${engineNames[engine]||engine} 设置已保存，新会话生效。`);}
  catch(error){$('agent-settings-error').textContent=error.message;}
  finally{button.disabled=false;$('agent-settings-agent').disabled=false;$('agent-settings-model').disabled=false;}
};
$('execution-agent').onchange=async()=>{
  const control=$('execution-agent'),value=control.value;control.disabled=true;
  try{state.workflow=await api('/api/workflow',{execution_agent:value});await refresh();toast(`新任务默认使用 ${engineNames[value]}；已有任务继续使用原 Agent。`);}
  catch(error){control.value=defaultAgent();toast(error.message,true);}
  finally{control.disabled=false;}
};
$('usage-refresh').onclick=async()=>{
  const button=$('usage-refresh');button.disabled=true;
  try{state.codex_usage=await api('/api/codex-usage/refresh',{});renderUsage();if(!state.codex_usage.refreshing)toast('刚刚已查询，请稍后刷新。');}
  catch(error){toast(error.message,true);}
  finally{button.disabled=!!state.codex_usage?.refreshing;}
};
document.querySelectorAll('.close-dialog').forEach(button=>button.onclick=()=>button.closest('dialog').close());
document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{taskFilter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===button));renderTasks();});
$('task-kind').onchange=()=>{if(!$('task-project').value&&projectView.mode==='project')$('task-project').value=projectView.project;$('task-category').value=$('task-kind').value==='edit'?'code_small':'analysis';$('task-calls-estimate').value=$('task-kind').value==='edit'?3:1;taskFields();};
$('task-route').onchange=routeFields;
$('preview-route').onclick=async()=>{const button=$('preview-route');button.disabled=true;try{const route=await api('/api/route-preview',taskBody());const model=state.models.find(m=>m.id===route.model_id);$('route-preview').textContent=`${model?.name||route.model_id}：${route.reason}${route.excluded?.length?'；其他模型：'+route.excluded.map(m=>m.model_name+' '+m.reason).join('；'):''}`;}catch(error){$('route-preview').textContent=error.message;}finally{button.disabled=false;}};
$('task-engine').onchange=taskFields;
$('model-enabled').onchange=()=>{if(!$('model-enabled').checked)$('model-default').checked=false;};
$('model-provider').onchange=()=>{
  const provider=$('model-provider').value;
  const bases={local:'http://localhost:1234/v1',deepseek:'https://api.deepseek.com',glm:'https://open.bigmodel.cn/api/paas/v4',custom:''};
  $('model-base').value=bases[provider];$('model-code').value='';$('model-bypass').checked=provider==='local';
  $('model-complexity').value=provider==='local'?'simple':'complex';
  renderThinkingSetting({provider,api_base:$('model-base').value});
};
function updateThinkingEndpoint(){const saved=state.models.find(m=>m.id===$('model-id').value);const same=saved&&saved.api_base===$('model-base').value.trim().replace(/\/$/,'')&&saved.model===$('model-code').value.trim();renderThinkingSetting(same?saved:{provider:$('model-provider').value,api_base:$('model-base').value});}
$('model-base').addEventListener('input',updateThinkingEndpoint);$('model-code').addEventListener('input',updateThinkingEndpoint);
$('model-thinking-level').onchange=renderThinkingNote;
$('model-thinking-probe').onclick=async()=>{const saved=state.models.find(m=>m.id===$('model-id').value);if(!saved||saved.api_base!==$('model-base').value.trim().replace(/\/$/,'')||saved.model!==$('model-code').value.trim()){toast('请先保存接口和模型配置，再读取引擎默认。',true);return;}const button=$('model-thinking-probe');const selected=$('model-thinking-level').value;button.disabled=true;button.textContent='读取中…';try{const thinking=await api(`/api/models/${encodeURIComponent(saved.id)}/thinking`,{});saved.thinking=thinking;renderThinkingSetting(saved,selected);toast('默认等级已确认；只读查询，没有调用模型生成。');}catch(error){$('model-error').textContent=error.message;}finally{button.disabled=false;button.textContent='读取引擎默认';}};
$('model-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('model-error').textContent='';try{
  const id=$('model-id').value;await api(id?`/api/models/${encodeURIComponent(id)}/save`:'/api/models',modelBody());$('model-dialog').close();toast('模型已保存');await refresh();
}catch(error){$('model-error').textContent=error.message;}finally{button.disabled=false;}};
$('discover-models').onclick=async()=>{const button=$('discover-models');button.disabled=true;button.textContent='读取中…';$('model-error').textContent='';try{
  const data=await api('/api/discover',{...modelBody(),id:$('model-id').value});$('discovered-models').innerHTML=data.models.map(model=>`<option value="${escapeText(model)}"></option>`).join('');
  if(data.models.length===1)$('model-code').value=data.models[0];toast(data.models.length?`读取到 ${data.models.length} 个模型，请选择模型 ID`:'接口没有返回列表，请手动填写模型 ID');
}catch(error){$('model-error').textContent=error.message;}finally{button.disabled=false;button.textContent='读取列表';}};
$('models').onclick=async event=>{const button=event.target.closest('[data-model-action]');if(!button)return;const id=button.dataset.id;const action=button.dataset.modelAction;
  if(action==='details'){if(expandedBillingModels.has(id))expandedBillingModels.delete(id);else expandedBillingModels.add(id);renderModels();return;}
  if(action==='edit'){openModel(id);return;}
  if(action==='delete'){pendingDelete=id;$('confirm-message').textContent=`确定移除「${state.models.find(m=>m.id===id)?.name}」？`;$('confirm-dialog').showModal();return;}
  if(action==='balance'){
    try{refreshingBalances.add(id);renderModels();await api(`/api/models/${encodeURIComponent(id)}/balance`,{});toast('正在同步余额');}
    catch(error){toast(error.message,true);}finally{refreshingBalances.delete(id);await refresh();}return;
  }
  try{if(action==='test'){testingModels.add(id);renderModels();const result=await api(`/api/models/${encodeURIComponent(id)}/test`,{});toast(result.ok?'连接测试通过':result.message,!result.ok);}else{await api(`/api/models/${encodeURIComponent(id)}/default`,{});toast('默认模型已更新');}}catch(error){toast(error.message,true);}finally{testingModels.delete(id);await refresh();}
};
$('confirm-delete').onclick=async()=>{const button=$('confirm-delete');button.disabled=true;try{await api(`/api/models/${encodeURIComponent(pendingDelete)}/delete`,{});$('confirm-dialog').close();toast('模型已移除，历史任务已保留');await refresh();}catch(error){toast(error.message,true);}finally{button.disabled=false;}};
$('task-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('task-error').textContent='';try{
  const task=await api('/api/tasks',taskBody());
  selectedTask=task.id;detailTab='result';$('task-dialog').close();taskFilter='all';document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b.dataset.filter==='all'));toast('任务已分派');await refresh();
}catch(error){$('task-error').textContent=error.message;}finally{button.disabled=false;}};
$('task-list').onclick=event=>{const row=event.target.closest('[data-task-id]');if(!row)return;selectedTask=row.dataset.taskId;detailTab='result';renderTasks();renderDetail();};
$('task-detail').onclick=async event=>{const tab=event.target.closest('[data-tab]');if(tab){detailTab=tab.dataset.tab;renderDetail();return;}if(event.target.closest('#continue-task')){openTask(state.tasks.find(t=>t.id===selectedTask));return;}if(event.target.closest('#recover-task')){const id=selectedTask;const task=state.tasks.find(t=>t.id===id);const reason=task?.recovery?.reason||'面板重启时任务未完成';if(!window.confirm(`恢复执行同一个任务记录？\n\n原因：${reason}\n\n不会自动重发，也不会创建新记录；执行完成情况与工具副作用未知，请自行核对改动。\n\n确认恢复？`))return;try{await api(`/api/tasks/${encodeURIComponent(id)}/recover`,{confirm:true});toast('已按原任务记录重新排队；未创建新记录。');await refresh();}catch(error){toast(error.message,true);}return;}if(event.target.closest('#stop-task')){try{await api(`/api/tasks/${encodeURIComponent(selectedTask)}/cancel`,{});toast('已停止。线上服务是否继续计算取决于服务端的取消机制。');await refresh();}catch(error){toast(error.message,true);}}};
$('workflow-toggle').onchange=async()=>{const button=$('workflow-toggle');const enabled=button.checked;button.disabled=true;try{await api('/api/workflow',{enabled});toast(enabled?'协作已开启，后续任务使用自动路由':'协作已关闭，运行中的任务继续执行');}catch(error){toast(error.message,true);}finally{button.disabled=false;await refresh();}};
$('workflow-settings').onclick=()=>{const w=state.workflow;$('workflow-scope').value=scopeValue();$('workflow-scope-hint').textContent=scopeHintText();$('workflow-paid').checked=!!w.paid_allowed;$('workflow-enforce-budget').checked=w.enforce_paid_budget!==false;$('workflow-enforce-calls').checked=w.enforce_model_call_limit!==false;updateBudgetInputs();$('workflow-task-budget').value=w.task_budget_cny;$('workflow-daily-budget').value=w.daily_budget_cny;$('workflow-max-calls').value=w.max_model_calls;$('workflow-form-error').textContent='';workflowSavedDraft=JSON.stringify(workflowDraft());workflowDirtyHint();$('workflow-dialog').showModal();};
let workflowSavedDraft='';
function workflowDraft(){return {model_scope:$('workflow-scope').value,paid_allowed:$('workflow-paid').checked,enforce_paid_budget:$('workflow-enforce-budget').checked,enforce_model_call_limit:$('workflow-enforce-calls').checked,task_budget_cny:Number($('workflow-task-budget').value),daily_budget_cny:Number($('workflow-daily-budget').value),max_model_calls:Number($('workflow-max-calls').value)};}
function workflowDirtyHint(){const hint=$('workflow-save-status');if(hint){const dirty=JSON.stringify(workflowDraft())!==workflowSavedDraft;hint.textContent=dirty?'有未保存的修改 · 点击保存设置才会生效':'修改后请点击保存设置才会生效';hint.classList.toggle('unsaved',dirty);}}
$('workflow-form').addEventListener('input',workflowDirtyHint);
$('workflow-scope').onchange=()=>{$('workflow-scope-hint').textContent=scopeHintText($('workflow-scope').value);workflowDirtyHint();};
$('workflow-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{await api('/api/workflow',{model_scope:$('workflow-scope').value,paid_allowed:$('workflow-paid').checked,enforce_paid_budget:$('workflow-enforce-budget').checked,enforce_model_call_limit:$('workflow-enforce-calls').checked,task_budget_cny:Number($('workflow-task-budget').value),daily_budget_cny:Number($('workflow-daily-budget').value),max_model_calls:Number($('workflow-max-calls').value)});$('workflow-dialog').close();toast('执行模型范围与预算限制已保存；执行中的任务不受影响，排队任务在启动前按新范围检查');await refresh();}catch(error){$('workflow-form-error').textContent=error.message;}finally{button.disabled=false;}};
$('task-model').onchange=updateTaskOutputHint;
$('new-goal').onclick=()=>{if(projectView.mode==='unbound'){toast('请先选择项目目录',true);return;}$('goal-form').reset();$('goal-project').value=projectView.mode==='project'?projectView.project:'';$('goal-error').textContent='';$('goal-dialog').showModal();};
$('goal-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('goal-error').textContent='';try{await api('/api/goals',{title:$('goal-title').value.trim(),objective:$('goal-objective').value.trim(),project:$('goal-project').value.trim()});$('goal-dialog').close();toast('目标已创建');await refresh();}catch(error){$('goal-error').textContent=error.message;}finally{button.disabled=false;}};
$('stage-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('stage-error').textContent='';try{
  const split=value=>value.split(/[,，、;；\s]+/).map(item=>item.trim()).filter(Boolean);
  await api('/api/stages',{goal_id:stageGoal,title:$('stage-title').value.trim(),objective:$('stage-objective').value.trim(),kind:$('stage-kind').value,depends_on:split($('stage-depends').value),files:$('stage-files').value.split(/\r?\n/).map(item=>item.trim()).filter(Boolean),acceptance:$('stage-acceptance').value.split(/[;；\n]+/).map(item=>item.trim()).filter(Boolean),candidates:split($('stage-candidates').value),fixed_model:$('stage-fixed').value.trim()});
  $('stage-dialog').close();toast('阶段已创建');await refresh();
}catch(error){$('stage-error').textContent=error.message;}finally{button.disabled=false;}};
$('action-form').onsubmit=async event=>{event.preventDefault();if(!pendingAction)return;const button=$('action-confirm');button.disabled=true;$('action-error').textContent='';const reason=$('action-reason').value.trim();const{type,id}=pendingAction;try{
  if(type==='goal-cancel')await api(`/api/goals/${encodeURIComponent(id)}/status`,{status:'cancelled'});
  else if(type==='stage-cancel')await api(`/api/stages/${encodeURIComponent(id)}/cancel`,{reason});
  else if(type==='stage-requeue')await api(`/api/stages/${encodeURIComponent(id)}/requeue`,{reason});
  else if(type==='dispatch')await api(`/api/stages/${encodeURIComponent(id)}/dispatch`,{prompt:reason,source:'panel'});
  $('action-dialog').close();pendingAction=null;toast('操作已记录');await refresh();
}catch(error){$('action-error').textContent=error.message;}finally{button.disabled=false;}};
$('goals-tree').addEventListener('toggle',event=>{const node=event.target;if(!(node instanceof HTMLDetailsElement))return;const key=node.dataset.goal?`goal:${node.dataset.goal}`:node.dataset.stage?`stage:${node.dataset.stage}`:node.dataset.task?`task:${node.dataset.task}`:null;if(key){if(node.open)treeOpen.add(key);else treeOpen.delete(key);}if(node.open){const host=node.closest('[data-task]');if(host&&state.tree)loadObservability(host.dataset.task);}},true);
$('goals-tree').onclick=async event=>{
  const goalButton=event.target.closest('[data-goal-action]');
  if(goalButton){const id=goalButton.dataset.id;const action=goalButton.dataset.goalAction;const goal=(state.tree?.goals||[]).find(item=>item.id===id);
    if(action==='stage'){openStage(id,goal?.title);return;}
    if(action==='complete'){try{await api(`/api/goals/${encodeURIComponent(id)}/status`,{status:'completed'});toast('目标已标记完成');await refresh();}catch(error){toast(error.message,true);}return;}
    if(action==='cancel'){openAction({type:'goal-cancel',id,title:'取消目标',message:`取消「${goal?.title||id}」？取消后不能改回，阶段也不会再自动派发。`,confirm:'取消目标',field:false});return;}
  }
  const stageButton=event.target.closest('[data-stage-action]');
  if(stageButton){const id=stageButton.dataset.id;const action=stageButton.dataset.stageAction;
    if(action==='dispatch'){openAction({type:'dispatch',id,title:'派发阶段',message:'派发按阶段固定类型执行：只读分析不会被改成完全权限修改。任务进入同一个顺序队列。',confirm:'派发',field:true,required:true,placeholder:'写清目标、需要保留的行为与验收条件'});return;}
    if(action==='cancel'){openAction({type:'stage-cancel',id,title:'取消阶段',message:'取消会写入持久记录；已通过验收的阶段不能取消。',confirm:'取消阶段',field:true,required:true,placeholder:'取消原因'});return;}
    if(action==='requeue'){openAction({type:'stage-requeue',id,title:'恢复 / 重新排队',message:'确认前不会重新发送收费请求；恢复原因会写入持久记录。',confirm:'恢复',field:true,required:true,placeholder:'恢复原因'});return;}
    if(action==='sync'){try{await api(`/api/stages/${encodeURIComponent(id)}/sync`,{});toast('阶段状态已同步');await refresh();}catch(error){toast(error.message,true);}return;}
  }
};
$('task-detail').addEventListener('toggle',event=>{const node=event.target;if(!(node instanceof HTMLDetailsElement)||node.id!=='detail-observability')return;const key='detail-obs:'+selectedTask;if(node.open)treeOpen.add(key);else treeOpen.delete(key);if(node.open)loadObservability(selectedTask);},true);

// Current-project entry: read-only name + lightweight switch menu (known/recent + system picker).
$('project-view-name').onclick=()=>{projectPathPinned=!projectPathPinned;renderProjectView();};
$('project-view-switch').onclick=event=>{event.stopPropagation();toggleProjectMenu();};
$('project-view-known').addEventListener('click',event=>{
  const item=event.target.closest('[data-project]');if(!item)return;
  switchToProject(item.dataset.project);
});
document.addEventListener('click',event=>{
  const menu=$('project-view-menu');if(!menu||menu.hidden)return;
  if(menu.contains(event.target))return;
  if($('project-view-switch')?.contains(event.target))return;
  closeProjectMenu();
});
document.addEventListener('keydown',event=>{
  if(event.key!=='Escape')return;
  const menu=$('project-view-menu');if(!menu||menu.hidden)return;
  closeProjectMenu();$('project-view-switch')?.focus();
});
$('execution-activity').onclick=event=>{
  const button=event.target.closest('[data-activity-project]');if(!button)return;
  switchToProject(button.dataset.activityProject);
};
$('project-view-choose').onclick=async()=>{
  closeProjectMenu();
  const button=$('project-view-choose'),epoch=viewEpoch;
  button.disabled=true;button.textContent='选择中…';button.setAttribute('aria-busy','true');
  try{
    let result=await api('/api/projects/pick',{});
    if(!/^[0-9a-f]{32}$/.test(result.id||''))throw new Error('工作台连接需要升级，请重新连接工作台');
    toast('请在 Windows 文件夹选择窗口中选择项目目录');
    const deadline=Date.now()+330000;
    while(result.status==='pending'){
      await new Promise(resolve=>setTimeout(resolve,750));
      if(epoch!==viewEpoch){const error=new Error('项目已切换，忽略旧选择');error.stale=true;throw error;}
      if(Date.now()>deadline)throw new Error('目录选择已超时，请重新点击选择目录');
      result=await api(`/api/projects/pick/${result.id}`);
    }
    if(result.status==='cancelled'){toast('已取消选择，保留当前项目');return;}
    if(result.status!=='selected'||!result.project)throw new Error(result.error||'目录选择失败，请重试');
    window.modelDeskLockProjectContext?.();
    const data=await api('/api/state',undefined,{mode:'project',project:result.project});
    if(!data.view||data.view.mode!=='project')throw new Error('工作台连接需要升级');
    changeProjectView(data.view);
  }catch(error){if(!error.stale)toast(error.message,true);}
  finally{button.disabled=false;button.textContent='选择其他目录…';button.removeAttribute('aria-busy');}
};
renderProjectView();

(async()=>{try{token=(await api('/api/session',undefined,{mode:'unbound',project:null})).token;await refresh();
  await loadProjectCatalog();
  await refreshProjectStorage();
}catch(error){toast(window.modelDeskHost?`工作台连接失败：${error.message}`:'无法连接面板，请先运行启动脚本。',true);}
// The chain starts even when the first read failed, so a panel opened before the
// service answered recovers on its own instead of staying silent forever.
startAutoPoll();})();

function updateBudgetInputs(){const disabled=!$('workflow-enforce-budget').checked;$('workflow-task-budget').disabled=disabled;$('workflow-daily-budget').disabled=disabled;$('workflow-max-calls').disabled=!$('workflow-enforce-calls').checked;}
$('workflow-enforce-budget').onchange=updateBudgetInputs;

$('workflow-enforce-calls').onchange=updateBudgetInputs;

// ---------- task history management (project-scoped, preview then explicit second confirmation) ----------
const historyStatuses=new Set(['all','completed','failed','cancelled']);
const historyStatusLabels={completed:'完成',failed:'失败',cancelled:'取消',running:'执行中',queued:'等待执行',recovery_pending:'待恢复'};
function historyStatusLabel(status){return historyStatusLabels[status]||statusNames[status]||String(status||'未知');}
function historyBoundProject(){return projectView.mode==='project'&&projectView.project?projectView.project:null;}
function setRecordsError(message){const box=$('task-records-error');if(!box)return;box.textContent=message||'';box.hidden=!message;}
function setConfirmError(message){const box=$('task-records-confirm-error');if(!box)return;box.textContent=message||'';box.hidden=!message;}
function clearHistoryToken(){historyToken=null;historyPreview=null;historyExpiresAt=0;}
function resetHistoryRecords(){historyGeneration++;historyRecords=[];historySelected.clear();historyStatus='all';historyKeyword='';historyProject=null;clearHistoryToken();setRecordsError('');}
// Called on every project change: closes only this document's management dialogs and drops management state.
function resetHistoryManagement(){
  resetHistoryRecords();historyLoading=false;historySubmitting=false;
  const list=$('task-records-list');if(list)list.innerHTML='';
  const search=$('task-records-search');if(search)search.value='';
  const projectBox=$('task-records-project');if(projectBox)projectBox.textContent='—';
  document.querySelectorAll('[data-record-status]').forEach(b=>b.classList.toggle('active',b.dataset.recordStatus==='all'));
  updateHistorySelectionUI();
}
function guideHistoryProject(){
  toast(projectView.mode==='all'?'任务记录管理只按具体项目操作：请用顶部“选择项目”切换到具体项目。':'任务记录管理需要先选择具体项目：请点击顶部“选择项目”。',true);
  const bar=document.querySelector('.project-view-bar');if(bar)bar.scrollIntoView({block:'nearest'});
  const switcher=$('project-view-switch');if(switcher)switcher.focus();
}
function historyMatches(record){
  if(historyStatus!=='all'&&record.status!==historyStatus)return false;
  if(!historyKeyword)return true;
  const haystack=[record.title,record.model_name,record.group_title,record.goal_id,record.id,record.status].map(value=>String(value??'').toLowerCase()).join(' ');
  return haystack.includes(historyKeyword.toLowerCase());
}
function historyDeletable(record){return !!record&&record.deletable===true&&record.id!==undefined&&record.id!==null&&record.id!=='';}
function updateHistorySelectionUI(){
  const selectable=historyRecords.filter(historyMatches).filter(historyDeletable).map(record=>String(record.id));
  const chosenInView=selectable.filter(id=>historySelected.has(id)).length;
  const allBox=$('task-records-select-all');
  if(allBox){allBox.disabled=!selectable.length;allBox.checked=selectable.length>0&&chosenInView===selectable.length;allBox.indeterminate=chosenInView>0&&chosenInView<selectable.length;}
  const count=$('task-records-count');
  if(count)count.textContent=`共 ${historyRecords.length} 条 · 当前筛选 ${historyRecords.filter(historyMatches).length} 条 · 可删除 ${historyRecords.filter(historyDeletable).length} 条 · 已选 ${historySelected.size} 条`;
  const preview=$('task-records-preview');
  if(preview)preview.disabled=historyLoading||historySubmitting||historySelected.size===0;
  const hint=$('task-records-hint');
  if(hint)hint.textContent=historyLoading?'正在读取记录…':historySelected.size?`已选 ${historySelected.size} 条；提交预览后仍需再次点击“确认永久删除”`:'勾选记录后才能预览删除清单';
}
function renderTaskRecords(){
  const list=$('task-records-list');if(!list)return;
  const scroll=list.scrollTop;
  if(historyLoading&&!historyRecords.length)list.innerHTML='<div class="empty"><strong>正在读取该项目任务记录…</strong></div>';
  else if(!historyRecords.length)list.innerHTML='<div class="empty"><strong>该项目暂无任务记录</strong><p>记录来自后端真实任务历史，面板不会伪造条目。</p></div>';
  else{
    const rows=historyRecords.filter(historyMatches);
    if(!rows.length)list.innerHTML='<div class="empty"><strong>该筛选下没有记录</strong><p>换个状态或清空关键词再看。</p></div>';
    else list.innerHTML=rows.map(record=>{
      const id=String(record.id??''),canDelete=historyDeletable(record),checked=canDelete&&historySelected.has(id);
      return `<label class="record-row ${canDelete?'':'record-locked'}">
        <input type="checkbox" data-record-id="${escapeText(id)}" ${canDelete?'':'disabled'} ${checked?'checked':''} aria-label="选择记录 ${escapeText(record.title||id)}">
        <span class="record-main"><span class="record-title">${escapeText(record.title||'（无标题）')}</span>
          <span class="record-meta"><span>${escapeText(id)}</span><span>·</span><span>${escapeText(record.model_name||'未记录模型')}</span>${record.group_title?`<span>·</span><span>${escapeText(record.group_title)}</span>`:''}${record.goal_id?`<span>·</span><span>目标 ${escapeText(record.goal_id)}</span>`:''}</span>
          <span class="record-meta"><span>开始 ${escapeText(record.created_at||'未记录')}</span><span>·</span><span>结束 ${escapeText(record.ended_at||'未记录')}</span></span>
          ${canDelete?'':`<span class="record-blocked">不可删除：${escapeText(record.blocked_reason||'后端未说明原因')}</span>`}</span>
        ${badge(record.status,historyStatusLabel(record.status))}</label>`;
    }).join('');
  }
  list.scrollTop=scroll;
  updateHistorySelectionUI();
}
async function loadTaskRecords(){
  if(historyLoading)return;
  const epoch=viewEpoch,generation=historyGeneration,project=historyBoundProject();
  if(!project){resetHistoryManagement();['task-records-confirm-dialog','task-records-dialog'].forEach(id=>{if($(id)?.open)$(id).close();});guideHistoryProject();return;}
  historyLoading=true;setRecordsError('');renderTaskRecords();
  try{
    const data=await api('/api/task-records');
    if(epoch!==viewEpoch||generation!==historyGeneration||!$('task-records-dialog').open||historyBoundProject()!==project)return;
    if(!data||data.project!==project){historyRecords=[];historySelected.clear();setRecordsError(`返回的项目与当前绑定项目不一致（${data?.project??'未返回'}），已忽略本次结果`);return;}
    historyProject=data.project;
    historyRecords=Array.isArray(data.tasks)?data.tasks.filter(record=>record&&typeof record==='object'):[];
    const deletable=new Set(historyRecords.filter(historyDeletable).map(record=>String(record.id)));
    for(const id of Array.from(historySelected))if(!deletable.has(id))historySelected.delete(id);
    clearHistoryToken(); // a fresh list invalidates any previous preview
  }catch(error){
    if(error.stale||epoch!==viewEpoch||generation!==historyGeneration)return;
    setRecordsError(error.message||'读取任务记录失败');
  }finally{if(generation===historyGeneration){historyLoading=false;renderTaskRecords();}}
}
function renderHistoryConfirm(data,project){
  $('task-records-confirm-project').textContent=project;
  const added=new Set((data.added_ids||[]).map(String));
  const tasks=Array.isArray(data.tasks)?data.tasks:[];
  $('task-records-confirm-list').innerHTML=tasks.length?tasks.map(task=>{
    const id=String(task.id??'');
    return `<li class="${added.has(id)?'added':''}"><span class="record-title">${escapeText(task.title||'（无标题）')}</span>${badge(task.status,historyStatusLabel(task.status))}<span class="subtle">${escapeText(id)}</span>${added.has(id)?'<span class="record-added">联动加入</span>':''}</li>`;
  }).join(''):'<li class="subtle">无</li>';
  const goals=Array.isArray(data.goals)?data.goals:[];
  $('task-records-confirm-goals').innerHTML=goals.length?goals.map(goal=>`<li><span class="record-title">${escapeText(goal.title||'（无标题）')}</span><span class="subtle">${escapeText(goal.id||'')}</span></li>`).join(''):'<li class="subtle">无关联目标</li>';
  $('task-records-confirm-summary').innerHTML=`<div><span class="label">最终删除任务</span><strong>${numberText(tasks.length)}</strong></div><div><span class="label">我勾选的</span><strong>${numberText((data.requested_ids||[]).length)}</strong></div><div><span class="label">额外联动任务</span><strong>${numberText(added.size)}</strong></div><div><span class="label">目标 / 阶段</span><strong>${numberText(goals.length)} / ${numberText(data.stage_count)}</strong></div>`;
  const warnings=Array.isArray(data.warnings)?data.warnings.filter(Boolean):[];
  $('task-records-confirm-warnings').innerHTML=warnings.length?warnings.map(text=>`<li>${escapeText(text)}</li>`).join(''):'<li>后端未返回额外警告；下面的固定提示仍然适用。</li>';
  const seconds=Math.max(0,Number(data.expires_in)||0);
  $('task-records-confirm-expiry').textContent=`确认令牌有效期 ${seconds} 秒；刷新列表、改勾选或切换项目都会作废，需要重新预览。`;
  setConfirmError('');
  $('task-records-delete').disabled=false;
}
async function requestHistoryPreview(){
  if(historySubmitting||historyLoading||!historySelected.size)return;
  const epoch=viewEpoch,project=historyBoundProject();
  if(!project||project!==historyProject){toast('项目绑定已变化，请重新打开任务记录管理',true);resetHistoryManagement();return;}
  const task_ids=Array.from(historySelected);
  historySubmitting=true;setRecordsError('');updateHistorySelectionUI();
  const button=$('task-records-preview');button.disabled=true;
  try{
    const data=await api('/api/task-records/preview',{task_ids});
    if(epoch!==viewEpoch||historyBoundProject()!==project||historyProject!==project)return; // stale preview is never shown
    if(!data||data.project!==project){setRecordsError(`预览返回的项目不一致（${data?.project??'未返回'}），未删除任何记录`);clearHistoryToken();return;}
    if(!data.token||!Array.isArray(data.tasks)||!data.tasks.length){setRecordsError('预览未返回有效清单，未删除任何记录');clearHistoryToken();return;}
    historyPreview=data;historyToken=String(data.token);historyExpiresAt=Date.now()+Math.max(0,Number(data.expires_in)||0)*1000;
    renderHistoryConfirm(data,project);
    $('task-records-confirm-dialog').showModal();
  }catch(error){
    if(error.stale||epoch!==viewEpoch)return;
    setRecordsError(error.message||'预览失败，未删除任何记录');clearHistoryToken();
  }finally{historySubmitting=false;updateHistorySelectionUI();}
}
function forgetHistoryTaskCaches(ids){
  state.tasks=state.tasks.filter(task=>!ids.has(String(task.id)));state.tree=null;pendingAction=null;
  for(const id of ids){
    if(state.observability)delete state.observability[id];
    treeOpen.delete(`detail-obs:${id}`);
    observabilityLoading.delete(id);
    if(selectedTask===id){selectedTask=null;detailPrompt='';}
    if(promptTask===id){promptTask=null;detailPrompt='';}
    if(continuingTask===id||continuingTask?.id===id){continuingTask=null;if($('task-dialog').open)$('task-dialog').close();}
  }
  renderTasks();renderDetail();renderGoals();
}
async function confirmHistoryDelete(){
  if(historySubmitting)return;
  if(!historyToken){toast('缺少有效的预览确认，未删除任何记录',true);return;}
  if(Date.now()>=historyExpiresAt){toast('预览确认已过期，请重新预览清单',true);clearHistoryToken();$('task-records-confirm-dialog').close();return;}
  const epoch=viewEpoch,project=historyBoundProject(),token=historyToken;
  if(!project||project!==historyProject){toast('项目绑定已变化，未删除任何记录',true);resetHistoryManagement();return;}
  const deletedIds=new Set((historyPreview?.tasks||[]).map(task=>String(task.id??'')).filter(Boolean));
  historySubmitting=true;
  const button=$('task-records-delete');button.disabled=true;
  try{
    const data=await api('/api/task-records/delete',{token});
    if(epoch!==viewEpoch||historyBoundProject()!==project){resetHistoryManagement();return;} // late result never applies to another project
    clearHistoryToken();
    if($('task-records-confirm-dialog')?.open)$('task-records-confirm-dialog').close();
    forgetHistoryTaskCaches(deletedIds);
    toast(`已删除：任务 ${numberText(data?.deleted_tasks)} · 目标 ${numberText(data?.deleted_goals)} · 阶段 ${numberText(data?.deleted_stages)}${data?.cleanup_pending?'；文件清理暂未完成，服务恢复后继续':''}（项目文件与费用总账保留）`,!!data?.cleanup_pending);
    await loadTaskRecords();
    await refresh();
    refreshProjectStorage();
  }catch(error){
    if(error.stale||epoch!==viewEpoch){resetHistoryManagement();return;}
    setConfirmError(error.message||'删除失败，未确认删除');
  }finally{historySubmitting=false;if($('task-records-delete'))$('task-records-delete').disabled=!historyToken;}
}
// Auxiliary, read-only storage facts: real per-project DB state plus the legacy global store.
// Legacy/unassigned history stays out of the daily project list and is only pointed at here.
async function loadRecordsStorage(){
  const stateBox=$('records-storage-state'),legacyBox=$('records-storage-legacy');
  if(stateBox)stateBox.textContent='正在读取项目存储状态…';
  await refreshProjectStorage();
  if(stateBox){
    if(projectStorageError)stateBox.textContent=`项目存储状态读取失败：${projectStorageError}；这不代表记录丢失。`;
    else{const text=projectStorageText(projectStorage);stateBox.textContent=text?`当前项目：${text}`:'项目存储状态尚未返回；这不代表记录丢失。';}
  }
  if(legacyBox)legacyBox.textContent='正在读取旧全局存储…';
  const epoch=viewEpoch,project=historyBoundProject();
  try{
    const data=await api('/api/db/space',undefined,{mode:'unbound',project:null});
    if(epoch!==viewEpoch||historyBoundProject()!==project)return;
    const rows=Array.isArray(data?.databases)?data.databases:[];
    legacyStorage=rows.find(row=>row.scope==='legacy')||rows[0]||null;
  }catch(error){if(error.stale)return;legacyStorage=null;}
  if(legacyBox)legacyBox.textContent=legacyStorage
    ? `旧全局库（迁移前 / 未归属原件）：${legacyStorage.path} · ${formatBytes(legacyStorage.size_bytes)}。这里只作定位提示，不并入上面的项目列表。`
    : '没有发现旧全局数据库，或它暂时不可读；这里只作辅助提示，不并入上面的项目列表。';
}
$('task-history-open').onclick=()=>openTaskHistory();
function openTaskHistory(){
  const project=historyBoundProject();
  if(!project){guideHistoryProject();return;}
  resetHistoryRecords();
  historyProject=project;
  $('task-records-project').textContent=project;
  $('task-records-search').value='';
  if($('task-records-compact-result'))$('task-records-compact-result').textContent='';
  document.querySelectorAll('[data-record-status]').forEach(b=>b.classList.toggle('active',b.dataset.recordStatus==='all'));
  renderTaskRecords();
  $('task-records-dialog').showModal();
  loadTaskRecords();
  loadRecordsStorage();
}
$('task-records-compact').onclick=async()=>{
  const project=historyBoundProject();
  if(!project){toast('压缩数据库需要先选择具体项目',true);return;}
  const button=$('task-records-compact'),result=$('task-records-compact-result'),epoch=viewEpoch;
  button.disabled=true;if(result)result.textContent='正在确认队列空闲并压缩…';
  try{
    const data=await api('/api/db/compact',{});
    if(epoch!==viewEpoch||historyBoundProject()!==project)return;
    const reclaimed=Number(data?.reclaimed_bytes)||0;
    const compacted=Array.isArray(data?.compacted)?data.compacted.length:0;
    if(result)result.textContent=data?.message?data.message:(compacted?`已压缩 ${compacted} 个数据库，回收 ${formatBytes(reclaimed)}；记录未变化。`:'没有可压缩的数据库；记录未变化。');
    if(compacted)toast(`数据库压缩完成，回收 ${formatBytes(reclaimed)}（未删除任何记录）`);
  }catch(error){
    if(error.stale)return;
    if(result)result.textContent=`压缩未执行：${error.message}`;
  }finally{button.disabled=false;await refreshProjectStorage();}
};
$('task-records-refresh').onclick=()=>{clearHistoryToken();loadTaskRecords();};
$('task-records-preview').onclick=()=>requestHistoryPreview();
$('task-records-delete').onclick=()=>confirmHistoryDelete();
$('task-records-search').oninput=event=>{historyKeyword=event.target.value.trim();renderTaskRecords();};
document.querySelectorAll('[data-record-status]').forEach(button=>button.onclick=()=>{
  historyStatus=historyStatuses.has(button.dataset.recordStatus)?button.dataset.recordStatus:'all';
  document.querySelectorAll('[data-record-status]').forEach(item=>item.classList.toggle('active',item.dataset.recordStatus===historyStatus));
  renderTaskRecords();
});
$('task-records-select-all').onchange=event=>{
  const ids=historyRecords.filter(historyMatches).filter(historyDeletable).map(record=>String(record.id));
  if(event.target.checked)ids.forEach(id=>historySelected.add(id));else ids.forEach(id=>historySelected.delete(id));
  clearHistoryToken(); // selection no longer matches any earlier preview
  updateHistorySelectionUI();
};
$('task-records-list').addEventListener('change',event=>{
  const box=event.target.closest('input[data-record-id]');if(!box)return;
  const id=box.dataset.recordId,record=historyRecords.find(item=>String(item.id??'')===id);
  if(!historyDeletable(record)){box.checked=false;toast(record?.blocked_reason?`该记录不可删除：${record.blocked_reason}`:'该记录不可删除',true);updateHistorySelectionUI();return;}
  if(box.checked)historySelected.add(id);else historySelected.delete(id);
  clearHistoryToken(); // the preview token is bound to the previous selection
  updateHistorySelectionUI();
});
function closeTaskHistory(){
  if($('task-records-confirm-dialog')?.open)$('task-records-confirm-dialog').close();
  if($('task-records-dialog')?.open)$('task-records-dialog').close();
  resetHistoryManagement();
}
document.querySelectorAll('.close-records-dialog,.close-records-confirm').forEach(button=>button.onclick=()=>{
  if(button.classList.contains('close-records-confirm')){clearHistoryToken();if($('task-records-confirm-dialog')?.open)$('task-records-confirm-dialog').close();updateHistorySelectionUI();return;}
  closeTaskHistory();
});
// Escape / programmatic close also drops the pending confirmation, so an old token can never be reused.
$('task-records-confirm-dialog').addEventListener('close',()=>{clearHistoryToken();updateHistorySelectionUI();});
$('task-records-dialog').addEventListener('close',()=>resetHistoryManagement());
