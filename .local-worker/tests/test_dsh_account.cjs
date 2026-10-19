'use strict';

// Offline checks for the DSH host-account adapter. The fake platform panel and
// fake account controller never touch DSH, Platform, a real token or a model.

const assert = require('node:assert/strict');
const test = require('node:test');

const ACCOUNT = require('../../integrations/dsh-brain/account.cjs');

function response(status, data) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return data; },
    async text() { return JSON.stringify(data); },
  };
}

function makePanel() {
  const posts = [];
  const sessions = [];
  let token = 'panel-token-fixture';
  let rejectNext = false;
  async function fetchImpl(url, init = {}) {
    const target = String(url);
    if (target.endsWith('/api/session')) {
      sessions.push(init);
      return response(200, { token });
    }
    if (target.endsWith('/api/host-account/snapshot')) {
      if (rejectNext) {
        rejectNext = false;
        return response(403, { code: 'panel_session_expired' });
      }
      if (init.headers['X-Panel-Token'] !== token) return response(403, {});
      posts.push({
        body: JSON.parse(init.body),
        headers: { ...init.headers },
        redirect: init.redirect,
      });
      return response(200, { ok: true });
    }
    return response(404, {});
  }
  return {
    fetchImpl,
    posts,
    sessions,
    get token() { return token; },
    rejectOnce() { rejectNext = true; },
    rotate() { token = 'panel-token-fixture-rotated'; },
  };
}

function channel() {
  const queue = [];
  let wake = null;
  function kick() {
    if (!wake) return;
    const resolve = wake;
    wake = null;
    resolve();
  }
  return {
    push(value) {
      queue.push(value);
      kick();
    },
    async *iterate(signal) {
      const abort = () => kick();
      signal.addEventListener('abort', abort, { once: true });
      try {
        while (!signal.aborted) {
          if (queue.length) {
            yield queue.shift();
            continue;
          }
          await new Promise((resolve) => { wake = resolve; });
        }
      } finally {
        signal.removeEventListener('abort', abort);
      }
    },
  };
}

function makeController(options = {}) {
  const watch = channel();
  const expiry = channel();
  const balances = options.balances || [{ status: 'ready', value: [{ currency: 'CNY', balance: '12.34' }], bonusWallets: [] }];
  let balanceIndex = 0;
  const calls = { state: 0, balance: 0, metadata: [] };
  const controller = {
    async getState() {
      calls.state += 1;
      const state = options.state || { status: 'credential-stored' };
      return typeof state === 'function' ? state() : state;
    },
    async getBalance(metadata) {
      calls.balance += 1;
      calls.metadata.push(metadata);
      const value = balances[Math.min(balanceIndex, balances.length - 1)];
      balanceIndex += 1;
      return typeof value === 'function' ? value() : value;
    },
    watch(signal) {
      return watch.iterate(signal);
    },
    watchExpiry(signal) {
      return expiry.iterate(signal);
    },
  };
  return { controller, watch, expiry, calls };
}

function adapterFor(controller, panel, extra = {}) {
  return ACCOUNT.createAccountAdapter({
    controller,
    panelUrl: 'http://127.0.0.1:8765',
    fetchImpl: panel.fetchImpl,
    now: () => new Date('2026-10-18T15:30:00.000Z'),
    refreshIntervalMs: 0,
    minRefreshMs: 0,
    debounceMs: 0,
    locale: 'zh-CN',
    version: '0.2.1-rc.2',
    ...extra,
  });
}

async function waitFor(check, label, timeoutMs = 2000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail('timed out waiting for ' + label);
}

test('public metadata and wallet projection keep only safe fields', () => {
  const metadata = ACCOUNT.clientMetadata(new Date('2026-10-18T15:30:00Z'), '0.2.1-rc.2', 'zh_CN');
  assert.equal(metadata.version, '0.2.1-rc.2');
  assert.equal(metadata.locale, 'zh_CN');
  assert.equal(typeof metadata.timezoneOffsetSeconds, 'number');
  assert.deepEqual(Object.keys(metadata).sort(), ['locale', 'timezoneOffsetSeconds', 'version']);
  assert.match(ACCOUNT.resolveDshVersion(null), /^\d+\.\d+\.\d+/);

  const wallets = ACCOUNT.normalizeWallets({
    status: 'ready',
    value: [{ currency: 'CNY', balance: '12.34' }],
    bonusWallets: [{ currency: 'USD', balance: '1.00' }],
  });
  assert.deepEqual(wallets, [
    { kind: 'normal', currency: 'CNY', balance: '12.34' },
    { kind: 'bonus', currency: 'USD', balance: '1.00' },
  ]);
  assert.throws(() => ACCOUNT.normalizeWallets({
    status: 'ready', value: [{ currency: 'EUR', balance: '1' }], bonusWallets: [],
  }), /币种/);
  assert.throws(() => ACCOUNT.normalizeWallets({
    status: 'ready', value: [{ currency: 'CNY', balance: 'NaN' }], bonusWallets: [],
  }), /金额/);
  assert.throws(() => ACCOUNT.normalizeWallets({ status: 'failed', value: [] }), /余额/);
});

test('strict Cordis-style metadata properties cannot prevent account publication', async () => {
  const panel = makePanel();
  const { controller, calls } = makeController();
  const reads = [];
  const ctx = new Proxy({ get: () => controller }, {
    get(target, key, receiver) {
      if (Reflect.has(target, key)) return Reflect.get(target, key, receiver);
      reads.push(key);
      throw { get message() { assert.fail('raw context error must not be read'); } };
    },
  });
  const adapter = ACCOUNT.createAccountAdapter({ ctx,
    panelUrl: 'http://127.0.0.1:8765', fetchImpl: panel.fetchImpl, refreshIntervalMs: 0 });
  assert.equal(await adapter.start(), true);
  await adapter.whenIdle();
  assert.deepEqual(reads, ['version', 'app']);
  assert.equal(calls.balance, 1);
  assert.equal(panel.posts.at(-1).body.status, 'ready');
  assert.equal(adapter._state().resolutionPath, 'plugin');
  assert.match(calls.metadata[0].version, /^\d+\.\d+\.\d+/);
  await adapter.dispose();
});

test('ready balance is pushed through the existing panel session token only', async (t) => {
  const panel = makePanel();
  const { controller, calls } = makeController({
    balances: [{
      status: 'ready',
      value: [{ currency: 'CNY', balance: '12.34' }],
      bonusWallets: [{ currency: 'CNY', balance: '1.00' }],
    }],
  });
  const logs = [];
  const adapter = adapterFor(controller, panel, { log: (_level, message) => logs.push(message) });
  t.after(() => adapter.dispose());
  assert.equal(await adapter.start(), true);
  await adapter.whenIdle();
  await waitFor(() => panel.posts.some((post) => post.body.status === 'ready'), 'ready snapshot');

  const ready = panel.posts.find((post) => post.body.status === 'ready');
  assert.equal(ready.body.host, 'dsh');
  assert.equal(ready.body.source, 'dsh-account-controller');
  assert.equal(ready.body.source_api, 'account.getBalance');
  assert.equal(ready.body.wallets.length, 2);
  assert.deepEqual(Object.keys(ready.body).sort(), [
    'error', 'expires_at', 'fetched_at', 'host', 'schema', 'source', 'source_api', 'status', 'wallets',
  ]);
  assert.equal(ready.headers['X-Panel-Token'], panel.token);
  assert.equal(panel.sessions.length, 1, 'panel token is cached in memory');
  assert.equal(panel.sessions[0].redirect, 'error');
  assert.ok(panel.posts.every(post => post.redirect === 'error'));
  assert.equal(calls.balance, 1);
  assert.deepEqual(Object.keys(calls.metadata[0]).sort(), ['locale', 'timezoneOffsetSeconds', 'version']);
  const observed = JSON.stringify(panel.posts.map((post) => post.body));
  assert.doesNotMatch(observed, /panel-token-fixture/);
  assert.doesNotMatch(observed, /profile|avatar|contact|token/i);
  assert.doesNotMatch(logs.join(' '), /panel-token-fixture/);

  await adapter.dispose();
  const stopped = panel.posts.at(-1);
  assert.equal(stopped.body.status, 'unavailable');
  assert.deepEqual(stopped.body.wallets, []);
});

test('signed-out state never reads or displays a balance', async () => {
  const panel = makePanel();
  const { controller, calls } = makeController({ state: { status: 'signed-out' } });
  const adapter = adapterFor(controller, panel);
  await adapter.start();
  await adapter.whenIdle();
  await waitFor(() => panel.posts.some((post) => post.body.status === 'signed-out'), 'signed-out snapshot');
  assert.equal(calls.balance, 0);
  const signedOut = panel.posts.find((post) => post.body.status === 'signed-out');
  assert.deepEqual(signedOut.body.wallets, []);
  await adapter.dispose();
});

test('an account change clears the previous amount before the new read', async () => {
  const panel = makePanel();
  const { controller, watch } = makeController({
    balances: [
      { status: 'ready', value: [{ currency: 'CNY', balance: '12.34' }], bonusWallets: [] },
      { status: 'ready', value: [{ currency: 'CNY', balance: '88.00' }], bonusWallets: [] },
    ],
  });
  const adapter = adapterFor(controller, panel);
  await adapter.start();
  await adapter.whenIdle();
  await waitFor(() => panel.posts.some((post) => post.body.status === 'ready'
    && post.body.wallets[0].balance === '12.34'), 'first account');

  watch.push({ value: { status: 'credential-stored' } });
  await waitFor(() => panel.posts.some((post) => post.body.status === 'ready'
    && post.body.wallets[0].balance === '88.00'), 'second account');
  await adapter.whenIdle();

  const firstReady = panel.posts.findIndex((post) => post.body.status === 'ready'
    && post.body.wallets[0].balance === '12.34');
  const clear = panel.posts.findIndex((post, index) => index > firstReady
    && post.body.status === 'updating' && post.body.wallets.length === 0);
  const secondReady = panel.posts.findIndex((post) => post.body.status === 'ready'
    && post.body.wallets[0].balance === '88.00');
  assert.ok(clear > firstReady, 'old amount is cleared after the account change');
  assert.ok(secondReady > clear, 'new amount arrives after the clearing snapshot');
  assert.equal(panel.posts.slice(clear + 1).some((post) => post.body.status === 'ready'
    && post.body.wallets[0] && post.body.wallets[0].balance === '12.34'), false);
  await adapter.dispose();
});

test('a failed new read clears the old amount instead of showing it as current', async () => {
  const panel = makePanel();
  const { controller, watch } = makeController({
    balances: [
      { status: 'ready', value: [{ currency: 'CNY', balance: '12.34' }], bonusWallets: [] },
      { status: 'failed' },
    ],
  });
  const adapter = adapterFor(controller, panel);
  await adapter.start();
  await adapter.whenIdle();
  await waitFor(() => panel.posts.some((post) => post.body.status === 'ready'), 'first ready');
  watch.push({ value: { status: 'credential-stored' } });
  await waitFor(() => panel.posts.some((post) => post.body.status === 'error'), 'error snapshot');
  await adapter.whenIdle();
  const error = panel.posts.find((post) => post.body.status === 'error');
  assert.deepEqual(error.body.wallets, []);
  await adapter.dispose();
});

test('credential expiry clears the amount and is marked expired', async () => {
  const panel = makePanel();
  const { controller, expiry } = makeController({
    balances: [{ status: 'ready', value: [{ currency: 'CNY', balance: '12.34' }], bonusWallets: [] }],
  });
  const adapter = adapterFor(controller, panel);
  await adapter.start();
  await adapter.whenIdle();
  await waitFor(() => panel.posts.some((post) => post.body.status === 'ready'), 'ready');
  expiry.push('session-expired');
  await waitFor(() => panel.posts.some((post) => post.body.status === 'expired'), 'expired snapshot');
  const expired = panel.posts.find((post) => post.body.status === 'expired');
  assert.deepEqual(expired.body.wallets, []);
  assert.match(expired.body.error, /登录凭据已失效/);
  await adapter.dispose();
});

test('a rotated panel session token is retried once without exposing it', async () => {
  const panel = makePanel();
  panel.rejectOnce();
  const { controller } = makeController();
  const adapter = adapterFor(controller, panel);
  await adapter.start();
  await adapter.whenIdle();
  await waitFor(() => panel.posts.some((post) => post.body.status === 'ready'), 'ready after retry');
  assert.equal(panel.sessions.length, 2);
  assert.deepEqual(panel.posts.map((post) => post.body.status), ['updating', 'ready']);
  await adapter.dispose();
});

test('missing official account controller publishes unavailable without reading an account', async () => {
  const panel = makePanel();
  const adapter = ACCOUNT.createAccountAdapter({
    ctx: { get: (name) => { assert.equal(name, 'accountController'); return null; } },
    panelUrl: 'http://127.0.0.1:8765',
    fetchImpl: panel.fetchImpl,
    refreshIntervalMs: 0,
  });
  assert.equal(await adapter.start(), false);
  assert.deepEqual(panel.posts.map(post => post.body.status), ['unavailable']);
  assert.deepEqual(panel.posts[0].body.wallets, []);
  assert.match(panel.posts[0].body.error, /等待服务注册/);
  await adapter.dispose();
});

test('SDK diagnostics and non-finite amounts never reach the page or logs', async () => {
  const panel = makePanel();
  const logs = [];
  const { controller } = makeController({ balances: [() => {
    throw new Error('Authorization: fixture-sensitive-value');
  }] });
  const adapter = adapterFor(controller, panel, { log: (...args) => logs.push(args) });
  await adapter.start();
  await adapter.whenIdle();
  assert.ok(panel.posts.some(post => post.body.status === 'error'));
  assert.doesNotMatch(JSON.stringify(panel.posts.map(post => post.body)) + JSON.stringify(logs),
    /fixture-sensitive-value|Authorization/);
  const opaque = { get message() { throw new Error('must not inspect SDK diagnostics'); } };
  assert.doesNotThrow(() => ACCOUNT.safeText(opaque));
  assert.throws(() => ACCOUNT.normalizeWallets({ status: 'ready',
    value: [{ currency: 'CNY', balance: '1e9999' }] }), /金额/);
  await adapter.dispose();
});

test('late old-account response is discarded after logout', async () => {
  const panel = makePanel();
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  const { controller, watch, calls } = makeController({ balances: [() => pending] });
  const adapter = adapterFor(controller, panel);
  await adapter.start();
  await waitFor(() => calls.balance === 1, 'old account read');
  watch.push({ value: { status: 'signed-out' } });
  await waitFor(() => panel.posts.some(post => post.body.status === 'signed-out'), 'logout');
  finish({ status: 'ready', value: [{ currency: 'CNY', balance: '99.00' }] });
  await adapter.whenIdle();
  assert.equal(panel.posts.some(post => post.body.status === 'ready'), false);
  assert.equal(panel.posts.at(-1).body.status, 'signed-out');
  await adapter.dispose();
});

test('expired credentials are not revived by the interval refresh', async () => {
  const panel = makePanel();
  const { controller, expiry, calls } = makeController();
  const adapter = adapterFor(controller, panel);
  await adapter.start();
  await adapter.whenIdle();
  expiry.push('session-expired');
  await waitFor(() => panel.posts.at(-1)?.body.status === 'expired', 'expiry');
  const count = calls.balance;
  await adapter.refresh({ force: true });
  assert.equal(calls.balance, count);
  assert.equal(panel.posts.at(-1).body.status, 'expired');
  await adapter.dispose();
});

test('a late-loading official controller is picked up without account polling', async () => {
  const panel = makePanel();
  const { controller } = makeController();
  let available = null;
  let retry;
  const adapter = adapterFor(undefined, panel, {
    controller: undefined,
    ctx: { get: () => available },
    refreshIntervalMs: 120000,
    timers: { setTimeout: fn => { retry = fn; return 1; }, clearTimeout() {},
      setInterval: () => 2, clearInterval() {} },
  });
  assert.equal(await adapter.start(), false);
  assert.deepEqual(panel.posts.map(post => post.body.status), ['unavailable']);
  assert.equal(typeof retry, 'function');
  available = controller;
  retry();
  await waitFor(() => panel.posts.some(post => post.body.status === 'ready'), 'late controller');
  await adapter.dispose();
});

test('service resolution respects plugin, root then live top-level Agent registries', () => {
  const plugin = makeController().controller;
  const root = makeController().controller;
  const agent = makeController().controller;
  const ctx = { get: () => plugin, root: { get: () => root },
    agents: { roots: () => [{ ctx: { get: () => agent } }] } };
  assert.deepEqual(ACCOUNT.resolveAccountController(ctx), { controller: plugin, resolutionPath: 'plugin' });
  ctx.get = () => ({ getBalance() {} }); // Incomplete service must fall through.
  assert.deepEqual(ACCOUNT.resolveAccountController(ctx), { controller: root, resolutionPath: 'root' });
  ctx.root.get = () => { throw new Error('fixture-private-context-data'); };
  assert.deepEqual(ACCOUNT.resolveAccountController(ctx), { controller: agent, resolutionPath: 'agent' });
  ctx.agents.roots = () => [];
  assert.equal(ACCOUNT.resolveAccountController(ctx), null);
});

test('broken context accessors are isolated and never logged', async () => {
  const logs = [];
  const panel = makePanel();
  const { controller } = makeController();
  const ctx = { get() { throw { get message() { assert.fail('read raw error'); } }; },
    get root() { throw new Error('fixture-secret'); },
    agents: { roots: () => [{ ctx: { get: () => controller } }] } };
  const adapter = adapterFor(undefined, panel, { ctx, log: (...args) => logs.push(args) });
  assert.equal(await adapter.start(), true);
  await adapter.whenIdle();
  assert.equal(adapter._state().resolutionPath, 'agent');
  assert.ok(logs.some(row => row[1].includes('已解析：agent')));
  assert.ok(logs.some(row => row[1].includes('发布通道已确认')));
  assert.doesNotMatch(JSON.stringify(logs), /fixture-secret/);
  assert.equal(panel.posts.at(-1).body.status, 'ready');
  await adapter.dispose();
});

test('an injected resolver is retried, deduplicated and reports only a fixed path label', async () => {
  const panel = makePanel();
  const { controller } = makeController();
  let count = 0;
  let release;
  let first = true;
  const blocked = new Promise(resolve => { release = resolve; });
  const logs = [];
  const adapter = adapterFor(undefined, panel, {
    resolveController: () => {
      count += 1;
      return count === 1 ? null : { controller, resolutionPath: 'fixture-sensitive-data' };
    },
    publisher: { post: async snapshot => {
      if (first) { first = false; await blocked; }
      panel.posts.push({ body: snapshot });
    } },
    log: (...args) => logs.push(args),
  });
  const firstStart = adapter.start();
  const sameStart = adapter.start();
  assert.equal(firstStart, sameStart);
  release();
  assert.equal(await firstStart, false);
  assert.equal(count, 1);
  assert.equal(await adapter.start(), true);
  await adapter.whenIdle();
  assert.equal(count, 2);
  assert.equal(panel.posts.at(-1).body.status, 'ready');
  assert.equal(adapter._state().resolutionPath, 'resolver');
  assert.doesNotMatch(JSON.stringify(logs), /fixture-sensitive-data/);
  await adapter.dispose();
});

test('disposal while waiting for a service cannot schedule a late discovery retry', async () => {
  let release;
  let first = true;
  let timers = 0;
  const blocked = new Promise(resolve => { release = resolve; });
  const adapter = ACCOUNT.createAccountAdapter({
    resolveController: () => null,
    publisher: { post: () => {
      if (first) { first = false; return blocked; }
      return Promise.resolve();
    } },
    timers: { setTimeout: () => { timers += 1; }, clearTimeout() {} },
  });
  const starting = adapter.start();
  await adapter.dispose();
  release();
  assert.equal(await starting, false);
  assert.equal(timers, 0);
});

test('the production loopback transport never follows panel redirects', async (t) => {
  const http = require('node:http');
  const requests = [];
  const server = http.createServer((req, res) => {
    requests.push(req.url);
    req.resume();
    if (req.url === '/api/session') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ token: 'local-transport-fixture' }));
    } else {
      res.writeHead(302, { Location: '/must-not-follow' });
      res.end();
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const { controller } = makeController();
  const adapter = adapterFor(controller, {}, { fetchImpl: undefined,
    panelUrl: `http://127.0.0.1:${server.address().port}` });
  await adapter.start();
  await adapter.whenIdle();
  await adapter.dispose();
  assert.ok(requests.includes('/api/host-account/snapshot'));
  assert.equal(requests.includes('/must-not-follow'), false);
});

test('disposal during the first publication cannot create a late refresh timer', async () => {
  let finish;
  let first = true;
  let intervals = 0;
  const blocked = new Promise(resolve => { finish = resolve; });
  const { controller } = makeController();
  const adapter = adapterFor(controller, {}, {
    refreshIntervalMs: 120000,
    publisher: { post: () => { if (first) { first = false; return blocked; }
      return Promise.resolve(true); } },
    timers: { setInterval: () => { intervals += 1; return 1; }, clearInterval() {} },
  });
  const starting = adapter.start();
  await adapter.dispose();
  finish(true);
  assert.equal(await starting, false);
  assert.equal(intervals, 0);
});
