// E2 observability panel: headless-browser checks of the real bundle against mock
// fixtures (light/dark/system, goal/stage/task/child tree, bad states, capability
// deny, collapsible details, unknown values, input validation, HTML escaping).
// Every response comes from the local mock below, so the check is offline by
// default.  Setting MODEL_DESK_LIVE_READONLY=1 additionally performs one GET of a
// running panel's /api/state; that opt-in read is manual diagnostics, not evidence.
// No model call, mutation or service restart ever happens.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('./browser_helpers.cjs');

const panelDir = path.join(__dirname, '..', 'panel');
const indexHtml = fs.readFileSync(path.join(panelDir, 'index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(panelDir, 'app.js'), 'utf8');
const styleCss = fs.readFileSync(path.join(panelDir, 'style.css'), 'utf8');

const payload = '<img src=x onerror="window.__xss=1">';
const now = new Date().toISOString();

const workflow = { version: 1, enabled: true, paid_allowed: true, enforce_paid_budget: false,
  enforce_model_call_limit: false, task_budget_cny: 5, daily_budget_cny: 20, max_model_calls: 30,
  readable: true, error: '', daily: { day: '2026-10-14', settled_cny: 0, settled_calls: 0,
    unsettled_cny: 0, legacy_reserved_cny: 4.2, unknown_calls: 0, calls: 0, has_actual: true, error: '' } };

const executorCapabilities = { schema: 1, executor: 'dsh', default_verdict: 'deny',
  note: '子代理支持状态（三态 + 证据版本/时间）与“允许派生”策略分开呈现；没有同身份实测证据时不可允许，且不因付费可启用；本地/home 禁止派生但仍可作为 worker 执行。',
  models: [
    { model_id: 'home', executor: 'dsh', policy: 'allow', state: 'supported', decision: 'deny',
      local: true, allowable: false, backend_implemented: true, worker_usable: true,
      proven_modes: ['edit'], reasons: ['用户策略禁止', '本地/home 模型本轮保持禁止派生子代理'],
      evidence_detail: { state: 'supported', measured: true, modes: ['edit'],
        tested_at: '2026-10-13T20:24:02+00:00', version: '0.2.0-rc.2',
        model_version: 'unversioned-v1', test_id: 'home-probe', source: 'fixture' },
      evidence_updated_at: '2026-10-13T20:24:02+00:00' },
    { model_id: 'cloud', executor: 'dsh', policy: 'allow', state: 'supported', decision: 'allow',
      local: false, allowable: true, backend_implemented: true, worker_usable: true,
      proven_modes: ['chat'], reasons: ['用户策略允许且存在同身份实测支持证据'],
      evidence_detail: { state: 'supported', measured: true, modes: ['chat'],
        tested_on: null, tested_at: '2026-10-13T21:27:11+00:00', version: '0.2.0-rc.2',
        model_version: 'unversioned-v1', test_id: 'cloud-probe',
        source: 'main verified read-only probe' },
      evidence_updated_at: '2026-10-13T20:24:02+00:00' },
    { model_id: 'blank', executor: 'dsh', policy: 'unset', state: 'unverified', decision: 'deny',
      local: false, allowable: false, backend_implemented: true, worker_usable: true,
      proven_modes: [], reasons: ['尚无该模型在该执行器上的实测证据', '用户未明确允许'],
      evidence_detail: null, evidence_updated_at: null },
  ] };

function model(id, name, provider, code) {
  return { id, name, provider, model: code, api_base: provider === 'local' ? 'http://127.0.0.1:1234/v1' : 'https://api.example/v1',
    enabled: true, bypass_proxy: true, api_key_set: provider !== 'local', max_input_tokens: 262144,
    max_output_tokens: 64000, preferred_tasks: [], capability_notes: '', cost_preference: 'economy',
    max_task_complexity: 'simple', agent_preset: provider === 'local' ? 'native' : 'standard',
    thinking_level: 'default', last_check: { ok: true, at: now, latency_ms: 120 } };
}

function task(overrides) {
  return { id: 'obs-unknown', title: `未取得用量 ${payload}`, kind: 'chat', engine: 'dsh',
    status: 'completed', phase: '回复完成', model_id: 'home', model_name: '家中的 Qwen',
    model: 'qwen-test', source: 'panel', created_at: now, model_calls: 1,
    files: ['a.py'], readonly_files: ['a.py'], files_readonly: true,
    request_output_tokens: 4096,
    route: { mode: 'manual', reason: '手动指定', requirements: { category: 'analysis',
      complexity: 'simple', input_tokens: 8192, output_tokens: 4096, expected_calls: 1 } },
    review: { state: 'executor_completed', label: '执行器完成 · 待独立验收', rework_count: 0, max_auto_rework: 1 },
    ...overrides };
}

function observability(id, overrides = {}) {
  const base = {
    task_id: id, title: '任务 ' + id, kind: 'chat', status: 'completed', phase: '回复完成',
    stage_id: 'stage-a', goal_id: 'goal-1', model_id: 'home', model: 'qwen-test', model_name: '家中的 Qwen',
    cost: { schema: 2, known: false, settled_cny: null, recorded_settled_cny: 0, settled_calls: 0,
      unsettled_cny: 0, reserved_cny: 0, reserved_is_real_cost: false,
      unknown_calls: 1, legacy_unsettled_cny: 0, label: '按用量计算费用 待核对',
      note: '金额未知时显示为待核对，不把预留 0 显示成未知真实费用 0。' },
    provider_usage: { available: false, live: false, partial: false, source: '未取得（服务商未返回 usage）',
      known_calls: 0, unknown_calls: 1, total_calls: 1, scope: '未取得（服务商未返回 usage）',
      input_tokens: null, cache_hit_tokens: null, cache_miss_tokens: null, output_tokens: null, total_tokens: null },
    file_scope: { mode: 'readonly_context', files: ['a.py'], readonly_files: ['a.py'],
      note: '只读上下文文件由 chat 阶段保存，用于代码索引与交接文件范围；只读权限由 kind/engine 沙箱保证。' },
    meter: { source: 'unavailable', confidence: 'unavailable', input_tokens: null, input_origin: null,
      output_target: null, output_origin: null, window_kind: null, window_verified: null,
      fit_state: null, guard: null, warnings: [], identity: null },
    context: { note: 'context_* 是字节审计，不等于 token 精确节省', bytes_before: null, bytes_after: null,
      bytes_saved: null, offloaded_count: 0, offloaded_bytes: 0, preview_bytes: 0, reused_count: 0,
      created_count: 0, not_trimmed_count: 0, already_offloaded_count: 0, skipped_reason: '', error: '', outputs: [] },
    failure: { schema: 1, task_id: id, family: id, outcome: 'unknown', reason: 'review_pending',
      label: '待独立验收', counts_as_model_quality: false, system_excluded: false, verified: false,
      confidence: 'medium', source: 'independent_review', evidence: [], note: '', read_only: true },
    forecast: { mode: 'manual', reason: '手动指定', estimated_cost_cny: null, prediction: null,
      observed_cost: null, pricing: null, retry_interval: null, retry_confidence: null, retry_samples: null,
      retry_rate: null, retry_basis: null,
      requirements: { category: 'analysis', complexity: 'simple', input_tokens: 8192, output_tokens: 4096,
        expected_calls: 1, estimate_semantics: 'approximate' },
      single_request_output: null, calibration: null },
    children: { state: '未启用/未验证（未获允许或无实测证据）', allowed: false,
      capability: { decision: 'deny', state: 'unverified', policy: 'unset', proven_modes: [], mode: null,
        reasons: ['尚无该模型在该执行器上的实测证据'] },
      count: 0, verified: 0, unverified: 0, per_child_billing: 'unavailable' },
    child_rows: [],
    billing_boundary: { parent: { settled_cny: 0, settled_calls: 0, unsettled_cny: 0, unknown_calls: 1,
      legacy_unsettled_cny: 0, schema: 2 }, children: { per_child: 'unavailable', authoritative: 'unavailable',
      aggregate: null, reason: '无子代理聚合记录' }, note: 'per-child 账单不可证实' },
    request_output: null, note: '缺失一律标未取得/未知',
  };
  return { ...base, ...overrides };
}

function treeFixture() {
  const stage = (id, title, kind, status, label, extra = {}) => ({
    id, title, objective: '阶段目标 ' + title, kind, kind_label: kind === 'chat' ? '只读分析 · chat' : '完全权限修改 · edit',
    status, status_label: label, ordinal: 1, depends_on: [], session_policy: 'new_session',
    session_key: 'desk-' + id, candidates: ['home'], fixed_model: 'home', project: 'E:\\proj',
    readiness: { ready: false, reasons: status === 'ready' ? [] : ['阶段当前状态不可派发：' + label],
      dependency_states: {}, blocked_by: [] },
    delivery: { deliveries: [], warnings: [] },
    tasks: [], ...extra });
  const scheduled = stage('stage-accepted', '只读调查', 'chat', 'accepted', '独立验收通过');
  scheduled.tasks = [{
    task_id: 'obs-settled', title: '已结算任务', kind: 'chat', status: 'completed', phase: '回复完成',
    model_id: 'cloud', model: 'deepseek-flash', model_name: '云端模型', created_at: now, ended_at: now,
    dsh_session_id: 'session-abc', dsh_session_key: 'desk-x', parent_task_id: null, rework_of: null,
    changed_files: ['a.py'], file_hashes: { 'a.py': 'abcdef0123456789' }, request_output_tokens: 32768,
    files: ['a.py'], readonly_files: ['a.py'], files_readonly: true,
    review_state: 'accepted', review_label: '独立验收通过',
    children: [
      { child_id: 'child-verified', run_id: 'run-1', provider: 'spawn', local: true,
        local_flag_note: 'local=true 指进程内 provider，不代表免费本地 API 模型',
        model_id: 'cloud', model: 'deepseek-flash', model_name: '云端模型', started_at: now, ended_at: now,
        stop_reason: 'completed', output_chars: 0, verified: true, state: '已结束',
        cost: '不可证实（DSH 无 per-child 账单，仅父级聚合）' },
      { child_id: 'child-unverified', run_id: 'run-2', provider: 'spawn', local: true,
        local_flag_note: 'local=true 指进程内 provider，不代表免费本地 API 模型',
        model_id: 'cloud', model: 'deepseek-flash', model_name: '云端模型', started_at: now, ended_at: null,
        stop_reason: null, output_chars: null, verified: false, state: '未验证 · 缺少结束事件',
        cost: '不可证实（DSH 无 per-child 账单，仅父级聚合）' },
    ],
    children_state: { state: '有实测生命周期记录', allowed: true,
      capability: { decision: 'allow', state: 'supported', policy: 'allow', proven_modes: ['chat'],
        mode: 'chat', reasons: ['用户策略允许'] },
      count: 2, verified: 1, unverified: 1, per_child_billing: 'unavailable' },
  }];
  const denied = stage('stage-denied', '未验证阶段', 'edit', 'executor_completed', '执行器完成 · 待独立验收');
  denied.tasks = [{
    task_id: 'obs-denied', title: '未验证子代理任务', kind: 'edit', status: 'completed', phase: '执行完成',
    model_id: 'blank', model: 'unknown-model', model_name: '未验证模型', created_at: now, ended_at: now,
    dsh_session_id: null, dsh_session_key: null, parent_task_id: null, rework_of: null,
    changed_files: [], file_hashes: {}, request_output_tokens: null, review_state: null, review_label: null,
    files: [], readonly_files: [], files_readonly: false,
    children: [],
    children_state: { state: '未启用/未验证（未获允许或无实测证据）', allowed: false,
      capability: { decision: 'deny', state: 'unverified', policy: 'unset', proven_modes: [], mode: null,
        reasons: ['尚无该模型在该执行器上的实测证据'] },
      count: 0, verified: 0, unverified: 0, per_child_billing: 'unavailable' },
  }];
  const goal = { id: 'goal-1', title: `工具感面板 ${payload}`, objective: '真实观测', project: 'E:\\proj',
    status: 'active', status_label: '进行中', created_at: now, updated_at: now, stage_count: 7, accepted_count: 2,
    stages: [scheduled, denied,
      stage('stage-final', '独立实现', 'edit', 'final_accepted', '主脑介入 · 最终验收通过'),
      stage('stage-returned', '退回整改', 'edit', 'returned', '退回整改'),
      stage('stage-blocked', '受阻阶段', 'edit', 'blocked', '受阻 · 需主脑判断'),
      stage('stage-cancelled', '取消阶段', 'edit', 'cancelled', '已取消'),
      stage('stage-recovery', '待恢复阶段', 'edit', 'recovery_pending', '待恢复 · 未完成派发需人工确认')],
  };
  goal.stages[0].delivery = { deliveries: [{ task_id: 'obs-settled', summary: '交接摘要', fresh: { valid: true, message: '交接文件与记录一致' }, files: [{ path: 'a.py', sha256: 'abcdef0123456789' }] }], warnings: [] };
  return { goals: [goal], stats: { goals: 1, stages: 7, accepted: 2, recovery_pending: 1 },
    recovery: [{ stage_id: 'stage-recovery', goal_id: 'goal-1', title: '待恢复阶段', task_id: 'ghost', task_status: null, reason: '面板重启时任务仍未结束' }],
    note: '阶段状态是真实持久记录' };
}

const stateFixture = {
  default_model: 'home',
  models: [model('home', '家中的 Qwen', 'local', 'qwen-test'), model('cloud', '云端模型', 'deepseek', 'deepseek-flash'), model('blank', '未验证模型', 'custom', 'unknown-model')],
  tasks: [task({})],
  workflow, executor_capabilities: executorCapabilities, delegation: { delegated: 1, executor_completed: 1, accepted: 0, returned: 0 },
  agent: { ready: true, edit_sandbox_mode: 'danger-full-access' },
  codex_usage: { status: 'ready', buckets: [], updated_at: now, refreshing: false },
  workspace: 'E:\\proj', server_time: now, stages: { stats: { goals: 1 } },
};

function settledObservability() {
  return observability('obs-settled', {
    cost: { schema: 2, known: true, settled_cny: 0.5, settled_calls: 2, unsettled_cny: 0,
      unknown_calls: 1, legacy_unsettled_cny: 1.5, label: '按用量计算费用', note: '未知不当作 0 元。' },
    provider_usage: { available: true, live: false, partial: true, source: 'provider usage（服务商返回）',
      known_calls: 2, unknown_calls: 1, total_calls: 3,
      scope: '已返回部分用量：仅已结算 2 次请求的 usage；另有 1 次请求未返回 usage（未知）。这不是全部调用或整个目标的总 tokens。',
      input_tokens: 1000, cache_hit_tokens: 900, cache_miss_tokens: 100, output_tokens: 40, total_tokens: 1040 },
    meter: { source: 'json_utf8_byte_proxy', confidence: 'approximate', input_tokens: 2000,
      input_origin: 'content_class_byte_proxy', output_target: 4096, output_origin: 'explicit_max_tokens_task_limit',
      window_kind: 'split_input_output_windows', window_verified: false, fit_state: 'unknown', guard: null,
      warnings: ['approximate 计数不用于硬拦'], identity: { executor: 'dsh' } },
    context: { note: 'context_* 是字节审计，不等于 token 精确节省', bytes_before: 878009, bytes_after: 678143,
      bytes_saved: 199866, offloaded_count: 9, offloaded_bytes: 211254, preview_bytes: 1200, reused_count: 1,
      created_count: 9, not_trimmed_count: 0, already_offloaded_count: 0, skipped_reason: '', error: '', outputs: [] },
    failure: { schema: 1, task_id: 'obs-settled', family: 'obs-settled', outcome: 'success',
      reason: 'completed_unverified', label: '独立验收通过', counts_as_model_quality: false,
      system_excluded: false, verified: true, confidence: 'high', source: 'independent_review',
      evidence: [], note: '', read_only: true },
    forecast: { mode: 'auto', reason: '自动选择', estimated_cost_cny: 3.2239, prediction: { estimated_cost_cny: 3.2 },
      observed_cost: { settled_cost_cny: 6.4 }, pricing: { known: true },
      retry_interval: { low: 1.1582, high: 1.7495, method: 'wilson_95' }, retry_confidence: 'medium',
      retry_samples: 7, retry_rate: 0.43, retry_basis: 'verified_independent_review',
      requirements: { category: 'code_multi', complexity: 'complex', input_tokens: 65000, output_tokens: 7000,
        expected_calls: 24, estimate_semantics: 'approximate' },
      single_request_output: { task_target: 32768, model_ceiling: 64000, effective_target: 32768,
        note: '模型最大输出是能力上限；任务单请求输出目标只约束本次请求' }, calibration: null },
    children: { state: '有实测生命周期记录', allowed: true,
      capability: { decision: 'allow', state: 'supported', policy: 'allow', proven_modes: ['chat'],
        mode: 'chat', reasons: ['用户策略允许'] }, count: 2, verified: 1, unverified: 1,
      per_child_billing: 'unavailable' },
    child_rows: [
      { child_id: 'child-verified', run_id: 'run-1', provider: 'spawn', local: true,
        local_flag_note: 'local=true 指进程内 provider，不代表免费本地 API 模型',
        model_id: 'cloud', model: 'deepseek-flash', model_name: '云端模型', started_at: now, ended_at: now,
        stop_reason: 'completed', output_chars: 0, verified: true, state: '已结束',
        cost: '不可证实（DSH 无 per-child 账单，仅父级聚合）' },
      { child_id: 'child-unverified', run_id: 'run-2', provider: 'spawn', local: true,
        local_flag_note: 'local=true 指进程内 provider，不代表免费本地 API 模型',
        model_id: 'cloud', model: 'deepseek-flash', model_name: '云端模型', started_at: now, ended_at: null,
        stop_reason: null, output_chars: null, verified: false, state: '未验证 · 缺少结束事件',
        cost: '不可证实（DSH 无 per-child 账单，仅父级聚合）' },
    ],
    billing_boundary: { parent: { settled_cny: 0.5, settled_calls: 2, unsettled_cny: 0, unknown_calls: 1,
      legacy_unsettled_cny: 0, schema: 2 }, children: { per_child: 'unavailable', authoritative: 'aggregate',
      aggregate: { requests: 3, settled_cny: 0.006, settled_calls: 3 }, reason: '仅聚合' }, note: 'per-child 不可证实' },
  });
}

async function newPage(browser, options = {}) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 1100 }, colorScheme: options.colorScheme || 'light' });
  const page = await context.newPage();
  const errors = [], mutations = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (request.method() !== 'GET') mutations.push(request.method() + ' ' + request.url()); });
  const tree = options.tree || treeFixture();
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname === '/api/session') return route.fulfill({ json: { token: 'ui-mock-token' } });
    if (pathname === '/api/state') return route.fulfill({ json: {...(options.state || stateFixture),view:{mode:'all',project:null}} });
    if (pathname === '/api/tree') return route.fulfill({ json: tree });
    if (pathname === '/api/observability') return route.fulfill({ json: { tasks: [], stats: { tasks: 0, shown: 0 } } });
    if (pathname === '/api/tasks/obs-settled/observability') return route.fulfill({ json: settledObservability() });
    if (pathname === '/api/tasks/obs-unknown/observability') return route.fulfill({ json: observability('obs-unknown') });
    if (pathname === '/api/tasks/obs-denied/observability') return route.fulfill({ json: observability('obs-denied') });
    if (pathname === '/app.js') return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: appJs });
    if (pathname === '/style.css') return route.fulfill({ contentType: 'text/css; charset=utf-8', body: styleCss });
    if (pathname === '/') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: indexHtml });
    return route.fulfill({ status: 404, json: { error: 'not mocked: ' + pathname } });
  });
  await page.goto('http://127.0.0.1:8899/?view=all');
  // The home page now keeps the goal tree behind one collapsed progress line; open it
  // so every existing tree assertion still reads exactly the same DOM.
  await page.locator('#goals-toggle').waitFor();
  await page.locator('#goals-toggle').click();
  await page.locator('.goal-node').first().waitFor();
  return { context, page, errors, mutations };
}

async function realApiReadOnly(report) {
  if (process.env.MODEL_DESK_LIVE_READONLY !== '1') {
    report.realApi = { skipped: true, reason: 'offline default; set MODEL_DESK_LIVE_READONLY=1 for manual diagnostics' };
    return;
  }
  const base = 'http://127.0.0.1:8765';
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  try {
    const response = await fetch(base + '/api/state', { signal: controller.signal, method: 'GET' });
    const data = await response.json();
    assert.equal(response.status, 200);
    assert.ok(Array.isArray(data.tasks), 'real /api/state must list tasks');
    assert.ok(data.workflow && typeof data.workflow.enabled === 'boolean');
    report.realApi = { reachable: true, status: response.status, models: (data.models || []).length,
      tasks: data.tasks.length, workflow_enabled: data.workflow.enabled };
  } finally {
    clearTimeout(timer);
  }
}

async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const report = { passed: false, checks: [], realApi: null, rejects: [] };
  try {
    // 1) light / dark / system theme still works with the new sections.
    let ui = await newPage(browser, { colorScheme: 'light' });
    assert.equal(await ui.page.locator('html').getAttribute('data-theme'), 'light');
    await ui.page.locator('#theme-mode').selectOption('dark');
    assert.equal(await ui.page.locator('html').getAttribute('data-theme'), 'dark');
    await ui.page.locator('#theme-mode').selectOption('system');
    await ui.page.emulateMedia({ colorScheme: 'dark' });
    await ui.page.waitForFunction(() => document.documentElement.dataset.theme === 'dark');
    await ui.page.emulateMedia({ colorScheme: 'light' });
    await ui.page.waitForFunction(() => document.documentElement.dataset.theme === 'light');
    const dailySummary = await ui.page.locator('#budget-summary').textContent();
    assert.ok(dailySummary.includes('按用量计算费用'), 'daily summary shows usage-based cost');
    assert.ok(!dailySummary.includes('旧版') && !dailySummary.includes('¥4.20'),
      'legacy reserves are not shown in the daily summary');
    report.checks.push('light/dark/system theme applied');

    // 2) real goal -> stage -> task -> child tree with honest statuses.
    const treeText = await ui.page.locator('#goals-tree').textContent();
    assert.equal(await ui.page.locator('#goal-count').textContent(), '1');
    for (const label of ['独立验收通过', '主脑介入 · 最终验收通过', '退回整改', '受阻 · 需主脑判断', '已取消',
      '待恢复 · 未完成派发需人工确认', '执行器完成 · 待独立验收', '只读分析 · chat', '完全权限修改 · edit']) {
      assert.ok(treeText.includes(label), 'tree should show ' + label);
    }
    assert.ok(treeText.includes('已核实 1 · 未核实 1'), 'child verification counts');
    assert.ok(treeText.includes('child-verified'), 'verified child id is shown');
    assert.ok(treeText.includes('未验证 · 缺少结束事件'), 'unverified child state is shown');
    assert.ok(treeText.includes('local=true 指进程内 provider'), 'local flag semantics explained');
    assert.ok(treeText.includes('模型/提供商以真实 registry 路由为准：云端模型'), 'child route from registry');
    assert.ok(treeText.includes('不可证实'), 'per-child cost is never a number');
    assert.ok(treeText.includes('未启用/未验证'), 'denied task shows unverified/unstarted children');
    assert.ok(treeText.includes('尚无该模型在该执行器上的实测证据'), 'deny reason shown');
    assert.ok(treeText.includes('只读上下文') && treeText.includes('只读权限由 kind/engine 沙箱保证'),
      'chat context files are labelled read-only, not modifiable');
    report.checks.push('goal/stage/task/child tree and all execution/review states');

    // 3) escapes: injected payload stays inert.
    assert.equal(await ui.page.evaluate(() => window.__xss), undefined);
    assert.ok((await ui.page.locator('body').textContent()).includes(payload), 'payload text escaped and shown');
    report.checks.push('HTML payload escaped and inert');

    // 4) collapsible details + lazy observability.
    const goal = ui.page.locator('.goal-node').first();
    assert.equal(await goal.evaluate(el => el.open), false, 'goal details start collapsed');
    await goal.locator('summary').first().click();
    assert.equal(await goal.evaluate(el => el.open), true);
    const stage = ui.page.locator('.stage-node').first();
    assert.equal(await stage.evaluate(el => el.open), false, 'stage details start collapsed');
    await stage.locator('summary').first().click();
    assert.equal(await stage.evaluate(el => el.open), true);
    const obs = ui.page.locator('#detail-observability');
    assert.equal(await obs.evaluate(el => el.open), false, 'observability starts collapsed');
    await obs.locator('summary').first().click();
    assert.equal(await obs.evaluate(el => el.open), true);
    await ui.page.waitForFunction(() => document.querySelector('#detail-observability').textContent.includes('未取得（服务商未返回 usage）'));
    const obsText = await obs.textContent();
    assert.ok(obsText.includes('待核对 1 次'), 'unknown calls stay explicit');
    assert.ok(obsText.includes('未取得'), 'missing values are labelled 未取得');
    assert.ok(/已结算费用\s*待核对/.test(obsText), 'no settled usage means 待核对, never a settled ¥0.00');
    assert.ok(!/已结算费用\s*¥0\.00/.test(obsText), 'no fake zero settled cost');
    assert.ok(obsText.includes('金额未知') || obsText.includes('待核对不是 ¥0.00'), 'pending amount is never shown as a real ¥0');
    assert.ok(obsText.includes('未结算预留（审计）') && obsText.includes('不是真实费用'), 'reserve is audit-only, not a real fee');
    assert.ok(!obsText.includes('旧版'), 'legacy reserve wording/amount is not shown in observability');
    assert.ok(obsText.includes('只读上下文'), 'observability shows the read-only context file scope');
    assert.ok(!/省.{0,4}\d+%/.test(obsText) && !obsText.includes('节省额度'), 'no invented saved-quota percentage');
    report.checks.push('collapsible details and unknown-value observability');

    // 5) settled observability shows provider usage, meter confidence, bytes, boundaries.
    await ui.page.locator('.task-node[data-task="obs-settled"] summary').first().click();
    await ui.page.waitForFunction(() => document.querySelector('.task-node[data-task="obs-settled"]').textContent.includes('json_utf8_byte_proxy'));
    const settled = await ui.page.locator('.task-node[data-task="obs-settled"]').textContent();
    for (const phrase of ['输入 1,000', '缓存命中 900', '输出 40', 'json_utf8_byte_proxy', 'approximate',
      '重试区间 1.1582–1.7495', 'wilson_95', '任务请求目标 32,768', '模型最大输出 64,000',
      '外置 9 个', 'per-child unavailable', '已返回部分用量', '不是全部调用或整个目标的总 tokens',
      '只读权限由 kind/engine 沙箱保证']) {
      assert.ok(settled.includes(phrase), 'settled observability should show ' + phrase);
    }
    assert.ok(!settled.includes('旧版') && !settled.includes('¥1.50'),
      'legacy reserve amount/wording is hidden even when the raw payload carries it');
    report.checks.push('settled cost, provider usage, meter, prediction interval, bytes and billing boundary');

    // 6) capability deny and local-home denial in the model edit dialog.
    await ui.page.locator('[data-model-action="edit"][data-id="home"]').click();
    let summary = await ui.page.locator('#model-subagent-summary').textContent();
    assert.ok(summary.includes('禁止派生'), 'local model shows deny policy');
    assert.ok(summary.includes('不可允许'), 'local model cannot be allowed');
    assert.ok(summary.includes('本地/home'), 'local reason is explicit');
    let detail = await ui.page.locator('#model-subagent-detail').textContent();
    assert.ok(detail.includes('home-probe'), 'evidence id/version shown');
    assert.ok(detail.includes('2026-10-13'), 'evidence time shown');
    await ui.page.locator('#model-dialog .close-dialog').first().click();

    await ui.page.locator('[data-model-action="edit"][data-id="cloud"]').click();
    summary = await ui.page.locator('#model-subagent-summary').textContent();
    assert.ok(summary.includes('支持（同身份实测）') && summary.includes('允许派生') && summary.includes('可允许'));
    detail = await ui.page.locator('#model-subagent-detail').textContent();
    assert.ok(detail.includes('chat') && !detail.includes('edit'), 'only measured chat mode is claimed');
    await ui.page.locator('#model-dialog .close-dialog').first().click();

    await ui.page.locator('[data-model-action="edit"][data-id="blank"]').click();
    summary = await ui.page.locator('#model-subagent-summary').textContent();
    assert.ok(summary.includes('未验证') && summary.includes('不可允许'));
    assert.ok(summary.includes('尚无该模型在该执行器上的实测证据'), 'unknown reason is explicit');
    await ui.page.locator('#model-dialog .close-dialog').first().click();
    report.checks.push('capability three-state, evidence version/time and denials in the model dialog');

    // 7) input validation and no hidden writes from rendering.
    assert.equal(await ui.page.locator('#goal-title').getAttribute('required'), '');
    assert.equal(await ui.page.locator('#stage-acceptance').getAttribute('required'), '');
    assert.deepEqual(await ui.page.locator('#stage-kind option').evaluateAll(options => options.map(option => option.value)), ['chat', 'edit']);
    assert.equal(await ui.page.locator('#task-request-output').getAttribute('max'), '131072');
    await ui.page.locator('#task-dialog').evaluate(el => el.close());
    await ui.page.locator('.stage-node[data-stage="stage-accepted"] [data-stage-action="dispatch"]').first().click();
    assert.equal(await ui.page.locator('#action-dialog').evaluate(el => el.open), true);
    assert.equal(await ui.page.locator('#action-reason').getAttribute('required'), '');
    assert.equal(await ui.page.locator('#action-confirm').textContent(), '派发');
    await ui.page.locator('#action-form .close-dialog').first().click();
    report.rejects = ui.mutations.slice();
    assert.deepEqual(ui.mutations, [], 'rendering and opening dialogs must not send any write request');
    report.checks.push('input validation and read-only rendering');

    // 8) dark-mode screenshots of the new panel.
    await ui.page.locator('#theme-mode').selectOption('dark');
    await ui.page.screenshot({ path: path.join(__dirname, 'panel-observability-dark.png'), fullPage: true });
    await ui.page.locator('#theme-mode').selectOption('light');
    await ui.page.screenshot({ path: path.join(__dirname, 'panel-observability-light.png'), fullPage: true });
    assert.deepEqual(ui.errors, [], 'no page errors');
    await ui.context.close();

    // 9) optional manual read-only check of an actually running panel API (GET only).
    await realApiReadOnly(report);
    report.checks.push(report.realApi.skipped
      ? 'live API check skipped (offline default)'
      : 'real panel API reachable read-only (GET /api/state)');
    report.passed = true;
    fs.writeFileSync(path.join(__dirname, 'panel-observability-ui-report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
