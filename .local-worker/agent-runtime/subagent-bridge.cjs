// Model Desk controlled subagent bridge.
//
// DSH publishes observe-only lifecycle edges for every accepted child:
// `subagent/start` and `subagent/end` with {runId, provider, id, local} plus the
// terminal stopReason/lastAssistantMessage.  This plugin records those edges to
// a per-run ledger so the *executor* — not free text guessing — is the source of
// the parent/child association.  It never changes a run, never starts a child
// itself and never blocks the parent: `apply` only subscribes.
//
// The subagent service dispatches with a scope-filtered carrier, so the
// listener must be registered `{global: true}` to receive it (same as the
// package's own invariant companion).
const fs = require('fs');

const SCHEMA = 1;
const SOURCE = 'dsh-subagent-lifecycle';

function ledgerPath(config) {
  const configured = config && typeof config.ledger === 'string' ? config.ledger : '';
  return configured || process.env.MODEL_DESK_SUBAGENT_LEDGER || '';
}

function say(file, row) {
  if (!file) return;
  try {
    fs.appendFileSync(file, JSON.stringify(row) + '\n');
  } catch (error) {
    // Observation must never break a real run.
  }
}

function text(value, limit) {
  try {
    if (value === undefined || value === null) return null;
    if (typeof value !== 'string') return null;
    return value.length > limit ? value.slice(0, limit) : value;
  } catch (error) {
    return null;
  }
}

function childModel(child) {
  const candidates = [
    () => child.model,
    () => child.modelId,
    () => child.config && child.config.model,
    () => child.options && child.options.model,
    () => child.route && child.route.model,
    () => child.provider && child.provider.model,
    () => child.session && child.session.model,
  ];
  for (const read of candidates) {
    try {
      const value = read();
      if (typeof value === 'string' && value) return value;
    } catch (error) {
      // keep probing the next shape
    }
  }
  return null;
}

function apply(ctx, config) {
  const file = ledgerPath(config);
  const record = (type, info, extra) => say(file, {
    schema: SCHEMA,
    source: SOURCE,
    type,
    at: new Date().toISOString(),
    provider: text(info && info.provider, 64),
    child_id: text(info && info.id, 200),
    run_id: text(info && String(info.runId), 120),
    local: !!(info && info.local),
    ...extra,
  });

  ctx.on('subagent/start', (info) => {
    let child = null;
    let model = null;
    let sessionId = null;
    try {
      child = ctx.agents && typeof ctx.agents.get === 'function' ? ctx.agents.get(info.id) : null;
    } catch (error) {
      child = null;
    }
    if (child) {
      model = childModel(child);
      try {
        const value = child.id || (child.session && child.session.id);
        sessionId = value ? String(value) : null;
      } catch (error) {
        sessionId = null;
      }
    }
    record('start', info, { child_model: model, child_session: sessionId, child_present: !!child });
  }, { global: true });

  ctx.on('subagent/end', (info) => {
    const output = info && typeof info.lastAssistantMessage === 'string' ? info.lastAssistantMessage : '';
    record('end', info, {
      stop_reason: text(info && info.stopReason, 64),
      output_chars: output.length,
      output_preview: output ? output.slice(0, 200) : null,
    });
  }, { global: true });
}

apply.inject = ['agents'];

module.exports = apply;
