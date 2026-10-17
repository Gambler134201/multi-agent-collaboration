'use strict';
/**
 * Model Desk workbench-record write protection (Cordis plugin).
 *
 * Mounted into the real DSH run through the worker patch, this plugin registers
 * one *global* monotonic guard with the installed `@deepseek-ai/dsh-tools`
 * registry.  DSH consults guards after the extensible `tools/pre-execute`
 * waterfall, after the permission decision, and *before* the tool body is
 * dispatched (`ToolRuntime.prepareExecution`), so a denied call never reaches
 * the file system.  A global registration also covers every child agent of a
 * mounted subagent tool.
 *
 * The module is deliberately small and fail-closed:
 *   - the loaded bytes must match the revision the dispatch recorded;
 *   - the guard must actually be reachable through `tools.guardReason`;
 *   - a proof file is written at load time, and the run refuses to start a turn
 *     when that proof is missing or does not match.
 *
 * It never writes through the tool registry, never retries a denied call and
 * never touches real records: the only file it writes is its own proof.
 */

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const policy = require(path.join(__dirname, 'record-policy.cjs'));

const SOURCE = 'model-desk-record-protection';
const SCHEMA = 1;
const POLICY_FILE = path.join(__dirname, 'record-policy.cjs');
const PLUGIN_FILE = __filename;

/** Content identity of the exact protection code DSH loaded. */
function selfRevision() {
  const digest = crypto.createHash('sha256');
  for (const file of [POLICY_FILE, PLUGIN_FILE]) digest.update(fs.readFileSync(file));
  return digest.digest('hex');
}

function proofPath(config) {
  const declared = config && typeof config.proof === 'string' ? config.proof.trim() : '';
  if (declared) return declared;
  const fromEnv = process.env.MODEL_DESK_RECORD_PROOF;
  return typeof fromEnv === 'string' && fromEnv.trim() ? fromEnv.trim() : '';
}

function readProof(file) {
  try {
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    return data && typeof data === 'object' ? data : {};
  } catch (error) {
    return {};
  }
}

function writeProof(file, row) {
  if (!file) throw new Error('Model Desk 记录保护证据路径缺失');
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(row));
}

function agentSessionId(agent) {
  try {
    if (!agent) return null;
    if (typeof agent.id === 'string' && agent.id) return agent.id;
    if (agent.session && typeof agent.session.id === 'string' && agent.session.id) return agent.session.id;
    if (typeof agent.sessionId === 'string' && agent.sessionId) return agent.sessionId;
  } catch (error) {
    return null;
  }
  return null;
}

function apply(ctx, config) {
  const settings = config && typeof config === 'object' ? config : {};
  const root = path.resolve(String(settings.root || process.env.MODEL_DESK_RECORD_ROOT || process.cwd()));
  const expected = String(settings.revision || '');
  const actual = selfRevision();
  if (!expected) throw new Error('Model Desk 记录保护缺少内容指纹，已拒绝装载');
  if (expected !== actual) throw new Error('Model Desk 记录保护内容指纹不一致，已拒绝装载');

  const guard = (exec) => {
    const name = exec && exec.name;
    try {
      return policy.decide(name, exec && exec.arguments, { root });
    } catch (error) {
      // An undecidable call to a governed tool is denied, never allowed.  Other
      // tools stay untouched so an internal fault cannot stop a whole run.
      if (!policy.isGuardedTool(name)) return undefined;
      const detail = String((error && error.message) || error);
      return '工作台记录写入保护内部错误（' + detail + '），已拒绝本次工具调用';
    }
  };

  const dispose = ctx.tools.guard(guard);
  if (typeof dispose !== 'function') {
    throw new Error('Model Desk 记录保护未能在 DSH 工具层注册 guard');
  }
  // Runtime self-test: only this guard answers the probe, and a plain-context
  // registration answers it without an agent scope.  A guard that had landed in
  // an agent layer, or a service without the guard hook, fails here.
  const probe = ctx.tools.guardReason({ name: policy.PROBE_TOOL, arguments: {} });
  if (probe !== policy.PROBE_REASON) {
    throw new Error('Model Desk 记录保护 guard 未在全局工具层生效，已拒绝装载');
  }

  const file = proofPath(settings);
  const base = {
    schema: SCHEMA,
    source: SOURCE,
    revision: actual,
    root,
    scope: 'global',
    probe: 'ok',
    pid: process.pid,
    at: new Date().toISOString(),
    session_id: null,
  };
  writeProof(file, base);

  ctx.on('agent/created', ({ agent }) => {
    const sessionId = agentSessionId(agent);
    if (!sessionId) return;
    const current = readProof(file);
    if (current.session_id) return; // the awaited parent edge records first
    writeProof(file, { ...base, ...current, session_id: sessionId, at: new Date().toISOString() });
  });
}

apply.inject = ['tools', 'agents'];
apply.SOURCE = SOURCE;
apply.selfRevision = selfRevision;

module.exports = apply;
