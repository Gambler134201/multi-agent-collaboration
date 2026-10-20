'use strict';

// Run with DSH's Electron in RUN_AS_NODE mode and an explicit app.asar path.
// Reads only installed public JS. No profiles, credentials, sockets or models.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {webcrypto} = require('node:crypto');
const {workbenchFetchRoute} = require('../../integrations/dsh-brain/index.cjs')._test;

function region(code, name) {
  const start = code.indexOf('//#region ' + name + '\n');
  assert.ok(start >= 0, 'installed region missing: ' + name);
  const end = code.indexOf('//#endregion', start);
  assert.ok(end > start);
  return code.slice(start, end);
}

async function main() {
  const root = path.join(process.argv[2], 'dsh/node_modules/@deepseek-ai/dsh-client-connection/lib');
  const hostCode = fs.readFileSync(path.join(root, 'index.js'), 'utf8');
  const clientCode = fs.readFileSync(path.join(root, 'client.browser.js'), 'utf8');
  const shared = {Request, Response, URL, Headers, Blob, FormData, console, crypto: webcrypto};
  const host = vm.runInNewContext(`class Service {}\n${region(hostCode, 'lib/types/rpc.js')}\n${region(hostCode, 'lib/types/rpc-host.js')}\nHostConnectionService`, {...shared});
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
  console.log(JSON.stringify({installed_connection_contract: 'passed', successful_open: true,
    failure_envelope: true, non_owner_rejected: true, shared_interceptor_untouched: true,
    profile_read: false, model_called: false}));
}

main().catch(error => {console.error(String(error.message)); process.exitCode = 1;});
