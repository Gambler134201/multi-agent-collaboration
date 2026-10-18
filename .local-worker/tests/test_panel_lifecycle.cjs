'use strict';
/**
 * Offline lifecycle tests for the workbench panel front end (panel/app.js).
 *
 * The real panel script is executed inside a Node `vm` against a small fake
 * browser (document/window/storage/fetch) and a virtual clock, so the polling
 * lifecycle, the idle throttle and the "unchanged markup is not rebuilt" rule are
 * verified without starting the panel service, without a provider call and
 * without touching any real workbench record or setting.
 *
 *   node .local-worker/tests/test_panel_lifecycle.cjs
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const panelDir = path.join(__dirname, '..', 'panel');
const appSource = fs.readFileSync(path.join(panelDir, 'app.js'), 'utf8');
const htmlSource = fs.readFileSync(path.join(panelDir, 'index.html'), 'utf8');

const PROJ_A = 'C:\\projects\\alpha';
const PROJ_B = 'C:\\projects\\beta';
const T0 = Date.parse('2026-01-01T00:00:00Z');

// Exposes the panel's private lifecycle state to the test without changing it.
const EXPORT = `;({
  poll:()=>({timer:pollTimer,dueAt:pollDueAt,chain:pollChain,inFlight:!!pollInFlight,
             interval:pollIntervalMs(),visible:panelVisible(),live:hasLiveWork(),failure:pollFailure}),
  refresh:()=>refresh(),
  startAutoPoll:()=>startAutoPoll(),
  stopAutoPoll:()=>stopAutoPoll(),
  readState:()=>state,
})`;

// ------------------------------------------------------------------ fake browser
let nodeSeq = 0;

class El {
  constructor(id, tag = 'div') {
    this.id = id;
    this.tagName = String(tag).toUpperCase();
    this.dataset = {};
    this.style = {};
    this.attrs = {};
    this.classSet = new Set();
    this.listeners = {};
    this.children = [];
    this.stubs = new Map();
    this._text = '';
    this._html = '';
    // Every innerHTML assignment replaces the subtree, so a new batch of node
    // identities is created; the test compares these to prove nodes survive.
    this.writes = 0;
    this.nodeIds = [];
  }
  get textContent() { return this._text; }
  set textContent(value) { this._text = value === null || value === undefined ? '' : String(value); }
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    const markup = value === null || value === undefined ? '' : String(value);
    this.writes += 1;
    this._html = markup;
    this.nodeIds = Array.from({ length: (markup.match(/<[a-zA-Z]/g) || []).length }, () => ++nodeSeq);
  }
  get className() { return this.attrs.class || ''; }
  set className(value) { this.attrs.class = String(value); }
  get hidden() { return this._hidden === true; }
  set hidden(value) { this._hidden = value === true; }
  get classList() {
    const self = this;
    return {
      add: (...names) => names.forEach(name => self.classSet.add(name)),
      remove: (...names) => names.forEach(name => self.classSet.delete(name)),
      toggle: (name, force) => {
        const on = force === undefined ? !self.classSet.has(name) : !!force;
        if (on) self.classSet.add(name); else self.classSet.delete(name);
        return on;
      },
      contains: name => self.classSet.has(name),
    };
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  removeEventListener(type, handler) {
    const list = this.listeners[type] || [];
    const index = list.indexOf(handler);
    if (index >= 0) list.splice(index, 1);
  }
  dispatch(event) {
    (this.listeners[event.type] || []).forEach(handler => handler(Object.assign({target: this, type: event.type}, event)));
    return true;
  }
  querySelector(selector) {
    if (!this.stubs.has(selector)) this.stubs.set(selector, new El('', 'div'));
    return this.stubs.get(selector);
  }
  querySelectorAll() { return []; }
  append(child) { this.children.push(child); return child; }
  appendChild(child) { return this.append(child); }
  // The panel fills <select> elements with `new Option(...)` through replaceChildren;
  // the fake browser needs the same two entry points, nothing more.
  replaceChildren(...nodes) { this.children = nodes.slice(); }
  closest() { return null; }
  contains() { return false; }
  focus() {}
  blur() {}
  scrollIntoView() {}
  remove() {}
  close() { this.open = false; }
  showModal() { this.open = true; }
}

function createHarness(options = {}) {
  // ------------------------------------------------------------- virtual clock
  let now = T0;
  const timers = new Map();
  let timerSeq = 0;
  const clock = {
    get now() { return now; },
    set now(value) { now = value; },
    setTimeout(fn, ms) { const id = ++timerSeq; timers.set(id, {fn, at: now + Math.max(0, Number(ms) || 0)}); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn, ms) {
      const period = Math.max(1, Number(ms) || 1);
      const id = ++timerSeq;
      const step = () => { if (!timers.has(id)) return; timers.set(id, {fn: step, at: now + period}); fn(); };
      timers.set(id, {fn: step, at: now + period});
      return id;
    },
    clearInterval(id) { timers.delete(id); },
    pending() { return timers.size; },
    due(maxAt) {
      let best = null;
      for (const [id, timer] of timers) if (timer.at <= maxAt && (!best || timer.at < best.timer.at)) best = {id, timer};
      return best;
    },
  };
  const flush = async () => { for (let i = 0; i < 20; i += 1) await new Promise(resolve => setImmediate(resolve)); };
  async function advance(ms) {
    const target = now + ms;
    for (let guard = 0; guard < 5000; guard += 1) {
      const due = clock.due(target);
      if (!due) break;
      timers.delete(due.id);
      now = due.timer.at;
      due.timer.fn();
      await flush();
    }
    now = target;
    await flush();
  }
  const RealDate = Date;
  class FakeDate extends RealDate {
    constructor(...args) { if (args.length === 0) super(now); else super(...args); }
    static now() { return now; }
  }

  // ------------------------------------------------------------- fake document
  const elements = new Map();
  const el = id => { if (!elements.has(id)) elements.set(id, new El(id)); return elements.get(id); };
  const document = {
    hidden: false,
    documentElement: new El('#documentElement', 'html'),
    body: new El('#body', 'body'),
    getElementById: el,
    createElement: tag => new El('', tag),
    querySelector: () => null,
    querySelectorAll: () => [],
    listeners: {},
    addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); },
    removeEventListener() {},
    dispatch(event) { (this.listeners[event.type] || []).forEach(handler => handler(event)); },
  };
  const window = {
    location: {search: options.search || '', href: 'http://127.0.0.1:8765/', pathname: '/'},
    history: {replaceState() {}},
    matchMedia: () => ({matches: false, addEventListener() {}, addListener() {}}),
    modelDeskInitialView: options.initialView === undefined ? {mode: 'project', project: PROJ_A} : options.initialView,
    confirm: () => false,
    listeners: {},
    addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); },
    removeEventListener() {},
    dispatch(event) { (this.listeners[event.type] || []).forEach(handler => handler(event)); },
  };
  const storage = () => {
    const store = new Map();
    return {getItem: key => (store.has(key) ? store.get(key) : null),
            setItem: (key, value) => store.set(key, String(value)),
            removeItem: key => store.delete(key),
            clear: () => store.clear()};
  };

  // ------------------------------------------------------------- fake service
  const server = {
    failState: false,
    reads: 0,
    models: [{id: 'm-local', name: '本地模型', model: 'qwen-local', provider: 'local',
              api_base: 'http://127.0.0.1:11434/v1', enabled: true, billing: null,
              last_check: {ok: true, latency_ms: 120}, thinking: {label: '引擎默认'}}],
    projects: new Map([
      [PROJ_A, {tasks: [completedTask('t-alpha', '甲项目任务')],
        tree: treeOf('阶段一', 'running', '执行中')}],
      [PROJ_B, {tasks: [completedTask('t-beta', '乙项目任务')],
        tree: treeOf('阶段一', 'running', '执行中')}],
    ]),
  };
  function completedTask(id, title) {
    return {id, title, kind: 'edit', status: 'completed', phase: '执行完成', source: 'panel',
            engine: 'dsh', model: 'm-local', model_id: 'm-local', model_name: '本地模型',
            created_at: '2026-01-01T00:00:00Z', started_at: '2026-01-01T00:00:00Z',
            ended_at: '2026-01-01T00:05:00Z', answer: '已完成', changed_files: ['src/a.py'],
            usage: {total_tokens: 120}, cost_settled_cny: 0.1, cost_settled_calls: 1,
            cost_unknown_calls: 0, cost_unsettled_cny: 0, tool_calls: [{status: 'completed'}],
            review: {state: 'accepted', label: '独立验收通过', updated_at: '2026-01-01T00:06:00Z',
                     rework_count: 0, max_auto_rework: 2, final: {accepted_at: '2026-01-01T00:06:00Z'}}};
  }
  function runningTask(id, title) {
    return {id, title, kind: 'edit', status: 'running', phase: '模型执行中', source: 'panel',
            engine: 'dsh', model: 'm-local', model_id: 'm-local', model_name: '本地模型',
            created_at: '2026-01-01T00:00:00Z', started_at: '2026-01-01T00:00:00Z',
            ended_at: null, answer: '', changed_files: [], usage: {total_tokens: 10},
            cost_settled_cny: 0, cost_settled_calls: 0, cost_unknown_calls: 0, cost_unsettled_cny: 0,
            tool_calls: [{status: 'in_progress'}], review: null};
  }
  function treeOf(stageTitle, stageStatus, stageLabel) {
    return {error: null, stats: {stages: 1, accepted: 0, recovery_pending: 0},
            goals: [{id: 'g1', title: '目标一', status: 'active', status_label: '进行中',
                     stage_count: 1, accepted_count: 0, objective: '调查并实现',
                     stages: [{id: 's1', ordinal: 1, title: stageTitle, status: stageStatus,
                               status_label: stageLabel, kind: 'implement', kind_label: '实现',
                               objective: '实现改动', session_policy: 'new_session', session_key: 'k1',
                               depends_on: [], readiness: {ready: true, reasons: []},
                               delivery: {deliveries: [], warnings: []}, tasks: []}]}]};
  }
  function projectOf(view) {
    const key = view && view.mode === 'project' ? view.project : null;
    return (key && server.projects.get(key)) || {tasks: [], tree: {error: null, stats: {}, goals: []}};
  }
  function statePayload(view) {
    server.reads += 1;
    const data = projectOf(view);
    return {view, server_time: T0 + server.reads,                 // changes on every read by design
            models: server.models, default_model: 'm-local',
            tasks: data.tasks, workspace: 'C:\\workbench', execution: '任务按顺序执行',
            agent: {name: 'DeepSeek Harness', version: '1.0', ready: true},
            execution_agents: {dsh: {name: 'DeepSeek Harness'}, pi: {name: 'Pi Agent'}},
            execution_activity: {scope: 'project', tasks: [], complete: true},
            workflow: {enabled: true, paid_allowed: false, enforce_paid_budget: true,
                       enforce_model_call_limit: true, task_budget_cny: 1, daily_budget_cny: 2,
                       max_model_calls: 5, daily: {settled_cny: 0.1, has_actual: true}},
            codex_usage: {status: 'ready', buckets: [], updated_at: '2026-01-01T00:00:00Z'},
            delegation: null, executor_capabilities: {}, stages: {stats: {}, recovery: []}};
  }
  function treePayload(view) { return projectOf(view).tree; }
  function decodeView(header) {
    if (!header) return {mode: 'unbound', project: null};
    return JSON.parse(Buffer.from(String(header).replace(/-/g, '+').replace(/_/g, '/'), 'base64').toString('utf8'));
  }
  function header(rec, name) {
    const wanted = String(name).toLowerCase();
    for (const [key, value] of Object.entries(rec.headers || {})) if (key.toLowerCase() === wanted) return value;
    return undefined;
  }
  function route(rec) {
    const routePath = rec.path.split('?')[0];
    const view = decodeView(header(rec, 'X-Model-Desk-View'));
    if (routePath === '/api/session') return {status: 200, body: {token: 'panel-token'}};
    if (routePath === '/api/state') {
      if (server.failState) return {status: 500, body: {error: '服务暂时不可用'}};
      return {status: 200, body: statePayload(view)};
    }
    if (routePath === '/api/tree') return {status: 200, body: treePayload(view)};
    if (routePath === '/api/projects') return {status: 200, body: {projects: [], recent_projects: []}};
    if (routePath === '/api/db/space') {
      return {status: 200, body: {databases: [{scope: 'project', path: view.project || '', initialized: true,
                                              exists: true, writable: true, size_bytes: 2048}]}};
    }
    if (/^\/api\/tasks\/[^/]+\/observability$/.test(routePath)) return {status: 200, body: {}};
    if (/^\/api\/tasks\/[^/]+\/log$/.test(routePath)) return {status: 200, body: {text: '执行日志'}};
    if (/^\/api\/tasks\/[^/]+$/.test(routePath)) return {status: 200, body: {prompt: '任务提示'}};
    return {status: 404, body: {error: '未知接口'}};
  }
  const net = {requests: [], inFlight: 0, maxInFlight: 0, stateInFlight: 0, maxStateInFlight: 0, hold: false, held: []};
  function fetchMock(url, init = {}) {
    const record = {path: String(url), method: init.method || 'GET', headers: init.headers || {}, at: now};
    net.requests.push(record);
    const isState = record.path.split('?')[0] === '/api/state';
    net.inFlight += 1;
    if (net.inFlight > net.maxInFlight) net.maxInFlight = net.inFlight;
    if (isState) {
      net.stateInFlight += 1;
      if (net.stateInFlight > net.maxStateInFlight) net.maxStateInFlight = net.stateInFlight;
    }
    const settle = result => {
      net.inFlight -= 1;
      if (isState) net.stateInFlight -= 1;
      return {ok: result.status < 400, status: result.status, json: async () => result.body};
    };
    if (net.hold) return new Promise(resolve => net.held.push({record, resolve: result => resolve(settle(result))}));
    return Promise.resolve(settle(route(record)));
  }
  function releaseHeld() {
    const pending = net.held.splice(0, net.held.length);
    pending.forEach(entry => entry.resolve(route(entry.record)));
  }

  // ------------------------------------------------------------- run app.js
  const context = {
    window, document, localStorage: storage(), sessionStorage: storage(),
    fetch: fetchMock, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    requestAnimationFrame: fn => clock.setTimeout(fn, 16),
    Date: FakeDate, URL, URLSearchParams, btoa, unescape, console,
    HTMLDetailsElement: class HTMLDetailsElement {},
    Option: class Option { constructor(text, value) { this.text = text; this.value = value; } },
    Event: class Event { constructor(type, init) { this.type = type; Object.assign(this, init || {}); } },
    CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; Object.assign(this, init || {}); } },
  };
  vm.createContext(context);
  const panel = vm.runInContext(appSource + EXPORT, context, {filename: 'panel/app.js'});

  const stateCalls = () => net.requests.filter(rec => rec.path === '/api/state');
  return {clock, advance, flush, net, server, panel, el, document, window,
          decodeView, header, completedTask, runningTask, treeOf, stateCalls, releaseHeld,
          async boot() { await flush(); await flush(); }};
}

// ------------------------------------------------------------------------ runner
const failures = [];
let checks = 0;
async function test(name, body) {
  try {
    await body();
    checks += 1;
    console.log('ok   - ' + name);
  } catch (error) {
    failures.push({name, error});
    console.log('FAIL - ' + name + ' :: ' + (error && error.message));
  }
}

(async () => {
  assert.ok(htmlSource.includes('id="task-list"') && htmlSource.includes('id="goals-tree"'),
    'the panel markup must still expose the containers the lifecycle test watches');

  await test('boot starts exactly one auto chain and renders the bound project', async () => {
    const h = createHarness();
    await h.boot();
    assert.ok(h.net.requests.some(rec => rec.path === '/api/session'), 'session token is read first');
    assert.ok(h.net.requests.some(rec => rec.path === '/api/db/space'), 'project storage state is read');
    assert.equal(h.panel.poll().inFlight, false, 'no request may stay open after boot');
    assert.notEqual(h.panel.poll().timer, null, 'one auto poll is scheduled');
    assert.equal(h.clock.pending(), 1, 'exactly one auto timer is pending, got ' + h.clock.pending());
    assert.ok(h.el('task-list').innerHTML.includes('甲项目任务'), 'project task is rendered');
    assert.equal(h.decodeView(h.header(h.net.requests.find(r => r.path === '/api/state'), 'X-Model-Desk-View')).project,
      PROJ_A, 'the first read carries the bound project view');
  });

  await test('a settled document drops from the 2s cadence to the idle cadence', async () => {
    const h = createHarness();
    await h.boot();
    const start = h.clock.now;
    await h.advance(90000);
    const calls = h.stateCalls();
    assert.ok(calls.length <= 24, '90s of idle reads must stay far below the old 45 reads, got ' + calls.length);
    const gaps = calls.slice(1).map((call, index) => call.at - calls[index].at);
    assert.ok(Math.max(...gaps) >= 9000, 'the idle cadence widens, largest gap ' + Math.max(...gaps));
    const late = calls.filter(call => call.at >= start + 50000);
    assert.ok(late.length >= 3 && late.length <= 6, 'the idle window still polls, got ' + late.length);
    assert.ok(late.slice(1).every((call, index) => call.at - late[index].at >= 9000),
      'idle reads are at least 9s apart');
    assert.equal(h.panel.poll().live, false);
    assert.notEqual(h.panel.poll().timer, null, 'the idle chain is still alive');
  });

  await test('queued or running work keeps the prompt live cadence', async () => {
    const h = createHarness();
    await h.boot();
    await h.advance(60000);                                   // let the document go idle first
    h.server.projects.get(PROJ_A).tasks.push(h.runningTask('t-run', '运行中的任务'));
    await h.advance(11000);
    assert.equal(h.panel.poll().live, true, 'work started outside the panel is noticed within the idle interval');
    const start = h.clock.now;
    await h.advance(12000);
    const live = h.stateCalls().filter(call => call.at >= start);
    assert.ok(live.length >= 4, 'running work must poll about every 2s, got ' + live.length + ' reads in 12s');
    assert.ok(h.el('task-list').innerHTML.includes('执行中'), 'running task stays visible in the list');
  });

  await test('a hidden document stops polling and a visible one resumes at once', async () => {
    const h = createHarness();
    await h.boot();
    h.document.hidden = true;
    h.document.dispatch({type: 'visibilitychange'});
    assert.equal(h.panel.poll().timer, null, 'hiding clears the auto timer');
    const hiddenFrom = h.stateCalls().length;
    await h.advance(60000);
    assert.equal(h.stateCalls().length, hiddenFrom, 'a hidden document performs no reads');
    assert.equal(h.clock.pending(), 0, 'no timer is left running while hidden');
    h.document.hidden = false;
    h.document.dispatch({type: 'visibilitychange'});
    await h.flush();
    assert.ok(h.stateCalls().length > hiddenFrom, 'showing the panel refreshes immediately');
    assert.notEqual(h.panel.poll().timer, null, 'the auto chain resumes after the panel is shown');
  });

  await test('slow responses never overlap and never fork a second chain', async () => {
    const h = createHarness();
    await h.boot();
    h.net.hold = true;
    const before = h.stateCalls().length;
    await h.advance(30000);                                    // many would-be ticks
    assert.equal(h.stateCalls().length - before, 1, 'only one request is in flight, whatever the delay');
    assert.equal(h.net.inFlight, 1);
    assert.equal(h.net.maxStateInFlight, 1, 'two state reads were never open at the same time');
    assert.equal(h.clock.pending(), 0, 'the waiting chain schedules nothing while it waits');
    h.net.hold = false;
    h.releaseHeld();
    await h.flush();
    assert.notEqual(h.panel.poll().timer, null, 'the same chain continues after the slow read');
    assert.equal(h.panel.poll().chain >= 1, true);
  });

  await test('a failed read shows the offline state and the chain recovers', async () => {
    const h = createHarness();
    await h.boot();
    h.server.failState = true;
    await h.advance(5000);
    assert.ok(h.el('sync').innerHTML.includes('连接已中断'), 'the panel reports the broken connection');
    assert.equal(h.el('sync').classList.contains('offline'), true);
    assert.equal(h.panel.poll().failure, true, 'a failed poll is remembered for the retry interval');
    const failedReads = h.stateCalls().length;
    await h.advance(5000);
    assert.ok(h.stateCalls().length > failedReads + 1, 'a failed poll retries promptly, not at the idle rate');
    h.server.failState = false;
    await h.advance(5000);
    assert.ok(h.el('sync').innerHTML.includes('实时同步'), 'the panel returns to live sync');
    assert.equal(h.el('sync').classList.contains('offline'), false);
    assert.equal(h.panel.poll().failure, false);
    assert.ok(h.el('task-list').innerHTML.includes('甲项目任务'), 'recovery restores the rendered tasks');
  });

  await test('unchanged data keeps the existing DOM nodes', async () => {
    const h = createHarness();
    await h.boot();
    const watched = ['models', 'task-list', 'task-detail', 'goals-tree', 'usage-windows', 'execution-activity'];
    const snapshot = () => watched.map(id => ({id, writes: h.el(id).writes, nodes: h.el(id).nodeIds.join(',')}));
    const before = snapshot();
    for (let round = 0; round < 3; round += 1) { await h.panel.refresh(); await h.flush(); }
    assert.deepEqual(snapshot(), before, 'three identical reads must not rewrite any watched container');
    assert.ok(h.el('task-detail').innerHTML.includes('独立验收通过'), 'the preserved detail still shows the acceptance');
  });

  await test('task, acceptance and stage changes still render', async () => {
    const h = createHarness();
    await h.boot();
    const list = h.el('task-list'), detail = h.el('task-detail'), goals = h.el('goals-tree');
    const writes = [list.writes, detail.writes, goals.writes];
    const task = h.server.projects.get(PROJ_A).tasks[0];
    task.status = 'recovery_pending'; task.phase = '待恢复'; task.review = null;
    task.ended_at = null; task.started_at = '2026-01-01T00:00:00Z';
    h.server.projects.get(PROJ_A).tree = h.treeOf('阶段一', 'accepted', '独立验收通过');
    h.server.projects.get(PROJ_A).tree.stats = {stages: 1, accepted: 1, recovery_pending: 1};
    await h.panel.refresh(); await h.flush();
    assert.ok(list.innerHTML.includes('待恢复'), 'the pending-recovery state is shown');
    assert.ok(goals.innerHTML.includes('独立验收通过'), 'the accepted stage is shown');
    task.status = 'completed'; task.phase = '执行完成';
    task.review = {state: 'final_accepted', label: '主脑接管完成 · 独立验收通过', updated_at: '2026-01-01T00:09:00Z',
                   rework_count: 1, max_auto_rework: 2, final: {accepted_at: '2026-01-01T00:09:00Z'}};
    task.ended_at = '2026-01-01T00:08:00Z';
    await h.panel.refresh(); await h.flush();
    assert.ok(list.innerHTML.includes('已完成'), 'the independently accepted task shows as completed');
    assert.ok(detail.innerHTML.includes('主脑接管完成 · 独立验收通过'), 'the acceptance wording is updated');
    assert.deepEqual([list.writes > writes[0], detail.writes > writes[1], goals.writes > writes[2]], [true, true, true],
      'changed sections do rewrite their DOM');
  });

  await test('project switch keeps its own refresh and ignores the old response', async () => {
    const h = createHarness();
    await h.boot();
    h.net.hold = true;
    await h.advance(3000);                                     // an old-project read is in flight
    assert.equal(h.net.held.length, 1, 'one old read is pending');
    h.net.hold = false;
    h.window.dispatch({type: 'modeldesk-project-context', detail: {mode: 'project', project: PROJ_B}});
    await h.flush();
    h.releaseHeld();                                           // the old answer arrives late
    await h.advance(4000);
    const views = h.stateCalls().map(rec => h.decodeView(h.header(rec, 'X-Model-Desk-View')).project);
    assert.ok(views.includes(PROJ_B), 'the new project is read, no refresh is lost: ' + views.join(' | '));
    assert.ok(h.el('task-list').innerHTML.includes('乙项目任务'), 'the new project records are rendered');
    assert.ok(!h.el('task-list').innerHTML.includes('甲项目任务'), 'the stale answer is never applied');
    assert.notEqual(h.panel.poll().timer, null, 'the chain continues for the new project');
    assert.equal(h.net.maxStateInFlight, 1, 'the switch never opens two state reads at once');
  });

  await test('user interaction brings an idle tick forward', async () => {
    const h = createHarness();
    await h.boot();
    await h.advance(60000);                                    // idle: 15s cadence
    const before = h.stateCalls().length;
    h.document.dispatch({type: 'pointerdown'});
    await h.advance(3000);
    assert.ok(h.stateCalls().length > before, 'working in the panel returns it to the prompt cadence');
  });

  await test('pagehide clears the auto timer', async () => {
    const h = createHarness();
    await h.boot();
    h.window.dispatch({type: 'pagehide'});
    assert.equal(h.panel.poll().timer, null, 'closing the page clears the chain');
    const before = h.stateCalls().length;
    await h.advance(60000);
    assert.equal(h.stateCalls().length, before, 'nothing polls after the page is closed');
    assert.equal(h.clock.pending(), 0, 'no timer survives the page close');
  });

  console.log(`\n${checks} checks passed, ${failures.length} failed`);
  if (failures.length) {
    failures.forEach(({name, error}) => console.error('\n' + name + '\n' + (error && error.stack)));
    process.exitCode = 1;
  }
})();
