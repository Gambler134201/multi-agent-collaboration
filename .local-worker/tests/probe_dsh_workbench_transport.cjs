'use strict';

// Run with DSH's Electron in RUN_AS_NODE mode and an explicit app.asar path.
// Reads only installed public JS. No profiles, credentials, sockets or models.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {pathToFileURL} = require('node:url');
const assert = require('node:assert/strict');
const {webcrypto} = require('node:crypto');
function loadPlugin(bundle) {
  const child = require('node:child_process');
  const original = child.execFile;
  child.execFile = (executable, args, options, callback) => {
    assert.equal(executable, process.execPath);
    assert.equal(path.basename(args[0]), 'panel_service.py');
    assert.equal(args[1], '--ensure');
    assert.equal(options.windowsHide, true);
    queueMicrotask(() => callback(null, JSON.stringify({ready: true, started: false})));
  };
  try {return require(path.join(bundle, 'index.cjs'));} finally {child.execFile = original;}
}
const {workbenchFetchRoute} = loadPlugin(path.resolve(__dirname, '../../integrations/dsh-brain'))._test;

function region(code, name) {
  const start = code.indexOf('//#region ' + name + '\n');
  assert.ok(start >= 0, 'installed region missing: ' + name);
  const end = code.indexOf('//#endregion', start);
  assert.ok(end > start);
  return code.slice(start, end);
}

async function main() {
  const root = path.join(process.argv[2], 'dsh/node_modules/@deepseek-ai/dsh-client-connection/lib');
  const {Context, Service} = await import(pathToFileURL(path.join(process.argv[2],
    'dsh/node_modules/@deepseek-ai/cordis/lib/index.js')));
  const hostCode = fs.readFileSync(path.join(root, 'index.js'), 'utf8');
  const clientCode = fs.readFileSync(path.join(root, 'client.browser.js'), 'utf8');
  // No browser credential provider is created. A fixture Peer supplies only
  // lifetime/principal fields; Service, injection and route ownership are real.
  class FixturePeer {
    constructor(ctx, options) { this.ctx = ctx; this.principal = options.principal; }
    async dispose() {}
  }
  const shared = {Request, Response, URL, Headers, Blob, FormData, console, crypto: webcrypto,
    Service, Peer: FixturePeer, isRemoteAuthority: () => false}; // fixture trustedHosts=[]
  const host = vm.runInNewContext(`${region(hostCode, 'lib/types/rpc.js')}\n${region(hostCode, 'lib/types/rpc-host.js')}\nHostConnectionService`, {...shared});
  const connection = Object.create(host.prototype);
  connection.fetchRoutes = new Map();
  connection.interceptors = new Map();
  const owner = {effect: register => register()};
  let opens = 0;
  let fail = false;
  const dispose = connection.registerFetchRoute(owner, workbenchFetchRoute(async () => {
    opens++;
    return fail ? {ok: false, error: {code: 'fixture/not-ready', message: 'not ready'}}
      : {ok: true, value: {url: 'http://127.0.0.1:8765/?host=dsh'}};
  }));
  const handler = connection.createSharedFetchHandler('/api');
  const createRpc = vm.runInNewContext(`${region(clientCode, 'lib/types/rpc.js')}
    ${region(clientCode, 'lib/types/client/random-uuid.js')}
    ${region(clientCode, 'lib/types/client/rpc.js')}
    createWebConnectionRpc`, {...shared});
  const peer = {principal: {kind: 'owner'}};
  const seen = [];
  const rpc = createRpc(async (url, init) => {
    seen.push(url);
    return handler.fetch(new Request('http://native-host/' + url, init), peer);
  });
  assert.equal((await rpc.call('/api', 'model-desk-dsh-brain/open', {sessionId: null})).ok, true);
  assert.deepEqual(seen, ['api/model-desk-dsh-brain/open']);
  fail = true;
  const failure = await rpc.call('/api', 'model-desk-dsh-brain/open', {sessionId: null});
  assert.equal(failure.ok, false);
  assert.equal(failure.error.code, 'fixture/not-ready');
  const forbidden = await handler.fetch(new Request('http://native-host/api/model-desk-dsh-brain/open', {method: 'POST'}), {principal: {kind: 'other'}});
  assert.equal(forbidden.status, 403);
  assert.equal(opens, 2, 'non-owner cannot bootstrap');
  assert.equal(connection.interceptors.size, 0, 'Gateway interceptor is not replaced');
  dispose();
  assert.equal(connection.fetchRoutes.size, 0);
  const bundle = process.argv[3] || path.resolve(__dirname, '../../integrations/dsh-brain');
  const plugin = loadPlugin(bundle);
  const account = require(path.join(bundle, 'account.cjs'));
  const originalAccount = account.createAccountAdapter;
  account.createAccountAdapter = () => ({async start() {}, async dispose() {}});
  const tick = () => new Promise(resolve => setTimeout(resolve, 30));
  try {
    for (const delayed of [false, true]) {
      const ctx = new Context();
      ctx.provide('agents'); ctx.set('agents', {roots: () => [], get: () => null});
      const installation = path.resolve(__dirname, '..');
      const freshSession = {id: 'native-fresh-chat', header: {id: 'native-fresh-chat', cwd: installation}};
      ctx.provide('sessions'); ctx.set('sessions', {get: id => id === freshSession.id ? freshSession : undefined});
      let live;
      const provideConnection = () => ctx.plugin({name: 'fixture-connection',
        apply(owner) {live = new host(owner, [], {});}});
      let provider;
      if (!delayed) {provider = provideConnection(); await provider;}
      const brain = ctx.plugin(plugin, {python: process.execPath,
        brainMcpServer: path.join(installation, 'brain_mcp_server.py'),
        workflowSettings: path.join(installation, 'workflow_settings.py'),
        panelUrl: 'http://127.0.0.1:8765'});
      await brain;
      if (delayed) {provider = provideConnection(); await provider;}
      await tick();
      assert.equal(live.fetchRoutes.size, 1, `real Cordis registration, delayed=${delayed}`);
      // A new native chat can have a Session but no Agent before the first
      // message. Header lookup must use the injected Session service too.
      const sharedRoute = live.createSharedFetchHandler('/api');
      const opened = await sharedRoute.fetch(new Request('http://native-host/api/model-desk-dsh-brain/open', {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({type: 'client-request', rpcId: 'fresh',
          method: 'model-desk-dsh-brain/open', payload: {sessionId: freshSession.id}}),
      }), peer);
      // loadPlugin replaces external bootstrap with a no-process fixture.
      assert.equal(opened.status, 200);
      const freshResult = await opened.json();
      assert.equal(freshResult.result.ok, true, freshResult.result.error?.message);
      assert.equal(new URL(freshResult.result.value.url).searchParams.get('project'), fs.realpathSync(installation));
      assert.equal(ctx.get('webServer'), undefined, 'route registration does not require WebServer');
      const oldConnection = live;
      await provider.dispose(); await tick();
      assert.equal(oldConnection.fetchRoutes.size, 0, 'withdrawal removes owned route');
      provider = provideConnection(); await provider; await tick();
      assert.equal(live.fetchRoutes.size, 1, 'replacement connection receives route');
      await brain.dispose(); await tick();
      assert.equal(live.fetchRoutes.size, 0, 'plugin disposal removes route');
      await provider.dispose();
    }
  } finally {account.createAccountAdapter = originalAccount;}
  console.log(JSON.stringify({installed_connection_contract: 'passed', successful_open: true,
    failure_envelope: true, non_owner_rejected: true, shared_interceptor_untouched: true,
    real_cordis_both_startup_orders: true, dependency_replacement: true, new_chat_without_agent: true,
    profile_read: false, model_called: false}));
}

main().catch(error => {console.error(String(error.message)); process.exitCode = 1;});
