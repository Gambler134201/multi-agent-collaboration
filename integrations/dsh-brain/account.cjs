'use strict';

/**
 * Optional DSH host-account balance bridge for the Model Desk workbench.
 *
 * The adapter reads only the official, host-side account controller service
 * (`accountController`) documented by
 * `@deepseek-ai/dsh-api-account-controller` 0.2.1-rc.2:
 *
 *   getState()                 -> signed-out / credential-stored
 *   getBalance(metadata)       -> ready wallets / failed / null when signed out
 *   watch(signal)              -> initial state and later account changes
 *   watchExpiry(signal)        -> live credential-expiry notification
 *
 * Every Platform-backed call receives AccountClientMetadata (client version,
 * locale, east-positive UTC offset).  The adapter never asks for a token,
 * PKCE value, profile, avatar, contact data or API key, and it never reads
 * DSH credential files.  A missing controller is reported as unavailable; the
 * workbench must not fall back to the configured API-key account or to the
 * Codex/GPT quota.
 *
 * The public snapshot is pushed to the already-running loopback workbench over
 * its existing panel-session token route.  That token is held in memory only,
 * never written to a URL, page, log or tool result.  No new server, CORS rule,
 * CSRF exception or model write permission is introduced.
 */

const path = require('node:path');
const http = require('node:http');
const { createRequire } = require('node:module');

const SNAPSHOT_SCHEMA = 1;
const SNAPSHOT_HOST = 'dsh';
const SNAPSHOT_SOURCE = 'dsh-account-controller';
const SNAPSHOT_SOURCE_API = 'account.getBalance';
const FALLBACK_DSH_VERSION = '0.2.1-rc.2';
const DEFAULT_REFRESH_MS = 120_000;
const MIN_REFRESH_MS = 15_000;
const DEFAULT_TTL_MS = 300_000;
const DEFAULT_DEBOUNCE_MS = 500;
const PUSH_TIMEOUT_MS = 2_500;
const PUBLISH_ERROR_LOG_MS = 300_000;
const MAX_WALLETS = 32;
const ERROR_LIMIT = 300;
const DECIMAL = /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
const CURRENCIES = new Set(['CNY', 'USD']);
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);

const SAFE_ERRORS = new Set([
  'DSH 官方余额接口返回失败；未显示旧余额。',
  'DSH 官方余额接口返回了未知状态；未显示旧余额。',
  'DSH 登录凭据已失效，请重新登录；未显示旧余额。',
  '适配器已停用；未显示旧余额。',
  'DSH 官方账户服务尚未就绪，正在等待服务注册；未显示旧余额。',
]);
function safeText(error) {
  // SDK/network exceptions can embed headers, URLs or credentials. Never
  // inspect/stringify them or forward their message to logs and snapshots.
  if (error === undefined || error === null || error === '') return '';
  return typeof error === 'string' && SAFE_ERRORS.has(error)
    ? error : 'DSH 账户接口暂不可用（原始诊断已省略）。';
}

function resolveDshVersion(ctx) {
  const candidates = [];
  // A Cordis plugin context is a strict service proxy, not a plain object.
  // Accessing an undeclared service throws even behind optional chaining.
  // Version hints are optional metadata; their absence must never prevent the
  // account adapter from starting. Never log raw context exceptions.
  for (const read of [() => ctx && ctx.version, () => ctx && ctx.app && ctx.app.version]) {
    try {
      const value = read();
      if (typeof value === 'string') candidates.push(value);
    } catch { /* Not a provided property in the installed Cordis context. */ }
  }
  try {
    const required = createRequire(path.join(__dirname, 'package.json'))('@deepseek-ai/dsh/package.json');
    if (required && typeof required.version === 'string') candidates.push(required.version);
  } catch {
    // The profile may not expose the DSH package to this plugin's resolver.
  }
  return candidates.find((value) => /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(value))
    || FALLBACK_DSH_VERSION;
}

function isAccountController(value) {
  try {
    return !!value && ['getState', 'getBalance', 'watch']
      .every(name => typeof value[name] === 'function');
  } catch {
    return false;
  }
}

function resolveAccountController(ctx) {
  // Only public service registries on the plugin, root and current top-level
  // Agents. Never inspect a credential provider or recursively scan contexts.
  const seen = new Set();
  function from(scope, resolutionPath) {
    if (!scope || seen.has(scope)) return null;
    seen.add(scope);
    try {
      const controller = typeof scope.get === 'function' ? scope.get('accountController') : null;
      return isAccountController(controller) ? { controller, resolutionPath } : null;
    } catch { return null; }
  }
  let resolved = from(ctx, 'plugin');
  if (resolved) return resolved;
  try { resolved = from(ctx && ctx.root, 'root'); } catch { /* unavailable scope */ }
  if (resolved) return resolved;
  try {
    const roots = ctx && ctx.agents && ctx.agents.roots();
    if (Array.isArray(roots)) {
      for (const agent of roots) {
        resolved = from(agent && agent.ctx, 'agent');
        if (resolved) return resolved;
      }
    }
  } catch { /* Host roots may not be ready yet. */ }
  return null;
}

function normalizeLocale(value) {
  if (typeof value !== 'string' || !value.trim()) return 'zh-CN';
  return value.trim().slice(0, 32);
}

function clientMetadata(date, version, locale) {
  const when = date instanceof Date ? date : new Date(date);
  return {
    version,
    locale: normalizeLocale(locale),
    // DSH's Platform contract uses an east-positive UTC offset in seconds.
    timezoneOffsetSeconds: -when.getTimezoneOffset() * 60,
  };
}

function normalizeWallets(result) {
  if (!result || result.status !== 'ready' || !Array.isArray(result.value)) {
    throw new Error('DSH 余额接口没有返回可核对的余额钱包');
  }
  const groups = [
    ['normal', result.value],
    ['bonus', Array.isArray(result.bonusWallets) ? result.bonusWallets : []],
  ];
  const wallets = [];
  for (const [kind, rows] of groups) {
    for (const row of rows) {
      if (!row || typeof row !== 'object') {
        throw new Error('DSH 余额接口返回的钱包格式不正确');
      }
      if (!CURRENCIES.has(row.currency)) {
        throw new Error('DSH 余额接口返回了不支持的币种');
      }
      if (typeof row.balance !== 'string' || row.balance.length > 128
          || !DECIMAL.test(row.balance) || !Number.isFinite(Number(row.balance))) {
        throw new Error('DSH 余额接口返回的金额不是十进制字符串');
      }
      wallets.push({ kind, currency: row.currency, balance: row.balance });
      if (wallets.length > MAX_WALLETS) {
        throw new Error('DSH 余额接口返回的钱包数量异常');
      }
    }
  }
  return wallets;
}

function makeSnapshot(status, options = {}) {
  const at = options.at instanceof Date ? options.at : new Date(options.at || Date.now());
  const ttlMs = Number.isFinite(options.ttlMs) ? options.ttlMs : DEFAULT_TTL_MS;
  const wallets = status === 'ready' && Array.isArray(options.wallets) ? options.wallets : [];
  return {
    schema: SNAPSHOT_SCHEMA,
    host: SNAPSHOT_HOST,
    status,
    source: SNAPSHOT_SOURCE,
    source_api: SNAPSHOT_SOURCE_API,
    fetched_at: at.toISOString(),
    expires_at: new Date(at.getTime() + ttlMs).toISOString(),
    wallets,
    error: status === 'ready' ? '' : safeText(options.error),
  };
}

function normalizePanelOrigin(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:') throw new Error('panel origin must be loopback HTTP');
  if (url.username || url.password) throw new Error('panel origin must not carry credentials');
  const host = url.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (!LOOPBACK_HOSTS.has(host)) throw new Error('panel origin must be loopback');
  if ((url.pathname && url.pathname !== '/') || url.search || url.hash) {
    throw new Error('panel origin must not carry a path, query or fragment');
  }
  // The workbench binds IPv4 loopback; avoid an IPv6 localhost resolution
  // mismatch that would otherwise look like an unavailable adapter.
  const connectHost = host === 'localhost' ? '127.0.0.1' : (host === '::1' ? '[::1]' : host);
  const port = url.port ? `:${url.port}` : '';
  return `http://${connectHost}${port}`;
}

function timeoutSignal(ms) {
  if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
    return AbortSignal.timeout(ms);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  if (timer && typeof timer.unref === 'function') timer.unref();
  return controller.signal;
}

function loopbackFetch(address, options = {}) {
  const url = new URL(address);
  normalizePanelOrigin(url.origin);
  // Direct node:http deliberately avoids global fetch dispatchers/proxies and
  // never follows redirects carrying the private panel-session header.
  return new Promise((resolve, reject) => {
    const request = http.request(url, { method: options.method || 'GET',
      headers: options.headers, signal: options.signal }, response => {
      const chunks = [];
      let size = 0;
      response.on('data', chunk => {
        size += chunk.length;
        if (size > 16384) response.destroy(new Error('local response too large'));
        else chunks.push(chunk);
      });
      response.on('error', reject);
      response.on('end', () => resolve({
        status: response.statusCode,
        ok: response.statusCode >= 200 && response.statusCode < 300,
        async json() { return JSON.parse(Buffer.concat(chunks).toString('utf8')); },
      }));
    });
    request.on('error', reject);
    request.end(options.body);
  });
}

function createPanelPublisher(options) {
  const origin = normalizePanelOrigin(options.panelUrl);
  const fetchImpl = options.fetchImpl;
  const timeoutMs = Number.isFinite(options.timeoutMs) ? options.timeoutMs : PUSH_TIMEOUT_MS;
  let token = '';
  let posts = Promise.resolve();

  async function sessionToken() {
    if (token) return token;
    const response = await fetchImpl(`${origin}/api/session`, {
      cache: 'no-store',
      redirect: 'error',
      signal: timeoutSignal(timeoutMs),
    });
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok || !data || typeof data.token !== 'string' || !data.token) {
      throw new Error(`workbench session unavailable (HTTP ${Number(response.status) || 0})`);
    }
    token = data.token;
    return token;
  }

  async function post(snapshot) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const credential = await sessionToken();
      const response = await fetchImpl(`${origin}/api/host-account/snapshot`, {
        method: 'POST',
        redirect: 'error',
        headers: {
          'Content-Type': 'application/json',
          'X-Panel-Token': credential,
        },
        body: JSON.stringify(snapshot),
        signal: timeoutSignal(timeoutMs),
      });
      if (response.status === 403) {
        // The panel restarted or rotated its in-memory token.  Retry once with
        // a fresh session token; the token itself is never logged or returned.
        token = '';
        continue;
      }
      if (!response.ok) {
        throw new Error(`workbench snapshot rejected (HTTP ${Number(response.status) || 0})`);
      }
      return true;
    }
    throw new Error('workbench session rejected after one token refresh');
  }

  return {
    post(snapshot) {
      // Account-change clearing and old reads must retain invocation order.
      const pending = posts.then(() => post(snapshot));
      posts = pending.catch(() => {});
      return pending;
    },
    reset() {
      token = '';
    },
  };
}

function createAccountAdapter(options = {}) {
  const ctx = options.ctx;
  const panelUrl = options.panelUrl;
  const log = typeof options.log === 'function' ? options.log : () => {};
  const fetchImpl = options.fetchImpl || loopbackFetch;
  const now = typeof options.now === 'function' ? options.now : () => new Date();
  const refreshIntervalMs = Number.isFinite(options.refreshIntervalMs)
    ? options.refreshIntervalMs : DEFAULT_REFRESH_MS;
  const minRefreshMs = Number.isFinite(options.minRefreshMs)
    ? options.minRefreshMs : MIN_REFRESH_MS;
  const ttlMs = Number.isFinite(options.ttlMs) ? options.ttlMs : DEFAULT_TTL_MS;
  const debounceMs = Number.isFinite(options.debounceMs)
    ? options.debounceMs : DEFAULT_DEBOUNCE_MS;
  const timers = options.timers || {};
  const setTimeoutFn = timers.setTimeout || setTimeout;
  const clearTimeoutFn = timers.clearTimeout || clearTimeout;
  const setIntervalFn = timers.setInterval || setInterval;
  const clearIntervalFn = timers.clearInterval || clearInterval;
  const version = options.version || resolveDshVersion(ctx);
  const locale = options.locale;
  const suppliedController = options.controller;
  const controllerResolver = typeof options.resolveController === 'function'
    ? options.resolveController : () => resolveAccountController(ctx);
  let controller = null;
  let publisher = options.publisher || null;
  let started = false;
  let disposed = false;
  let generation = 0;
  let abort = null;
  let watchTask = null;
  let expiryTask = null;
  let intervalTimer = null;
  let debounceTimer = null;
  let inFlight = null;
  let queuedRefresh = null;
  let lastFetchAt = 0;
  let lastPublishErrorAt = 0;
  let credentialExpired = false;
  let lastSnapshotAt = 0;
  let warnedUnavailable = false;
  let resolutionPath = null;
  let startPromise = null;
  let publicationConfirmed = false;
  let stateReported = false;

  function currentDate() {
    const value = now();
    return value instanceof Date ? value : new Date(value);
  }

  function snapshot(status, data = {}) {
    const at = Math.max(currentMs(), lastSnapshotAt + 1);
    lastSnapshotAt = at;
    return makeSnapshot(status, { ...data, at: new Date(at), ttlMs });
  }

  function currentMs() {
    return currentDate().getTime();
  }

  function resolveController() {
    try {
      const found = suppliedController
        ? { controller: suppliedController, resolutionPath: 'supplied' } : controllerResolver();
      const candidate = found && found.controller;
      if (!isAccountController(candidate)) return null;
      resolutionPath = ['plugin', 'root', 'agent', 'supplied'].includes(found.resolutionPath)
        ? found.resolutionPath : 'resolver';
      return candidate;
    } catch {
      return null;
    }
  }

  function metadata() {
    return clientMetadata(currentDate(), version, locale);
  }

  function publish(snapshot) {
    if (!publisher) return Promise.resolve(false);
    return publisher.post(snapshot).then(() => {
      if (!publicationConfirmed) {
        publicationConfirmed = true;
        log('info', 'DSH 账户快照发布通道已确认。');
      }
      return true;
    }).catch((error) => {
      const at = currentMs();
      if (at - lastPublishErrorAt > PUBLISH_ERROR_LOG_MS) {
        lastPublishErrorAt = at;
        log('warn', '工作台账户快照暂不可达；DSH 主脑与任务不受影响。' + safeText(error));
      }
      return false;
    });
  }

  function scheduleRefresh(delay, refreshOptions = {}) {
    if (disposed) return;
    if (debounceTimer) clearTimeoutFn(debounceTimer);
    debounceTimer = setTimeoutFn(() => {
      debounceTimer = null;
      refresh(refreshOptions).catch((error) => {
        log('warn', 'DSH 账户余额刷新失败：' + safeText(error));
      });
    }, Math.max(0, delay));
    if (debounceTimer && typeof debounceTimer.unref === 'function') debounceTimer.unref();
  }

  function refresh(refreshOptions = {}) {
    if (disposed) return Promise.resolve(false);
    if (inFlight) {
      // An account change may arrive while an older balance read is running.
      // Keep the newest forced request so its result is never silently lost.
      queuedRefresh = {
        ...queuedRefresh,
        ...refreshOptions,
        force: Boolean(refreshOptions.force || (queuedRefresh && queuedRefresh.force)),
      };
      return inFlight.then(() => false);
    }
    const force = refreshOptions.force === true;
    const runGeneration = refreshOptions.generation === undefined
      ? generation : refreshOptions.generation;
    const run = async () => {
      if (credentialExpired) return false;
      if (!force && currentMs() - lastFetchAt < minRefreshMs) return false;
      if (refreshOptions.clear) {
        await publish(snapshot('updating'));
      }
      let state;
      try {
        state = await controller.getState();
      } catch (error) {
        if (!disposed && runGeneration === generation) {
          await publish(snapshot('error', {
            error: 'DSH 账户状态读取失败；未显示旧余额。' + safeText(error),
          }));
        }
        return false;
      }
      if (disposed || runGeneration !== generation) return false;
      if (!stateReported) {
        stateReported = true;
        const label = state && ['credential-stored', 'signed-out'].includes(state.status)
          ? state.status : 'unknown';
        log('info', `DSH 账户公开状态：${label}。`);
      }
      if (!state || state.status !== 'credential-stored') {
        await publish(snapshot('signed-out'));
        return true;
      }
      let result;
      try {
        result = await controller.getBalance(metadata());
      } catch (error) {
        if (!disposed && runGeneration === generation) {
          await publish(snapshot('error', {
            error: 'DSH 官方余额读取失败；未显示旧余额。' + safeText(error),
          }));
        }
        return false;
      }
      if (disposed || runGeneration !== generation) return false;
      if (result === null) {
        await publish(snapshot('signed-out'));
        return true;
      }
      if (!result || result.status === 'failed') {
        await publish(snapshot('error', {
          error: 'DSH 官方余额接口返回失败；未显示旧余额。',
        }));
        return false;
      }
      if (result.status !== 'ready') {
        await publish(snapshot('error', {
          error: 'DSH 官方余额接口返回了未知状态；未显示旧余额。',
        }));
        return false;
      }
      let wallets;
      try {
        wallets = normalizeWallets(result);
      } catch (error) {
        await publish(snapshot('error', {
          error: safeText(error),
        }));
        return false;
      }
      await publish(snapshot('ready', { wallets }));
      lastFetchAt = currentMs();
      return true;
    };
    inFlight = run().finally(() => {
      inFlight = null;
      if (queuedRefresh && !disposed) {
        const next = queuedRefresh;
        queuedRefresh = null;
        refresh(next).catch((error) => {
          log('warn', 'DSH 账户余额刷新失败：' + safeText(error));
        });
      }
    });
    return inFlight;
  }

  async function whenIdle() {
    while (inFlight) {
      const current = inFlight;
      try {
        await current;
      } catch {
        // Individual refresh errors are already converted into a safe snapshot.
      }
    }
  }

  async function watchState() {
    try {
      for await (const frame of controller.watch(abort.signal)) {
        if (disposed) break;
        const value = frame && typeof frame === 'object' && Object.hasOwn(frame, 'value')
          ? frame.value : frame;
        const status = value && value.status;
        generation += 1;
        if (status === 'signed-out') {
          credentialExpired = false;
          await publish(snapshot('signed-out'));
        } else if (status === 'credential-stored') {
          credentialExpired = false;
          // Every credential-state change clears the previous account's
          // amounts before the new read; an in-flight old read is discarded by
          // the generation check in refresh().
          await publish(snapshot('updating'));
          scheduleRefresh(debounceMs, { force: true, generation });
        }
      }
    } catch (error) {
      if (!disposed) log('warn', 'DSH 账户状态流中断：' + safeText(error));
    }
  }

  async function watchExpiry() {
    if (typeof controller.watchExpiry !== 'function') return;
    try {
      for await (const event of controller.watchExpiry(abort.signal)) {
        if (disposed) break;
        if (event === 'session-expired') {
          credentialExpired = true;
          generation += 1;
          await publish(snapshot('expired', {
            error: 'DSH 登录凭据已失效，请重新登录；未显示旧余额。',
          }));
        }
      }
    } catch (error) {
      if (!disposed) log('warn', 'DSH 登录失效通知流中断：' + safeText(error));
    }
  }

  async function startOnce() {
    if (started || disposed) return false;
    if (!publisher) {
      if (!panelUrl || typeof fetchImpl !== 'function') {
        log('warn', '没有可用的本机工作台账户通道；工作台余额保持未知。');
        return false;
      }
      try {
        publisher = createPanelPublisher({ panelUrl, fetchImpl });
      } catch (error) {
        log('warn', '工作台账户通道地址无效；余额保持未知。' + safeText(error));
        return false;
      }
    }
    if (debounceTimer) {
      clearTimeoutFn(debounceTimer);
      debounceTimer = null;
    }
    controller = resolveController();
    if (!controller) {
      if (!warnedUnavailable) {
        warnedUnavailable = true;
        log('warn', 'DSH 官方账户控制器未找到（plugin/root/agent）；等待服务注册。');
      }
      // Publish an honest non-financial status even before service discovery;
      // otherwise an old diagnostic/amount can look like a live adapter result.
      await publish(snapshot('unavailable', {
        error: 'DSH 官方账户服务尚未就绪，正在等待服务注册；未显示旧余额。',
      }));
      if (refreshIntervalMs > 0 && !disposed) {
        debounceTimer = setTimeoutFn(() => {
          debounceTimer = null;
          start().catch(() => log('warn', 'DSH 账户适配器暂不可用。'));
        }, MIN_REFRESH_MS);
        if (debounceTimer && typeof debounceTimer.unref === 'function') debounceTimer.unref();
      }
      return false;
    }
    log('info', `DSH 官方账户控制器已解析：${resolutionPath}。`);
    started = true;
    abort = new AbortController();
    watchTask = watchState();
    if (typeof controller.watchExpiry === 'function') expiryTask = watchExpiry();
    await publish(snapshot('updating'));
    if (disposed) return false;
    refresh({ force: true, generation });
    if (refreshIntervalMs > 0) {
      intervalTimer = setIntervalFn(() => {
        refresh({ force: true });
      }, refreshIntervalMs);
      if (intervalTimer && typeof intervalTimer.unref === 'function') intervalTimer.unref();
    }
    return true;
  }

  function start() {
    if (!startPromise) {
      startPromise = startOnce().finally(() => { startPromise = null; });
    }
    return startPromise;
  }

  async function dispose() {
    if (disposed) return;
    disposed = true;
    generation += 1;
    if (debounceTimer) {
      clearTimeoutFn(debounceTimer);
      debounceTimer = null;
    }
    if (intervalTimer) {
      clearIntervalFn(intervalTimer);
      intervalTimer = null;
    }
    if (abort) {
      abort.abort();
      abort = null;
    }
    const tasks = [watchTask, expiryTask].filter(Boolean);
    if (tasks.length) {
      await Promise.race([
        Promise.allSettled(tasks),
        new Promise((resolve) => {
          const timer = setTimeoutFn(resolve, 1_000);
          if (timer && typeof timer.unref === 'function') timer.unref();
        }),
      ]);
    }
    if (publisher) {
      try {
        await publisher.post(snapshot('unavailable', { error: '适配器已停用；未显示旧余额。',
        }));
      } catch {
        // The panel may already be gone; its freshness limit will mark the
        // previous snapshot stale, and it never renders it as current.
      }
    }
    started = false;
  }

  return {
    start,
    dispose,
    refresh,
    whenIdle,
    // Test-only visibility; no caller uses this to read credentials.
    _state() {
      return {
        started,
        disposed,
        generation,
        controllerAvailable: !!controller,
        publisherAvailable: !!publisher,
        resolutionPath,
        publicationConfirmed,
      };
    },
  };
}

module.exports = {
  createAccountAdapter,
  createPanelPublisher,
  clientMetadata,
  normalizeWallets,
  makeSnapshot,
  normalizePanelOrigin,
  resolveDshVersion,
  resolveAccountController,
  safeText,
  _test: Object.freeze({
    SNAPSHOT_SCHEMA,
    SNAPSHOT_HOST,
    SNAPSHOT_SOURCE,
    SNAPSHOT_SOURCE_API,
    FALLBACK_DSH_VERSION,
    DEFAULT_REFRESH_MS,
    MIN_REFRESH_MS,
    DEFAULT_TTL_MS,
    PUBLISH_ERROR_LOG_MS,
    DECIMAL,
  }),
};
