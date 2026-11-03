(function (global) {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const list = value => Array.isArray(value) ? value : [];
  const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const text = (value, fallback = '') => typeof value === 'string' && value ? value : fallback;
  const ACCEPTED = new Set(['accepted', 'final_accepted']);
  const engines = {pi:'Pi Agent', dsh:'DeepSeek Harness', workbuddy:'WorkBuddy', zcode:'ZCode', direct:'直接调用'};
  const labels = {accepted:'阶段已验收', final_accepted:'阶段已验收', running:'正在执行',
    dispatched:'已派发，等待状态确认', pending:'等待安排或核对', ready:'可以安排执行',
    executor_completed:'等待独立验收', returned:'需要整改', blocked:'需要处理',
    failed:'执行未成功', cancelled:'已停止', recovery_pending:'等待恢复核对'};
  function sameProject(a, b) {
    const norm = v => {const s = String(v || '').replace(/\\/g, '/').replace(/\/+$/, ''); return /^[a-z]:\//i.test(s) || s.startsWith('//') ? s.toLowerCase() : s;};
    return !!a && !!b && norm(a) === norm(b);
  }
  function scopedSnapshot(data, view) {
    if (view?.mode !== 'project' || !view.project) return {goals:[], tasks:[], error:'请先选择一个明确项目。'};
    const tree = object(data?.tree);
    const goals = list(tree.goals).filter(g => g && sameProject(g.project, view.project));
    const tasks = list(data?.tasks).filter(t => t && sameProject(t.project, view.project));
    const ignored = list(tree.goals).length - goals.length;
    return {goals, tasks, error:text(data?.error) || text(tree.error) || (!Array.isArray(tree.goals) ? '目标与阶段数据尚未读取成功。' : ''),
      note:ignored ? '部分目标缺少当前项目归属证明，未在图中展示。' : ''};
  }
  function summary(stage) {
    const s = object(stage.summary);
    if (s.schema === 1 && s.source === 'controller_projection' && typeof s.label === 'string') return s;
    // Historical fallback uses structured stage status only. No phase/title inference.
    return {label:labels[stage.status] || '状态待核对', tone:ACCEPTED.has(stage.status) ? 'success' : 'neutral',
      detail:'该记录没有共享中文状态摘要，请结合原任务和验收记录核对。', next_step:'图形只读，不会自动派发或恢复任务。'};
  }
  function buildGraph(goal) {
    const source = list(goal?.stages), limit = 80, warnings = [];
    if (Number.isInteger(goal?.stage_count) && goal.stage_count > source.length) warnings.push('当前响应只包含 ' + source.length + ' / ' + goal.stage_count + ' 个阶段，图中不是完整计划。');
    const counts = new Map();
    for (const s of source) if (s && typeof s.id === 'string' && s.id) counts.set(s.id, (counts.get(s.id) || 0) + 1);
    if (source.some(s => !s || typeof s.id !== 'string' || !s.id)) warnings.push('有阶段缺少有效标识，未绘制。');
    if ([...counts.values()].some(n => n > 1)) warnings.push('存在重复阶段标识，歧义节点未绘制。');
    const stages = source.filter(s => s && counts.get(s.id) === 1 && typeof s.id === 'string').slice(0, limit);
    if (source.length > limit) warnings.push('图中最多展示 80 个阶段；未展示的前置不会被视为已满足。');
    const byId = new Map(stages.map(s => [s.id, s]));
    const nodes = stages.map(stage => {
      const deps = [...new Set(list(stage.depends_on).filter(id => typeof id === 'string' && id))];
      return {id:stage.id, stage, deps, missing:deps.filter(id => !byId.has(id)), rank:0, cyclic:false};
    });
    const lookup = new Map(nodes.map(n => [n.id, n])), edges = [];
    const remaining = new Map(nodes.map(n => [n.id, n.deps.filter(d => lookup.has(d)).length]));
    const children = new Map(nodes.map(n => [n.id, []]));
    for (const n of nodes) for (const dep of n.deps) if (lookup.has(dep)) {edges.push({source:dep, target:n.id}); children.get(dep).push(n.id);}
    const queue = nodes.filter(n => remaining.get(n.id) === 0).map(n => n.id), visited = new Set();
    for (let i = 0; i < queue.length; i++) {
      const id = queue[i], node = lookup.get(id); visited.add(id);
      for (const child of children.get(id)) {
        const next = lookup.get(child); next.rank = Math.max(next.rank, node.rank + 1);
        remaining.set(child, remaining.get(child) - 1);
        if (remaining.get(child) === 0) queue.push(child);
      }
    }
    const unresolved = nodes.filter(n => !visited.has(n.id));
    if (unresolved.length) {
      warnings.push('依赖存在环路或受环路影响；相关阶段单独列出，请核对原计划。');
      const rank = Math.max(0, ...nodes.filter(n => visited.has(n.id)).map(n => n.rank)) + 1;
      unresolved.forEach(n => {n.cyclic = true; n.rank = rank;});
    }
    if (nodes.some(n => n.missing.length)) warnings.push('存在未读取到的前置阶段，不推断其验收状态。');
    // Broken dependency sets do not masquerade as a runnable chain.
    const visibleEdges = edges.filter(e => !lookup.get(e.source).cyclic && !lookup.get(e.target).cyclic);
    const ranks = [...new Set(nodes.map(n => n.rank))].sort((a, b) => a - b);
    return {nodes, edges:visibleEdges, layers:ranks.map(rank => nodes.filter(n => n.rank === rank)), warnings};
  }
  function attempts(stage, tasks) {
    const index = new Map(list(tasks).map(t => [t.id, t]));
    const entries = new Map(list(stage.tasks).filter(t => t?.task_id).map(t => [t.task_id, t]));
    const ids = [...new Set([...list(stage.task_ids), ...entries.keys()].filter(id => typeof id === 'string' && id))];
    if (stage.active_task_id && !ids.includes(stage.active_task_id)) ids.push(stage.active_task_id);
    return ids.map(id => ({...object(entries.get(id)), ...object(index.get(id)), id}));
  }
  function identity(stage, tasks) {
    const rows = attempts(stage, tasks), current = rows.find(t => t.id === stage.active_task_id);
    if (!current) return stage.fixed_model ? '计划模型：' + stage.fixed_model : '尚无当前执行任务';
    return (engines[current.engine] || '执行端未记录') + ' · ' + text(current.model_name, text(current.model, text(current.model_id, '模型未记录')));
  }
  function nodeMarkup(node, selected, tasks) {
    const s = summary(node.stage), tone = ['success', 'warning', 'danger', 'accent'].includes(s.tone) ? s.tone : 'neutral';
    return '<button type="button" class="tg-node tg-tone-' + tone + '" data-tg-node="' + esc(node.id) + '" aria-pressed="' + (node.id === selected) + '">' +
      '<span class="tg-node-title">' + esc(text(node.stage.title, '未命名阶段')) + '</span><span class="tg-state"><span class="tg-dot" aria-hidden="true"></span>' + esc(s.label) + '</span>' +
      '<span class="tg-meta">' + esc(identity(node.stage, tasks)) + '</span>' +
      (node.missing.length || node.cyclic ? '<span class="tg-invalid">依赖需要核对</span>' : '') + '</button>';
  }
  function detailMarkup(node, graph, tasks) {
    const stage = node.stage, s = summary(stage), rows = attempts(stage, tasks);
    const block = (title, body) => '<section class="tg-detail-section"><h4>' + title + '</h4>' + body + '</section>';
    const p = value => '<p>' + esc(value) + '</p>';
    const deps = node.deps.length ? node.deps.map(id => {
      const found = graph.nodes.find(n => n.id === id);
      return '<div class="tg-dependency">' + (found ? '<button type="button" class="tg-link" data-tg-jump="' + esc(id) + '">' + esc(text(found.stage.title, id)) + '</button><span>' + esc(summary(found.stage).label) + '</span>' : '<span>' + esc(id) + '</span><span>前置记录未取得</span>') + '</div>';
    }).join('') : p('没有记录前置阶段；这不代表已经派发。');
    const reasons = list(stage.readiness?.reasons).filter(x => typeof x === 'string');
    const deliveries = list(stage.delivery?.deliveries);
    const files = list(stage.files).filter(x => typeof x === 'string');
    const taskRows = rows.map(t => {
      const review = object(t.review), result = object(t.execution_result), ts = object(result.summary);
      return '<div class="tg-attempt"><button type="button" class="tg-link" data-tg-task="' + esc(t.id) + '">' + esc(text(t.title, t.id)) + '</button><p class="tg-meta">' + esc((engines[t.engine] || '执行端未记录') + ' · ' + text(t.model_name, text(t.model, text(t.model_id, '模型未记录')))) + '</p><p class="tg-meta">' + esc(ts.label || ({completed:'执行结束，另见验收记录',failed:'原执行失败记录保留',running:'正在执行',queued:'等待执行',cancelled:'已停止',recovery_pending:'等待恢复核对'}[t.status] || '执行状态待核对')) + '</p><p class="tg-meta">' + esc(t.review_label || review.label || '验收结论见原任务') + '</p></div>';
    }).join('') || p('尚未关联执行任务。');
    const integrations=[...new Set(rows.flatMap(t=>list(t.execution_result?.integration?.records).map(r=>text(r?.name))).filter(Boolean))];
    const handovers = deliveries.map(d => {
      const fresh = object(d.fresh);
      const fileList = list(d.files).map(f => '<li><code>' + esc(text(f?.path)) + '</code></li>').join('');
      return '<div class="tg-handover"><p>' + esc(fresh.valid === true ? '交接依据当前有效' : fresh.stale === true ? '交接依据已过期' : '交接依据需要核对') + '</p>' +
        (text(d.summary) ? p(d.summary) : '') + (fileList ? '<ul>' + fileList + '</ul>' : '') + '<button type="button" class="tg-link" data-tg-task="' + esc(text(d.task_id)) + '">查看交付任务</button></div>';
    }).join('');
    const criteria = list(stage.acceptance).map(c => typeof c === 'string' ? c : text(c?.text, text(c?.criterion))).filter(Boolean);
    return '<div class="tg-kicker">选中的阶段</div><h3>' + esc(text(stage.title, '未命名阶段')) + '</h3><p class="tg-selected-state">' + esc(s.label) + '</p>' +
      block('这一步做什么', p(text(stage.objective, '尚未记录阶段说明。'))) +
      block('当前情况', p(text(s.detail, '状态说明未记录。')) + (node.cyclic ? p('依赖图存在异常，不代表阶段可执行。') : '') + (reasons.length ? '<details><summary>查看就绪检查原始原因</summary><ul>' + reasons.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul></details>' : '')) +
      block('前置阶段', deps) + block('接下来怎么处理', p(text(s.next_step, '由主脑核对后决定；图中不自动推进。'))) +
      block('关联任务与执行端', taskRows) + block(stage.kind === 'chat' ? '只读上下文' : '预期交付', files.length ? '<ul>' + files.map(f => '<li><code>' + esc(f) + '</code></li>').join('') + '</ul>' : p(stage.kind === 'chat' ? '未列出只读上下文文件。' : '未列出预期文件；不代表没有成果。')) +
      (criteria.length ? block('验收条件', '<ul>' + criteria.map(c => '<li>' + esc(c) + '</li>').join('') + '</ul>') : '') +
      block('成果与交接', handovers || p('尚无可用交接记录；预期文件不是实际交付证明。')) +
      (integrations.length?block('相关集成作业',integrations.map(name=>'<button type="button" class="tg-link" data-tg-integration="'+esc(name)+'">查看 '+esc(name)+'</button>').join('')):'')+
      (list(stage.delivery?.warnings).length ? p('部分交接记录读取异常，请在原任务中核对。') : '') +
      '<details class="tg-ids"><summary>记录标识</summary>' + p(stage.id) + '</details>';
  }
  let instance = null;
  function create({getView, getData, onTask = () => {}, onIntegration = () => {}, toast = () => {}}) {
    if (instance) return instance;
    const doc = global.document;
    if (!doc?.body) return null;
    const dialog = doc.createElement('dialog');
    dialog.id = 'task-graph-dialog'; dialog.className = 'tg-dialog'; dialog.setAttribute('aria-labelledby', 'tg-title');
    dialog.innerHTML = '<div class="dialog-heading"><div><span class="eyebrow">任务协作 · 只读</span><h2 id="tg-title">任务关系图</h2></div><button type="button" class="icon-button tg-close" aria-label="关闭任务关系图">×</button></div>' +
      '<div class="tg-toolbar"><label for="tg-goal">当前目标<select id="tg-goal"></select></label><p class="tg-project"></p></div>' +
      '<p class="tg-error" role="alert" hidden></p><p class="tg-note" hidden></p>' +
      '<div class="tg-layout"><section class="tg-canvas" aria-label="阶段依赖图"><p class="tg-guide">从上往下看；连线只表示已记录的前置依赖。点击阶段查看说明。</p><div class="tg-overview" role="status"></div><p class="tg-warning" hidden></p><div class="tg-graph"></div></section><aside class="tg-detail" aria-label="阶段说明" aria-live="polite"></aside></div>' +
      '<div class="tg-loose" hidden></div><p class="tg-footer">方框表示计划阶段，关联任务见详情。同层排布不证明正在并发；是否执行取决于验收、授权与可用资源。查看不会派发、同步或修改记录。</p>';
    doc.body.appendChild(dialog);
    const find = sel => dialog.querySelector(sel);
    let goalId = '', selected = '', signature = '', focusBefore = null, current = null, frame = null;
    const nodes = () => [...dialog.querySelectorAll('[data-tg-node]')];
    function focusNode(id) {nodes().find(b => b.dataset.tgNode === id)?.focus();}
    function draw() {
      frame = null;
      const area = find('.tg-graph'), svg = find('.tg-wires');
      if (!dialog.open || !svg || !current) return;
      const box = area.getBoundingClientRect(); if (!box.width) return;
      svg.setAttribute('viewBox', '0 0 ' + box.width + ' ' + box.height);
      const lookup = new Map(nodes().map(b => [b.dataset.tgNode, b.getBoundingClientRect()]));
      svg.querySelector('g').innerHTML = current.graph.edges.map(e => {
        const a = lookup.get(e.source), b = lookup.get(e.target); if (!a || !b) return '';
        const x = a.left + a.width / 2 - box.left, y = a.bottom - box.top + 1;
        const tx = b.left + b.width / 2 - box.left, ty = b.top - box.top - 5;
        const highlight = e.source === selected || e.target === selected;
        return '<path class="' + (highlight ? 'tg-edge-selected' : '') + '" d="M' + x + ' ' + y + ' C' + x + ' ' + (y + 17) + ',' + tx + ' ' + (ty - 17) + ',' + tx + ' ' + ty + '" marker-end="url(#tg-arrow)"/>';
      }).join('');
    }
    function scheduleDraw() {if (frame === null) frame = global.requestAnimationFrame(draw);}
    const observer = global.ResizeObserver ? new global.ResizeObserver(scheduleDraw) : null;
    observer?.observe(find('.tg-graph'));
    function render() {
      if (!dialog.open) return;
      const view = getView();
      if (view?.mode !== 'project' || !view.project) {dialog.close(); return;}
      const snapshot = scopedSnapshot(getData(), view);
      const nextSignature = JSON.stringify([view.project, snapshot, goalId, selected]);
      if (signature === nextSignature) return;
      const focused = doc.activeElement, focusId = focused?.dataset?.tgNode;
      const sideScroll = find('.tg-detail').scrollTop, graphScroll = find('.tg-canvas').scrollTop;
      const goals = [...snapshot.goals].sort((a, b) => Number(b.status === 'active') - Number(a.status === 'active'));
      if (!goals.some(g => g.id === goalId)) {goalId = text(goals[0]?.id); selected = '';}
      const goal = goals.find(g => g.id === goalId), graph = buildGraph(goal);
      if (!graph.nodes.some(n => n.id === selected)) selected = graph.nodes.find(n => n.stage.active_task_id && !ACCEPTED.has(n.stage.status))?.id || graph.nodes[0]?.id || '';
      current = {snapshot, graph};
      find('.tg-project').textContent = view.project;
      const picker = find('#tg-goal');
      const options = goals.map(g => '<option value="' + esc(g.id) + '">' + esc(text(g.title, '未命名目标')) + (g.status === 'active' ? '' : g.status === 'cancelled' ? ' · 已取消' : g.status === 'completed' ? ' · 已完成' : ' · 状态待核对') + '</option>').join('');
      if (picker.innerHTML !== options) picker.innerHTML = options;
      picker.value = goalId; picker.disabled = !goals.length;
      find('.tg-error').textContent = snapshot.error ? '数据未能确认' + (goals.length ? '（显示最近读取内容，不代表当前状态）' : '') + '：' + snapshot.error : '';
      find('.tg-error').hidden = !snapshot.error;
      find('.tg-note').textContent = snapshot.note; find('.tg-note').hidden = !snapshot.note;
      find('.tg-warning').textContent = graph.warnings.join(' '); find('.tg-warning').hidden = !graph.warnings.length;
      const count = graph.nodes.length, accepted = graph.nodes.filter(n => ACCEPTED.has(n.stage.status)).length;
      find('.tg-overview').textContent = goal ? '阶段 ' + accepted + ' / ' + count + ' 已验收' + (goal.status === 'cancelled' ? ' · 目标已取消，不会自动恢复' : goal.status === 'completed' ? ' · 目标已完成' : ' · 执行结束仍需独立验收') : '当前项目没有可展示的目标';
      find('.tg-graph').innerHTML = count ? '<svg class="tg-wires" aria-hidden="true"><defs><marker id="tg-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L8,4 L0,8"/></marker></defs><g></g></svg>' + graph.layers.map((layer, i) => '<section class="tg-layer" aria-label="依赖层 ' + (i + 1) + '">' + layer.map(n => nodeMarkup(n, selected, snapshot.tasks)).join('') + '</section>').join('') : '<div class="tg-empty"><strong>' + (goal ? '这个目标尚未记录阶段' : '尚未建立目标与阶段关系') + '</strong><p>只展示已有依赖，不根据任务标题或创建顺序猜测流程。可由主脑在现有目标与阶段功能中建立计划。</p></div>';
      find('.tg-detail').innerHTML = selected ? detailMarkup(graph.nodes.find(n => n.id === selected), graph, snapshot.tasks) : '<p>选择一个阶段，查看用途、前置条件、执行端及交付记录。</p>';
      const related = new Set(snapshot.goals.flatMap(g => list(g.stages).flatMap(s => [...list(s.task_ids), ...list(s.tasks).map(t => t?.task_id), s.active_task_id])).filter(Boolean));
      const loose = snapshot.tasks.filter(t => !related.has(t.id));
      find('.tg-loose').hidden = !loose.length;
      find('.tg-loose').innerHTML = loose.length ? '<details><summary>未关联阶段的任务 · ' + loose.length + ' 项</summary><p>这些任务不自动连线，也不计入目标阶段进度。</p>' + loose.map(t => '<button type="button" class="tg-link" data-tg-task="' + esc(t.id) + '">' + esc(text(t.title, t.id)) + '</button>').join('') + '</details>' : '';
      signature = JSON.stringify([view.project, snapshot, goalId, selected]);
      find('.tg-detail').scrollTop = sideScroll; find('.tg-canvas').scrollTop = graphScroll;
      if (focusId) focusNode(focusId);
      scheduleDraw();
    }
    dialog.addEventListener('click', event => {
      if (event.target.closest('.tg-close')) {dialog.close(); return;}
      const integration=event.target.closest('[data-tg-integration]');
      if(integration){const name=integration.dataset.tgIntegration;dialog.close();onIntegration(name);return;}
      const button = event.target.closest('[data-tg-node],[data-tg-jump],[data-tg-task]'); if (!button) return;
      if (button.dataset.tgTask) {
        const id = button.dataset.tgTask;
        if (!current?.snapshot.tasks.some(t => t.id === id)) {toast('当前任务列表未取得这条记录，请到任务记录管理中核对。', true); return;}
        dialog.close(); onTask(id); return;
      }
      selected = button.dataset.tgNode || button.dataset.tgJump; signature = ''; render(); find('.tg-detail').scrollTop = 0; focusNode(selected);
    });
    find('#tg-goal').addEventListener('change', event => {goalId = event.target.value; selected = ''; signature = ''; render();});
    dialog.addEventListener('close', () => {if (frame !== null) global.cancelAnimationFrame(frame); frame = null; current = null; signature = ''; try {if (focusBefore?.isConnected) focusBefore.focus();} catch {}});
    instance = {
      open() {const view = getView(); if (view?.mode !== 'project' || !view.project) {toast('请先选择一个明确项目，再查看任务关系。', true); return false;} if (!dialog.open) {focusBefore = doc.activeElement; dialog.showModal();} signature = ''; render(); return true;},
      update:render,
      onProjectChange() {if (dialog.open) dialog.close(); goalId = ''; selected = ''; signature = ''; current = null;}
    };
    return instance;
  }
  const exported = {create, _test:{sameProject, scopedSnapshot, summary, buildGraph, attempts, identity, nodeMarkup, detailMarkup}};
  global.ModelDeskTaskGraph = exported;
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
})(typeof window !== 'undefined' ? window : globalThis);
