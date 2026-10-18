'use strict';

/**
 * Offline boundary tests for the optional DSH native main-brain bundle.
 *
 * These tests never load a real DSH profile, start the workbench, call a model
 * or read account/session data. A fake Cordis/Agent host records the actual
 * agent-scoped registrations, and a temporary Node script stands in for
 * `workflow_settings.py --status` so the switch can be flipped without touching
 * real settings.
 *
 *   node --test .local-worker/tests/test_dsh_brain_plugin.cjs
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const PLUGIN_PATH = path.join(__dirname, '..', '..', 'integrations', 'dsh-brain', 'index.cjs');
const PLUGIN = require(PLUGIN_PATH);
const { RULE_SECTION } = PLUGIN._test;

const STATUS_SCRIPT = `'use strict';
const fs = require('node:fs');
const path = require('node:path');
try {
  process.stdout.write(fs.readFileSync(path.join(__dirname, 'status.json'), 'utf8'));
} catch {
  process.exit(2);
}
`;

const FAKE_MCP = `'use strict';
exports.name = 'fake-mcp-client';
exports.inject = ['tools'];
exports.Config = (config) => ({ ...config, maxInstructionBytes: 4096 });
exports.apply = async function apply() {};
`;

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-brain-plugin-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function write(file, content) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, content, 'utf8');
}

function fixture(t) {
  const dir = tempDir(t);
  const project = path.join(dir, 'project');
  fs.mkdirSync(project, { recursive: true });
  const statusScript = path.join(dir, 'status.cjs');
  const statusFile = path.join(dir, 'status.json');
  const brainServer = path.join(dir, 'brain_mcp_server.py');
  const fakeMcp = path.join(dir, 'fake-mcp.cjs');
  write(statusScript, STATUS_SCRIPT);
  write(statusFile, JSON.stringify({ enabled: true, readable: true }));
  write(brainServer, '# fixture only; never executed in these tests\n');
  write(fakeMcp, FAKE_MCP);
  return {
    dir,
    project,
    projectReal: fs.realpathSync.native(project),
    statusFile,
    config: {
      schema: 1,
      python: process.execPath,
      brainMcpServer: brainServer,
      workflowSettings: statusScript,
      panelUrl: 'http://127.0.0.1:8765',
      serverName: 'model_desk_brain',
      ownerPrefix: 'dsh',
      toolCallTimeoutMs: 10_000,
      statusTimeoutMs: 5_000,
      mcpClientPath: fakeMcp,
    },
  };
}

function setStatus(f, value) {
  write(f.statusFile, typeof value === 'string' ? value : JSON.stringify(value));
}

function flagValue(args, flag) {
  const index = args.indexOf(flag);
  return index >= 0 ? args[index + 1] : undefined;
}

function makeEffectHost() {
  const effects = [];
  return {
    effects,
    effect(callback) {
      const iterator = callback();
      if (!iterator || typeof iterator.next !== 'function') {
        const cleanup = iterator;
        effects.push(cleanup);
        let called = false;
        return async () => {
          if (called) return;
          called = true;
          if (typeof cleanup === 'function') await cleanup();
        };
      }
      const first = iterator.next();
      if (!first.done) iterator.next();
      const cleanup = first.done ? null : first.value;
      effects.push(cleanup);
      let called = false;
      return async () => {
        if (called) return;
        called = true;
        if (typeof cleanup === 'function') await cleanup();
      };
    },
  };
}

function createRootCtx(initialAgents = []) {
  const agents = [...initialAgents];
  const listeners = new Map();
  const host = makeEffectHost();
  const ctx = {
    agents: {
      roots: () => agents.slice(),
      get: (id) => agents.find((agent) => agent.id === id),
    },
    logger: { warn() {}, info() {}, error() {} },
    on(event, handler, options) {
      const entries = listeners.get(event) || [];
      entries.push({ handler, options });
      listeners.set(event, entries);
      return () => {
        const current = listeners.get(event) || [];
        const index = current.findIndex((entry) => entry.handler === handler);
        if (index >= 0) current.splice(index, 1);
      };
    },
    effect: host.effect,
  };
  ctx.__agents = agents;
  ctx.__listeners = listeners;
  ctx.__effects = host.effects;
  ctx.__fire = async (event, payload) => {
    for (const entry of [...(listeners.get(event) || [])]) {
      await entry.handler(payload);
    }
  };
  ctx.__handlers = (event) => [...(listeners.get(event) || [])].map((entry) => entry.handler);
  ctx.__disposeAll = async () => {
    for (const disposer of [...host.effects].reverse()) {
      if (typeof disposer === 'function') await disposer();
    }
  };
  return ctx;
}

function makeAgent(id, cwd) {
  const listeners = new Map();
  const guards = [];
  const sections = [];
  const mounts = [];
  const effects = [];
  const makeAgentCtx = {
    listeners,
    guards,
    sections,
    mounts,
    effects,
    on(event, handler, options) {
      const entries = listeners.get(event) || [];
      entries.push({ handler, options });
      listeners.set(event, entries);
      return () => {
        const current = listeners.get(event) || [];
        const index = current.findIndex((entry) => entry.handler === handler);
        if (index >= 0) current.splice(index, 1);
      };
    },
    effect(callback) {
      const iterator = callback();
      const first = iterator && typeof iterator.next === 'function' ? iterator.next() : { done: true, value: callback };
      const cleanup = first.done ? null : first.value;
      effects.push(cleanup);
      let called = false;
      return async () => {
        if (called) return;
        called = true;
        if (typeof cleanup === 'function') await cleanup();
      };
    },
    plugin(moduleValue, config) {
      const mount = { moduleValue, config, disposed: false };
      mounts.push(mount);
      return {
        disposed: false,
        async dispose() {
          this.disposed = true;
          mount.disposed = true;
        },
        scope: {
          async dispose() {
            this.disposed = true;
          },
        },
      };
    },
    tools: {
      guard(guard) {
        guards.push(guard);
        return () => {
          const index = guards.indexOf(guard);
          if (index >= 0) guards.splice(index, 1);
        };
      },
    },
    systemPrompt: {
      section(section) {
        sections.push(section);
        return () => {
          const index = sections.indexOf(section);
          if (index >= 0) sections.splice(index, 1);
        };
      },
    },
  };
  const agent = { id, session: { id, header: { id, cwd } }, ctx: makeAgentCtx };
  makeAgentCtx.agent = agent;
  return agent;
}

async function applyPlugin(t, fixtureValue, options = {}) {
  const ctx = options.ctx || createRootCtx(options.agents || []);
  await PLUGIN.apply(ctx, options.config || fixtureValue.config);
  t.after(async () => {
    try {
      await ctx.__disposeAll();
    } catch {
      // Best effort cleanup of the fake host.
    }
  });
  return ctx;
}

function ownerGuard(agent) {
  return agent.ctx.guards[0];
}

function guardCall(agent, name, execAgent = agent) {
  const guard = ownerGuard(agent);
  assert.equal(typeof guard, 'function', 'expected one agent-scoped tool guard');
  return guard({ name, agent: execAgent, args: {} });
}

function publicName(raw) {
  return `mcp__model_desk_brain__${raw}`;
}

async function firePreStep(agent, step = 1, decision = { kind: 'continue', messages: [] }) {
  const handlers = agent.ctx.listeners.get('agent/pre-step') || [];
  assert.ok(handlers.length > 0, 'expected a pre-step listener');
  for (const entry of handlers) {
    await entry.handler(
      { agent, messages: [], step, signal: { aborted: false } },
      async () => decision,
    );
  }
}

function currentRules(agent) {
  return agent.ctx.sections.find((section) => section.name === RULE_SECTION);
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test('binds one top-level native chat and mounts one agent-scoped MCP client', async (t) => {
  const f = fixture(t);
  const agent = makeAgent('native-chat-1', f.project);
  const ctx = await applyPlugin(t, f, { agents: [agent] });

  assert.equal(agent.ctx.mounts.length, 1, 'exactly one MCP mount per Agent');
  assert.equal(ctx.__listeners.get('agent/created').length, 1);
  const mount = agent.ctx.mounts[0];
  assert.equal(mount.moduleValue.name, 'fake-mcp-client');
  assert.equal(mount.config.transport, 'stdio');
  assert.equal(mount.config.serverName, 'model_desk_brain');
  assert.equal(mount.config.command, process.execPath);
  assert.equal(mount.config.cwd, f.projectReal);
  assert.equal(mount.config.failOnStartupError, true);
  assert.equal(mount.config.toolCallTimeoutMs, 10_000);
  assert.equal(mount.config.maxInstructionBytes, 4096, 'native client Config defaults are applied');

  const args = mount.config.args;
  assert.equal(args[0], f.config.brainMcpServer);
  assert.equal(flagValue(args, '--project'), f.projectReal);
  assert.equal(flagValue(args, '--host'), 'dsh');
  assert.equal(flagValue(args, '--role'), 'brain');
  assert.equal(flagValue(args, '--session-binding'), 'user_bound');
  assert.match(flagValue(args, '--session-id'), /^dshb-[0-9a-f]{32}$/);
  assert.equal(flagValue(args, '--native-session-id'), 'native-chat-1');
  assert.equal(flagValue(args, '--launcher'), 'dsh-brain-plugin');
  assert.equal(flagValue(args, '--panel-url'), 'http://127.0.0.1:8765');
  assert.equal(flagValue(args, '--model'), undefined, 'model is not guessed');
  assert.equal(flagValue(args, '--host-version'), undefined, 'host version is not guessed');

  const rules = currentRules(agent);
  assert.ok(rules, 'rules are active while the switch is on');
  assert.equal(rules.order, PLUGIN._test.RULE_ORDER);
  assert.equal(rules.interpolate, false);
  const text = rules.text({ agent, scope: agent });
  assert.match(text, /主脑/);
  assert.match(text, /completed 不等于 accepted/);
  assert.match(text, /最多一次整改/);
  assert.match(text, /尚未接通自动唤醒/);
  assert.match(text, /不要周期性轮询/);
  assert.match(text, /brain_dispatch/);
  assert.match(text, /reviewer_file/);
  assert.match(text, new RegExp(f.projectReal.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));

  const child = makeAgent('native-chat-1-child', f.project);
  assert.equal(rules.text({ agent: child, scope: child }), '', 'child assembly gets no brain rules');
  assert.match(guardCall(agent, publicName('brain_dispatch'), child), /子 Agent|只允许/);
  assert.equal(guardCall(agent, publicName('brain_dispatch')), undefined);
  assert.equal(guardCall(agent, publicName('brain_status')), undefined);
  assert.equal(guardCall(agent, 'read_file'), undefined, 'normal DSH tools are not blocked');

  // Duplicate created, sequential and concurrent, must not remount.
  await ctx.__fire('agent/created', { agent, source: 'fresh' });
  await Promise.all([
    ctx.__fire('agent/created', { agent, source: 'fresh' }),
    ctx.__fire('agent/created', { agent, source: 'fresh' }),
  ]);
  assert.equal(agent.ctx.mounts.length, 1);
  assert.equal(agent.ctx.guards.length, 1);
  assert.equal(agent.ctx.sections.length, 1);
});

test('fails closed for missing/relative/mismatched native identity and project', async (t) => {
  const f = fixture(t);
  const file = path.join(f.dir, 'not-a-directory.txt');
  write(file, 'x');
  const cases = [
    ['relative cwd', 'relative-project'],
    ['missing cwd', path.join(f.dir, 'does-not-exist')],
    ['cwd is a file', file],
    ['agent/session id mismatch', f.project, { id: 'native-a', session: 'native-b', header: 'native-a' }],
    ['header id mismatch', f.project, { id: 'native-a', session: 'native-a', header: 'native-b' }],
    ['header missing', f.project, null],
  ];

  for (const [label, cwd, ids] of cases) {
    const agent = makeAgent(ids ? ids.id : 'native-a', cwd);
    if (ids) {
      agent.session.id = ids.session;
      agent.session.header.id = ids.header;
    } else {
      agent.session.header = undefined;
    }
    const ctx = createRootCtx([agent]);
    await PLUGIN.apply(ctx, f.config);
    assert.equal(agent.ctx.mounts.length, 0, `${label}: no mount`);
    assert.equal(agent.ctx.guards.length, 0, `${label}: no guard`);
    assert.equal(agent.ctx.sections.length, 0, `${label}: no rules`);
    await ctx.__disposeAll();
  }
});

test('only an exact root instance is bound; missing host API fails closed without throwing', async (t) => {
  const f = fixture(t);
  const other = makeAgent('other-root', f.project);
  const child = makeAgent('child-agent', f.project);
  const ctx = createRootCtx([other]);
  await PLUGIN.apply(ctx, f.config);
  assert.equal(other.ctx.mounts.length, 1);
  assert.equal(child.ctx.mounts.length, 0, 'non-root Agent is not bound');
  await ctx.__disposeAll();

  const noAgents = { logger: { warn() {} }, on() {}, effect() {} };
  await assert.doesNotReject(() => PLUGIN.apply(noAgents, f.config));

  const throwingRoots = createRootCtx([]);
  throwingRoots.agents.roots = () => {
    throw new Error('roots unavailable');
  };
  await assert.doesNotReject(() => PLUGIN.apply(throwingRoots, f.config));
  assert.equal(throwingRoots.__listeners.get('agent/created')?.length || 0, 0);
});

test('first binding stays fixed when the native project drifts', async (t) => {
  const f = fixture(t);
  const otherProject = path.join(f.dir, 'other-project');
  fs.mkdirSync(otherProject, { recursive: true });
  const agent = makeAgent('drift-chat', f.project);
  const ctx = await applyPlugin(t, f, { agents: [agent] });
  const originalArgs = agent.ctx.mounts[0].config.args.slice();

  agent.session.header.cwd = otherProject;
  assert.match(guardCall(agent, publicName('brain_dispatch')), /漂移/);
  await flush();
  assert.equal(currentRules(agent), undefined, 'drifted binding removes brain rules');
  assert.equal(agent.ctx.mounts.length, 1, 'drift never creates a second mount');
  assert.deepEqual(agent.ctx.mounts[0].config.args, originalArgs, 'project arguments stay at first binding');

  // Restoring the path does NOT resurrect the binding: fail closed is sticky.
  agent.session.header.cwd = f.project;
  assert.match(guardCall(agent, publicName('brain_dispatch')), /失效/);

  await firePreStep(agent, 1);
  assert.match(guardCall(agent, publicName('brain_status')), /失效/);
  assert.equal(agent.ctx.mounts.length, 1);
});

test('different native chats stay isolated and cannot use each other guards', async (t) => {
  const f = fixture(t);
  const projectB = path.join(f.dir, 'project-b');
  fs.mkdirSync(projectB, { recursive: true });
  const agentA = makeAgent('chat-a', f.project);
  const agentB = makeAgent('chat-b', projectB);
  await applyPlugin(t, f, { agents: [agentA, agentB] });

  assert.equal(agentA.ctx.mounts.length, 1);
  assert.equal(agentB.ctx.mounts.length, 1);
  const argsA = agentA.ctx.mounts[0].config.args;
  const argsB = agentB.ctx.mounts[0].config.args;
  assert.equal(flagValue(argsA, '--project'), f.projectReal);
  assert.equal(flagValue(argsB, '--project'), fs.realpathSync.native(projectB));
  assert.notEqual(flagValue(argsA, '--session-id'), flagValue(argsB, '--session-id'));
  assert.notEqual(flagValue(argsA, '--owner'), flagValue(argsB, '--owner'));
  assert.notEqual(flagValue(argsA, '--owner'), 'dsh', 'owner is not a shared fixed identity');

  assert.match(guardCall(agentA, publicName('brain_dispatch'), agentB), /只允许/);
  assert.match(guardCall(agentB, publicName('brain_dispatch'), agentA), /只允许/);
  const rulesA = currentRules(agentA);
  assert.equal(rulesA.text({ agent: agentB, scope: agentB }), '');
  assert.equal(rulesA.text({ agent: agentA, scope: agentA }).length > 0, true);
});

test('switch is re-read per dispatch and is never a cached allow flag', async (t) => {
  const f = fixture(t);
  const agent = makeAgent('switch-chat', f.project);
  await applyPlugin(t, f, { agents: [agent] });
  assert.ok(currentRules(agent));
  assert.equal(guardCall(agent, publicName('brain_dispatch')), undefined);

  setStatus(f, { enabled: false, readable: true });
  assert.match(guardCall(agent, publicName('brain_dispatch')), /关闭/);
  assert.equal(currentRules(agent), undefined, 'guard removes auto-dispatch rules immediately');
  assert.match(guardCall(agent, publicName('brain_rework')), /关闭/);
  assert.equal(guardCall(agent, publicName('brain_status')), undefined, 'status remains available');
  assert.equal(guardCall(agent, publicName('brain_delivery')), undefined, 'delivery remains available');
  assert.equal(guardCall(agent, publicName('brain_review')), undefined, 'review/return remains available');
  assert.equal(guardCall(agent, 'read_file'), undefined);

  await firePreStep(agent, 1);
  assert.equal(currentRules(agent), undefined, 'switch off removes auto-dispatch rules');

  setStatus(f, { enabled: true, readable: false, error: 'unreadable' });
  assert.match(guardCall(agent, publicName('brain_dispatch')), /不可读/);
  setStatus(f, '{not json');
  assert.match(guardCall(agent, publicName('brain_dispatch')), /不可读/);
  fs.rmSync(f.statusFile);
  assert.match(guardCall(agent, publicName('brain_dispatch')), /不可读/, 'missing settings snapshot is OFF');

  setStatus(f, { enabled: true, readable: true });
  assert.equal(guardCall(agent, publicName('brain_dispatch')), undefined, 'fresh on state allows again');
  await firePreStep(agent, 1);
  assert.ok(currentRules(agent), 'switch on restores auto-dispatch rules');
  assert.equal(agent.ctx.mounts.length, 1, 'switch changes never remount the MCP client');

  setStatus(f, { enabled: false, readable: true });
  assert.match(guardCall(agent, publicName('brain_dispatch')), /关闭/, 'guard does not reuse the pre-step state');

  // A closed switch does not reject or alter an ordinary chat step.
  setStatus(f, { enabled: false, readable: true });
  const decision = { kind: 'continue', messages: ['keep'] };
  await firePreStep(agent, 2, decision);
  assert.deepEqual(decision, { kind: 'continue', messages: ['keep'] });
});

test('executor marker rejects the adapter and every brain tool', async (t) => {
  const f = fixture(t);
  const previous = process.env.MODEL_DESK_EXECUTOR;
  try {
    process.env.MODEL_DESK_EXECUTOR = '1';
    const blocked = makeAgent('executor-profile', f.project);
    const blockedCtx = createRootCtx([blocked]);
    await PLUGIN.apply(blockedCtx, f.config);
    assert.equal(blockedCtx.__listeners.get('agent/created')?.length || 0, 0);
    assert.equal(blocked.ctx.mounts.length, 0);

    delete process.env.MODEL_DESK_EXECUTOR;
    const agent = makeAgent('marker-later', f.project);
    await applyPlugin(t, f, { agents: [agent] });
    process.env.MODEL_DESK_EXECUTOR = '1';
    assert.match(guardCall(agent, publicName('brain_dispatch')), /执行器标记/);
    assert.match(guardCall(agent, publicName('brain_status')), /执行器标记/);
    assert.equal(guardCall(agent, 'read_file'), undefined, 'normal DSH tools still work');
  } finally {
    if (previous === undefined) delete process.env.MODEL_DESK_EXECUTOR;
    else process.env.MODEL_DESK_EXECUTOR = previous;
  }
});

test('invalid configuration and MCP startup failure stay inert without throwing', async (t) => {
  const f = fixture(t);
  const invalidConfigs = [
    ['missing configFile', { configFile: '' }],
    ['configFile not found', { configFile: path.join(f.dir, 'missing-config.json') }],
    ['fixed project', { ...f.config, project: f.project }],
    ['fixed owner', { ...f.config, owner: 'shared' }],
    ['relative python', { ...f.config, python: 'python.exe' }],
    ['unknown field', { ...f.config, unexpected: true }],
    ['missing mcp module', { ...f.config, mcpClientPath: path.join(f.dir, 'missing-mcp.cjs') }],
  ];
  for (const [label, config] of invalidConfigs) {
    const agent = makeAgent(`invalid-${label}`, f.project);
    const ctx = createRootCtx([agent]);
    await assert.doesNotReject(() => PLUGIN.apply(ctx, config), label);
    assert.equal(agent.ctx.mounts.length, 0, `${label}: no mount`);
    assert.equal(agent.ctx.sections.length, 0, `${label}: no rules`);
    await ctx.__disposeAll();
  }

  const throwingModule = path.join(f.dir, 'throwing-mcp.cjs');
  write(throwingModule, "throw new Error('fixture module unavailable');\n");
  const agent = makeAgent('mcp-startup-failure', f.project);
  const ctx = await applyPlugin(t, f, {
    agents: [agent],
    config: { ...f.config, mcpClientPath: throwingModule },
  });
  assert.equal(agent.ctx.mounts.length, 0, 'module resolution failed before ctx.plugin');
  assert.equal(agent.ctx.guards.length, 0, 'failed mount disposes the guard');
  assert.equal(agent.ctx.sections.length, 0, 'failed mount never loads rules');
  assert.equal((agent.ctx.listeners.get('agent/pre-step') || []).length, 0);
  assert.equal(ctx.__listeners.get('agent/created').length, 1, 'listener remains for a later retry');
});

test('configFile is read, inline config overrides it, and no project binding is accepted', async (t) => {
  const f = fixture(t);
  const configFile = path.join(f.dir, 'dsh-brain.json');
  write(configFile, JSON.stringify({
    schema: 1,
    python: f.config.python,
    brainMcpServer: f.config.brainMcpServer,
    workflowSettings: f.config.workflowSettings,
    panelUrl: f.config.panelUrl,
    serverName: 'from_file',
    ownerPrefix: 'dsh',
    toolCallTimeoutMs: 10_000,
    statusTimeoutMs: 5_000,
    mcpClientPath: f.config.mcpClientPath,
  }));
  const agent = makeAgent('config-file-chat', f.project);
  await applyPlugin(t, f, {
    agents: [agent],
    config: { configFile, serverName: 'model_desk_brain' },
  });
  assert.equal(agent.ctx.mounts.length, 1);
  assert.equal(agent.ctx.mounts[0].config.serverName, 'model_desk_brain', 'inline config wins');
  assert.equal(guardCall(agent, publicName('brain_dispatch')), undefined);
});

test('a resumed session id reuses the first binding only when identity/project match', async (t) => {
  const f = fixture(t);
  const projectB = path.join(f.dir, 'project-b');
  fs.mkdirSync(projectB, { recursive: true });
  const agent = makeAgent('resume-chat', f.project);
  const ctx = createRootCtx([agent]);
  await PLUGIN.apply(ctx, f.config);
  assert.equal(agent.ctx.mounts.length, 1);

  await ctx.__fire('agent/disposed', { agent });
  assert.equal(agent.ctx.guards.length, 0);
  ctx.__agents.splice(0, ctx.__agents.length);

  const resumed = makeAgent('resume-chat', f.project);
  ctx.__agents.push(resumed);
  await ctx.__fire('agent/created', { agent: resumed, source: 'resume' });
  assert.equal(resumed.ctx.mounts.length, 1, 'same native identity/project reuses the first binding');
  assert.equal(flagValue(resumed.ctx.mounts[0].config.args, '--project'), f.projectReal);

  await ctx.__fire('agent/disposed', { agent: resumed });
  ctx.__agents.splice(0, ctx.__agents.length);
  const drifted = makeAgent('resume-chat', projectB);
  ctx.__agents.push(drifted);
  await ctx.__fire('agent/created', { agent: drifted, source: 'resume' });
  assert.equal(drifted.ctx.mounts.length, 0, 'a resumed id with a different project is not rebound');
  assert.equal(drifted.ctx.guards.length, 0);
});

test('resolves an installed MCP client from the DSH context base without a vendor path', async (t) => {
  const f = fixture(t);
  const { pathToFileURL } = require('node:url');
  const profile = path.join(f.dir, 'profile');
  const packageDir = path.join(profile, 'node_modules', '@deepseek-ai', 'dsh-mcp-client');
  write(path.join(packageDir, 'package.json'), JSON.stringify({
    name: '@deepseek-ai/dsh-mcp-client',
    version: 'fixture',
    main: 'index.cjs',
  }));
  write(path.join(packageDir, 'index.cjs'), FAKE_MCP);
  const agent = makeAgent('base-url-chat', f.project);
  agent.ctx.baseUrl = pathToFileURL(path.join(profile, 'host.cjs')).href;
  const config = { ...f.config };
  delete config.mcpClientPath;
  await applyPlugin(t, f, { agents: [agent], config });
  assert.equal(agent.ctx.mounts.length, 1);
  assert.equal(agent.ctx.mounts[0].moduleValue.name, 'fake-mcp-client');
  assert.equal(agent.ctx.mounts[0].config.serverName, 'model_desk_brain');
});

test('disposal and hot reload dispose old MCP fibers without duplicate live mounts', async (t) => {
  const f = fixture(t);
  const agent = makeAgent('reload-chat', f.project);
  const ctx = createRootCtx([agent]);
  await PLUGIN.apply(ctx, f.config);
  const firstMount = agent.ctx.mounts[0];
  assert.equal(agent.ctx.mounts.length, 1);

  // Re-applying on the same root context is the hot-reload path: the previous
  // instance is disposed before the new one binds existing roots.
  await PLUGIN.apply(ctx, f.config);
  assert.equal(agent.ctx.mounts.length, 2);
  assert.equal(firstMount.disposed, true, 'old MCP fiber is disposed');
  assert.equal(agent.ctx.mounts[1].disposed, false);
  assert.equal(agent.ctx.guards.length, 1);
  assert.equal(agent.ctx.sections.length, 1);

  const secondMount = agent.ctx.mounts[1];
  await ctx.__disposeAll();
  await flush();
  assert.equal(secondMount.disposed, true);
  assert.equal(agent.ctx.guards.length, 0);
  assert.equal(agent.ctx.sections.length, 0);
  assert.equal((agent.ctx.listeners.get('agent/pre-step') || []).length, 0);
  assert.equal(ctx.__listeners.get('agent/created').length, 0);
});

test('real workbench record proof marker excludes executor processes', async (t) => {
  const f = fixture(t);
  const agent = makeAgent('executor-record-proof', f.project);
  const prior = process.env.MODEL_DESK_RECORD_PROOF;
  process.env.MODEL_DESK_RECORD_PROOF = path.join(f.dir, 'proof.json');
  try {
    await applyPlugin(t, f, { agents: [agent] });
    assert.equal(agent.ctx.mounts.length, 0, 'actual executor environment must not get brain tools');
  } finally {
    if (prior === undefined) delete process.env.MODEL_DESK_RECORD_PROOF;
    else process.env.MODEL_DESK_RECORD_PROOF = prior;
  }
});

test('rule registration failure denies new work even with enabled workflow', async (t) => {
  const f = fixture(t);
  const agent = makeAgent('rules-failed', f.project);
  agent.ctx.systemPrompt.section = () => { throw new Error('registration fault'); };
  await applyPlugin(t, f, { agents: [agent] });
  assert.equal(agent.ctx.mounts.length, 1);
  assert.equal(agent.ctx.sections.length, 0);
  assert.match(guardCall(agent, publicName('brain_dispatch')), /规则|未就绪/);
  assert.equal(guardCall(agent, publicName('brain_delivery')), undefined);
});

test('disposing during MCP startup also disposes the late fiber before dropping guard', async (t) => {
  const f = fixture(t);
  const agent = makeAgent('late-start', f.project);
  const ctx = await applyPlugin(t, f);
  let finishMount;
  let started;
  const start = new Promise((resolve) => { started = resolve; });
  const fiber = { disposed: false, dispose() { this.disposed = true; } };
  agent.ctx.plugin = () => {
    started();
    return new Promise((resolve) => { finishMount = resolve; });
  };
  ctx.__agents.push(agent);
  const created = ctx.__fire('agent/created', { agent });
  await start;
  const disposed = ctx.__fire('agent/disposed', { agent });
  await flush();
  assert.match(guardCall(agent, publicName('brain_review')), /失效/);
  finishMount(fiber);
  await Promise.all([created, disposed]);
  assert.equal(fiber.disposed, true, 'a late MCP startup must not survive teardown');
  assert.equal(agent.ctx.guards.length, 0);
  assert.equal(agent.ctx.sections.length, 0);
});

test('hot reload waits for Cordis inertia before mounting replacement MCP', async (t) => {
  const f = fixture(t);
  const agent = makeAgent('inertia', f.project);
  let finishTeardown;
  const firstFiber = { dispose() {
    this.inertia = new Promise((resolve) => {
      finishTeardown = () => { this.inertia = undefined; resolve(); };
    });
  } };
  let mounts = 0;
  const normalPlugin = agent.ctx.plugin;
  agent.ctx.plugin = (...args) => {
    mounts += 1;
    return mounts === 1 ? firstFiber : normalPlugin(...args);
  };
  const ctx = await applyPlugin(t, f, { agents: [agent] });
  const reload = PLUGIN.apply(ctx, f.config);
  await flush();
  assert.equal(mounts, 1, 'replacement must await the previous fiber quiescence');
  assert.match(guardCall(agent, publicName('brain_review')), /失效/);
  finishTeardown();
  await reload;
  assert.equal(mounts, 2);
});

test('failed MCP teardown retains denial and refuses a replacement connection', async (t) => {
  const f = fixture(t);
  const agent = makeAgent('failed-dispose', f.project);
  let mounts = 0;
  agent.ctx.plugin = () => {
    mounts += 1;
    return { dispose() { throw new Error('teardown unknown'); } };
  };
  const ctx = await applyPlugin(t, f, { agents: [agent] });
  await PLUGIN.apply(ctx, f.config);
  assert.equal(mounts, 1);
  assert.match(guardCall(agent, publicName('brain_review')), /失效/);
});

test('Windows current-drive-rooted paths cannot become a trusted project', {skip:process.platform!=='win32'}, () => {
  assert.equal(PLUGIN._test.resolveProject('\\Windows'), null);
  assert.equal(PLUGIN._test.resolveProject('/Windows'), null);
});
