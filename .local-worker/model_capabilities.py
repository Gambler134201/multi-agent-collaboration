"""Per-model capability declarations and the user's allow/deny policy.

This module is the single place that answers one question: *may this specific
model, on this specific executor, with this specific Agent preset and version,
start subagents right now?*  It is deliberately small and dependency-free so
both the panel (public API) and the DSH worker (the enforcement point) can call
the same decision function and never disagree.

Design rules (D1):

* Capability state is a three-way value: ``supported`` / ``unsupported`` /
  ``unverified``.  A model that has never been measured resolves to
  ``unverified``; there is no boolean default that silently treats an unknown
  model as capable.
* The user's ``allow`` / ``deny`` policy is stored separately from the measured
  evidence.  ``deny`` always wins; ``allow`` can never upgrade ``unverified``
  into ``supported``.  A policy of ``unset`` means "follow the local default".
* Evidence carries the exact identity it was measured on.  If the executor,
  model, Agent preset, release version, upstream endpoint or preset content
  changes, the evidence no longer applies and the capability decays back to
  ``unverified`` (fail closed).
* ``supported`` is never accepted from a missing measurement: an evidence row
  must be explicitly ``measured: true``, carry a parseable ``tested_at`` and
  name the model it was measured on.  A record without a model identity is
  refused instead of being matched to every model.
* Local/home models are denied by default and this round keeps them denied
  regardless of the stored decision.
* Public views never carry secrets or encrypted key material.

The module knows nothing about HTTP, the panel server or the DSH process; it
only reads dictionaries that were already stripped of private fields.  The one
piece of outside knowledge it accepts is a registered installed-version
resolver, so the panel and the worker derive the *same* versioned identity
instead of one of them silently using ``unversioned-v1``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------- contract

CAPABILITY_SCHEMA = 1

STATE_SUPPORTED = "supported"
STATE_UNSUPPORTED = "unsupported"
STATE_UNVERIFIED = "unverified"
STATES = (STATE_SUPPORTED, STATE_UNSUPPORTED, STATE_UNVERIFIED)

POLICY_ALLOW = "allow"
POLICY_DENY = "deny"
POLICY_UNSET = "unset"
POLICIES = (POLICY_ALLOW, POLICY_DENY, POLICY_UNSET)

VERDICT_ALLOW = "allow"
VERDICT_DENY = "deny"

# The task modes a grant can cover.  A measurement only proves the mode it was
# actually run in: the accepted controlled probe is a read-only parent, so it
# proves ``chat`` delegation and never the full-access ``edit`` mode.  Capability
# (what was measured) stays separate from the user allow/deny policy.
MODE_CHAT = "chat"
MODE_EDIT = "edit"
MODES = (MODE_CHAT, MODE_EDIT)
READ_ONLY_MODES = (MODE_CHAT,)

DEFAULT_EXECUTOR = "dsh"
DEFAULT_PRESET = "native"
UNVERSIONED = "unversioned-v1"
# The route/endpoint and the Agent-preset content revision are part of the
# measured identity (D2): a grant must not survive a changed upstream endpoint
# or a changed preset declaration, even when the release version is unchanged.
UNRESOLVED_PRESET = "preset-unresolved-v1"

# A verified grant that is younger than this is still trusted; older evidence is
# reported as expired. Identity/version drift expires evidence immediately and
# does not wait for the clock.
EVIDENCE_MAX_AGE_DAYS = 180

# Only this executor can ever report a verified subagent grant in this round.
# Aidier/direct execution paths stay explicitly unimplemented.
KNOWN_EXECUTORS = ("dsh", "aider", "direct")

# Keys that must never leave this module through a public view.
_PRIVATE_TOKENS = ("secret", "api_key", "apikey", "password", "token", "credential",
                   "authorization", "bearer", "private")

# Usage/accounting keys that legitimately contain "token" as a word and are not
# secrets. Kept as an explicit allowlist so the redaction rule stays conservative.
_PUBLIC_EXCEPTIONS = frozenset({
    "prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
    "cached_tokens", "cache_hit_tokens", "cache_miss_tokens", "max_tokens", "max_input_tokens",
    "max_output_tokens", "request_output_tokens", "token_limit", "tokens", "usage",
})


def _now():
    return datetime.now(timezone.utc)


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _text(value, limit=200):
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _looks_private(key):
    lowered = str(key).lower()
    if lowered in _PUBLIC_EXCEPTIONS:
        return False
    return any(token in lowered for token in _PRIVATE_TOKENS)


def public_view(value):
    """Return a deep copy with every private-looking key removed.

    Used for API responses so a capability payload can never leak a stored key,
    an encrypted secret or an Authorization header even if a caller passes a raw
    registry row by mistake.
    """
    if isinstance(value, dict):
        return {key: public_view(item) for key, item in value.items() if not _looks_private(key)}
    if isinstance(value, (list, tuple)):
        return [public_view(item) for item in value]
    return copy.deepcopy(value)


# ----------------------------------------------------- installed-version hook

_INSTALLED_VERSION_RESOLVER = None


def register_version_resolver(resolver):
    """Register the one resolver for the installed executor version.

    The panel and the worker must bind evidence to the *same* version.  Instead
    of guessing ``unversioned-v1`` in a public view while the worker resolves
    the real release from the installation, the executor adapter registers a
    resolver here at import time.  Passing ``None`` clears it (used by tests and
    by processes that must not touch the installation).
    """
    global _INSTALLED_VERSION_RESOLVER
    _INSTALLED_VERSION_RESOLVER = resolver if callable(resolver) else None


def installed_version():
    """Return the resolver's current answer, or ``None`` when unavailable."""
    if _INSTALLED_VERSION_RESOLVER is None:
        return None
    try:
        return _INSTALLED_VERSION_RESOLVER()
    except Exception:
        return None


def resolve_version(model=None, explicit=None):
    """Resolve one version with an explicit, inspectable precedence.

    Order: caller-supplied identity > row ``executor_version`` > registered
    installation resolver > ``unversioned-v1``.  Public views and enforcement
    both use this function, so they cannot disagree about the version.
    """
    value = _text(explicit, 120)
    if value:
        return value
    if isinstance(model, dict):
        value = _text(model.get("executor_version"), 120)
        if value:
            return value
    return _text(installed_version(), 120) or UNVERSIONED


# ------------------------------------------------------ preset-content hook

_PRESET_REVISION_RESOLVER = None


def register_preset_resolver(resolver):
    """Register the one resolver for an Agent-preset content revision.

    ``executor_adapter`` registers a resolver that hashes the *actual* preset
    declaration (shipped ASAR entry or the user's native file) plus the runtime
    bridges.  Both the panel and the worker then derive the same content
    identity, so replacing a preset without bumping the release version
    invalidates the old evidence instead of silently reusing it.  ``None``
    clears the resolver; an unavailable preset resolves to
    ``preset-unresolved-v1`` (fail closed) rather than matching anything.
    """
    global _PRESET_REVISION_RESOLVER
    _PRESET_REVISION_RESOLVER = resolver if callable(resolver) else None


def resolved_preset_revision(preset):
    """Content revision for one preset id, or the fail-closed placeholder."""
    if _PRESET_REVISION_RESOLVER is None:
        return UNRESOLVED_PRESET
    try:
        value = _PRESET_REVISION_RESOLVER(_text(preset, 40) or DEFAULT_PRESET)
    except Exception:
        return UNRESOLVED_PRESET
    return _text(value, 64) or UNRESOLVED_PRESET


# ------------------------------------------------------------------- identity

IDENTITY_FIELDS = ("executor", "model", "agent_preset", "version", "model_version",
                   "endpoint", "preset_revision")


def evidence_identity(executor=DEFAULT_EXECUTOR, model="", preset=DEFAULT_PRESET,
                      version=None, model_version=None, endpoint=None,
                      preset_revision=None):
    """Build the minimum identity a subagent grant is bound to.

    ``version`` is the executor build (e.g. the DSH release); ``model_version``
    is the configured upstream model revision when the user supplied one;
    ``endpoint`` is the upstream route the worker actually bills against; and
    ``preset_revision`` is the content hash of the mounted Agent preset.  An
    omitted ``preset_revision`` is resolved through the registered hook so a
    fabricated "supported" record cannot skip the content binding.  ``version=
    None`` means "resolve it" (caller > row > installation).
    """
    preset = _text(preset, 40) or DEFAULT_PRESET
    return {
        "executor": _text(executor, 40) or DEFAULT_EXECUTOR,
        "model": _text(model, 200),
        "agent_preset": preset,
        "version": resolve_version(explicit=version),
        "model_version": _text(model_version, 120) or UNVERSIONED,
        "endpoint": _text(endpoint, 300),
        "preset_revision": _text(preset_revision, 64) or resolved_preset_revision(preset),
    }


def capability_digest(block):
    """Stable content hash of a stored capability block.

    Included in the worker session identity so replacing evidence (even with the
    same number of records), editing a grant or flipping the policy is visible
    to the session cache.  Only public, canonical fields are hashed, so an
    unrelated ``updated_at`` change does not churn sessions.
    """
    block = block if isinstance(block, dict) else {}
    evidence = block.get("evidence") if isinstance(block.get("evidence"), list) else []
    canonical = {
        "policy": _text(block.get("policy"), 20) or POLICY_UNSET,
        "evidence": [
            {key: public_view(row.get(key)) for key in
             ("state", "measured", "identity", "tested_at", "executor_version", "test_id", "source")}
            for row in evidence if isinstance(row, dict)
        ],
    }
    text = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def identity_key(identity):
    """Stable comparison key for one identity tuple (order is fixed)."""
    identity = identity if isinstance(identity, dict) else {}
    return (
        _text(identity.get("executor"), 40),
        _text(identity.get("model"), 200),
        _text(identity.get("agent_preset"), 40),
        _text(identity.get("version"), 120),
        _text(identity.get("model_version"), 120),
        _text(identity.get("endpoint"), 300),
        _text(identity.get("preset_revision"), 64),
    )


def identity_differences(recorded, current):
    """Return the identity fields that changed between two measurements."""
    recorded_key = identity_key(recorded)
    current_key = identity_key(current)
    return [name for name, old, new in zip(IDENTITY_FIELDS, recorded_key, current_key) if old != new]


# ------------------------------------------------------------------ evaluation

def _default_policy(model):
    """Local/home models are denied by default; everything else follows state."""
    if isinstance(model, dict) and model.get("provider") == "local":
        return POLICY_DENY
    if isinstance(model, dict) and model.get("id") == "home":
        return POLICY_DENY
    return POLICY_UNSET


def _local_model(model):
    return bool(isinstance(model, dict) and (model.get("provider") == "local" or model.get("id") == "home"))


def _evidence_records(model):
    block = model.get("subagent") if isinstance(model, dict) else None
    if not isinstance(block, dict):
        return []
    records = block.get("evidence")
    if isinstance(records, list):
        return [row for row in records if isinstance(row, dict)]
    if isinstance(records, dict):
        return [records]
    return []


def _match_record(records, identity):
    """Exact identity match only.

    A record whose model (or preset/version) is missing must never be matched to
    an arbitrary model: the old fallback that copied the current model into the
    recorded identity turned a per-executor note into a grant.  Fail closed.
    """
    wanted = identity_key(identity)
    if not wanted[1]:
        # No model in the current identity means nothing can be proven.
        return None
    for record in records:
        recorded = record.get("identity") if isinstance(record.get("identity"), dict) else record
        if identity_key(recorded) == wanted:
            return record
    return None


def _state_from_record(record):
    state = _text(record.get("state"), 20)
    if state not in STATES:
        return None
    if record.get("verified") is False:
        return None
    if record.get("measured") is not True:
        # A claim that is not explicitly a measurement is not evidence, whether
        # it arrived through normalize_block or a hand-edited registry row.
        return None
    return state


def _pick(supplied, key, fallback):
    """Caller-supplied identity wins when the key is present and non-None.

    An explicitly supplied empty endpoint is meaningful ("this identity was
    measured without a route"), so it must not silently fall back to the row's
    endpoint and turn into a wildcard match.
    """
    if key in supplied and supplied.get(key) is not None:
        return supplied[key]
    return fallback


def _record_modes(record):
    """Modes a stored evidence row really measured.

    A read-only probe row proves ``chat`` only, regardless of any ``modes`` field
    a caller attached to it.  Otherwise an explicit ``modes`` list wins; a row
    with neither is unproven, so a mode request against it stays denied.
    """
    if not isinstance(record, dict):
        return []
    if record.get("read_only_probe") is True:
        return list(READ_ONLY_MODES)
    raw = record.get("modes")
    return [mode for mode in (raw if isinstance(raw, list) else []) if mode in MODES]


def evaluate(model, executor=DEFAULT_EXECUTOR, identity=None, now=None, mode=None):
    """Resolve whether subagents may start for this model on this executor.

    ``model`` is a registry row (or any mapping with the same public fields).
    ``identity`` overrides the current measurement identity; when omitted the
    identity is derived from the row itself so panel and worker agree.
    ``mode`` is the task mode being requested (``chat``/``edit``).  When it is
    supplied the stored evidence must have measured that exact mode; an
    unmeasured mode stays denied even under an ``allow`` policy, so a read-only
    probe can never be presented as proof of full-access ``edit`` delegation.

    Returns a JSON-safe dictionary.  ``decision`` is the enforcement result and
    is the only field callers may branch on for allow/deny.
    """
    model = model if isinstance(model, dict) else {}
    executor = _text(executor or model.get("engine"), 40) or DEFAULT_EXECUTOR
    supplied = identity if isinstance(identity, dict) else {}
    current = evidence_identity(
        executor=supplied.get("executor") or executor,
        model=supplied.get("model") or model.get("model") or "",
        preset=supplied.get("agent_preset") or model.get("agent_preset") or DEFAULT_PRESET,
        version=resolve_version(model, supplied.get("version")),
        model_version=_pick(supplied, "model_version", model.get("model_version")),
        endpoint=_pick(supplied, "endpoint", model.get("api_base")),
        preset_revision=_pick(supplied, "preset_revision", model.get("preset_revision")),
    )

    block = model.get("subagent") if isinstance(model.get("subagent"), dict) else {}
    policy = _text(block.get("policy"), 20) or _default_policy(model)
    if policy not in POLICIES:
        policy = _default_policy(model)

    reasons = []
    if executor != DEFAULT_EXECUTOR:
        # Other executors are explicitly unimplemented, never assumed capable.
        return public_view({
            "decision": VERDICT_DENY, "state": STATE_UNVERIFIED, "policy": policy,
            "executor": executor, "identity": public_view(current), "evidence": None,
            "stale": False, "expired": False, "local": _local_model(model),
            "digest": capability_digest(block),
            "mode": _text(mode, 20).lower() or None,
            "proven_modes": [],
            "reasons": [f"执行器 {executor} 尚未实现子代理能力接口"],
            "schema": CAPABILITY_SCHEMA,
        })

    records = _evidence_records(model)
    match = _match_record(records, current)
    stale = False
    expired = False
    record = None
    if match is not None:
        record = match
    elif records:
        # Evidence exists for a different identity: the grant no longer applies.
        stale = True
        recorded = records[-1].get("identity") if isinstance(records[-1], dict) else None
        recorded = recorded if isinstance(recorded, dict) else {}
        changed = identity_differences(recorded, current)
        if changed:
            reasons.append("证据身份已变化：" + "、".join(changed))
        else:
            reasons.append("证据身份与当前执行器/模型/预设/版本不一致")

    state = None
    if record is not None:
        state = _state_from_record(record)
        if state is None:
            reasons.append("证据缺少有效的实测状态")
        measured = _parse_time(record.get("tested_at"))
        if state is not None:
            if measured is None:
                # Missing or unreadable measurement time is not fresh evidence.
                expired = True
                reasons.append("证据缺少可解析的实测时间，按过期处理")
            elif measured > (now or _now()) + timedelta(minutes=5):
                # A future timestamp was never measured at all.
                expired = True
                reasons.append("证据时间晚于当前时间，不能当作实测")
            else:
                age = ((now or _now()) - measured).days
                if age > EVIDENCE_MAX_AGE_DAYS:
                    expired = True
                    reasons.append(f"证据已超过 {EVIDENCE_MAX_AGE_DAYS} 天有效期")

    if state is None:
        state = STATE_UNVERIFIED
        if not reasons:
            reasons.append("尚无该模型在该执行器上的实测证据")

    if state == STATE_SUPPORTED and (stale or expired):
        state = STATE_UNVERIFIED
        if stale and not any(item.startswith("证据身份") for item in reasons):
            reasons.append("证据身份与当前执行器/模型/预设/版本不一致")
    if stale and not any("证据" in item for item in reasons):
        reasons.append("已有证据与当前身份不一致")

    if _local_model(model):
        # User requirement: the home/local model must not derive children in this
        # round even if a stored record claims support.
        policy = POLICY_DENY
        reasons.append("本地/home 模型本轮保持禁止派生子代理")

    decision = VERDICT_DENY
    if policy == POLICY_DENY:
        if not reasons:
            reasons.append("用户策略为禁止")
        if "用户策略禁止" not in reasons:
            reasons.append("用户策略禁止")
    elif policy == POLICY_ALLOW and state == STATE_SUPPORTED and not stale and not expired:
        decision = VERDICT_ALLOW
        reasons.append("用户策略允许且存在同身份实测支持证据")
    else:
        if policy == POLICY_UNSET:
            reasons.append("用户未明确允许")
        decision = VERDICT_DENY

    # Mode eligibility is capability, not policy: a grant can only cover a mode
    # that was actually measured.  A read-only probe therefore allows ``chat``
    # and never silently authorises full-access ``edit`` delegation.
    requested = _text(mode, 20).lower() or None
    if requested is not None and requested not in MODES:
        decision = VERDICT_DENY
        reasons.append(f"未知任务模式 {requested}，按禁止处理")
    proven_modes = sorted(set(_record_modes(record)))
    if requested in MODES and requested not in proven_modes:
        decision = VERDICT_DENY
        if proven_modes:
            reasons.append("实测模式未覆盖 " + requested + "（已证实：" + "、".join(proven_modes)
                           + "），未验证模式默认禁用")
        else:
            reasons.append("证据没有可核对的已实测模式，" + requested + " 模式默认禁用")

    evidence_out = public_view({
        "state": state,
        "tested_at": record.get("tested_at") if record is not None else None,
        "executor_version": record.get("executor_version") if record is not None else None,
        "test_id": record.get("test_id") if record is not None else None,
        "source": record.get("source") if record is not None else None,
    }) if record is not None else None
    result = {
        "schema": CAPABILITY_SCHEMA,
        "decision": decision,
        "state": state,
        "policy": policy,
        "executor": executor,
        "identity": public_view(current),
        "evidence": evidence_out,
        "digest": capability_digest(block),
        "stale": stale,
        "expired": expired,
        "local": _local_model(model),
        "mode": requested,
        "proven_modes": proven_modes,
        "reasons": reasons,
    }
    return public_view(result)


def normalize_block(block, previous=None):
    """Validate and normalize the stored ``subagent`` block for a model row.

    Only the user-owned ``policy`` and an optional pre-measured ``evidence``
    record may come from a client.  The capability *state* is always recomputed
    from evidence by :func:`evaluate`, never trusted from input, and an evidence
    row is only kept when it proves it was actually measured (``measured: true``
    plus a model identity and a parseable ``tested_at``).
    """
    previous = previous if isinstance(previous, dict) else {}
    block = block if isinstance(block, dict) else {}
    policy = _text(block.get("policy", previous.get("policy", POLICY_UNSET)), 20) or POLICY_UNSET
    if policy not in POLICIES:
        raise ValueError("子代理策略只能是 allow、deny 或 unset")
    result = {"schema": CAPABILITY_SCHEMA, "policy": policy,
              "evidence": _normalize_evidence(block.get("evidence", previous.get("evidence")),
                                              previous.get("evidence")),
              "updated_at": block.get("updated_at") or previous.get("updated_at") or _now().isoformat()}
    return result


def _normalize_evidence(value, previous=None):
    """Return the accepted evidence rows; everything unproven is dropped.

    ``value is None`` means "the caller did not touch evidence" and keeps the
    previous rows.  An explicit empty list clears them.  A row is accepted only
    when it is an explicit measurement (``measured is true``), names the model
    it was measured on and carries a parseable ``tested_at``; otherwise it is
    refused instead of being stored as a grant.
    """
    source = previous if value is None else value
    rows = source if isinstance(source, list) else ([source] if isinstance(source, dict) else [])
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = _text(row.get("state"), 20)
        if state not in STATES:
            continue
        if row.get("measured") is not True:
            # Including "supported": no measurement, no evidence.
            continue
        identity = row.get("identity") if isinstance(row.get("identity"), dict) else {}
        model_name = _text(identity.get("model") or row.get("model"), 200)
        if not model_name:
            # A model-less record could match anything; refuse it.
            continue
        tested = _parse_time(row.get("tested_at"))
        if tested is None:
            continue
        raw_modes = row.get("modes")
        modes = [mode for mode in (raw_modes if isinstance(raw_modes, list) else []) if mode in MODES]
        if row.get("read_only_probe") is True:
            # The accepted controlled probe runs a read-only parent, so it proves
            # ``chat`` only.  A client cannot mint an edit grant by adding
            # "edit" to a read-only row.
            modes = list(READ_ONLY_MODES)
            modes_source = "read_only_probe"
        elif modes:
            modes_source = "measured"
        else:
            modes_source = "unproven"
        result.append(public_view({
            "state": state,
            "measured": True,
            "modes": modes,
            "modes_source": modes_source,
            "read_only_probe": bool(row.get("read_only_probe") is True),
            "identity": evidence_identity(
                executor=identity.get("executor") or DEFAULT_EXECUTOR,
                model=model_name,
                preset=identity.get("agent_preset") or DEFAULT_PRESET,
                version=resolve_version(explicit=identity.get("version")),
                model_version=identity.get("model_version"),
                endpoint=_pick(identity, "endpoint", None),
                preset_revision=_pick(identity, "preset_revision", None)),
            "tested_at": tested.isoformat(),
            "executor_version": _text(row.get("executor_version"), 120) or None,
            "test_id": _text(row.get("test_id"), 80) or None,
            "source": _text(row.get("source"), 120) or None,
        }))
    return result


def public_block(model, executor=DEFAULT_EXECUTOR, identity=None):
    """The capability block attached to a model in every public payload."""
    decision = evaluate(model, executor=executor, identity=identity)
    block = model.get("subagent") if isinstance(model, dict) and isinstance(model.get("subagent"), dict) else {}
    return {
        "policy": decision["policy"],
        "decision": decision["decision"],
        "state": decision["state"],
        "executor": decision["executor"],
        "identity": decision["identity"],
        "stale": decision["stale"],
        "expired": decision["expired"],
        "local": decision["local"],
        "mode": decision.get("mode"),
        "proven_modes": decision.get("proven_modes") or [],
        "reasons": decision["reasons"],
        "evidence_state": (decision.get("evidence") or {}).get("state"),
        "tested_at": (decision.get("evidence") or {}).get("tested_at"),
        "digest": decision.get("digest"),
        "updated_at": block.get("updated_at"),
    }


def capabilities_view(models, executor=DEFAULT_EXECUTOR, identity=None, engine=None):
    """Read-only panel payload: the policy decision for every configured model.

    ``identity`` pins the executor/version for the whole view.  The model and the
    Agent preset always come from the row under evaluation: letting an aggregate
    identity's defaults win would compare a per-model grant against the wrong
    preset (the panel used to show ``native`` for every row and disagree with the
    worker, which evaluates the row's real preset).
    """
    base = identity if isinstance(identity, dict) else {}
    rows = []
    for model in models if isinstance(models, (list, tuple)) else []:
        if not isinstance(model, dict):
            continue
        row_identity = None
        if base:
            row_identity = {
                "executor": base.get("executor") or executor,
                "model": model.get("model") or base.get("model") or "",
                "agent_preset": model.get("agent_preset") or base.get("agent_preset") or DEFAULT_PRESET,
                "version": base.get("version"),
                "model_version": model.get("model_version") or base.get("model_version"),
                "endpoint": _pick(base, "endpoint", model.get("api_base")),
                "preset_revision": _pick(base, "preset_revision", model.get("preset_revision")),
            }
        public = public_block(model, executor=executor, identity=row_identity)
        public["model_id"] = model.get("id")
        public["model"] = model.get("model")
        public["provider"] = model.get("provider")
        public["agent_preset"] = model.get("agent_preset") or DEFAULT_PRESET
        public["enabled"] = bool(model.get("enabled", True))
        public["env"] = executor
        rows.append(public)
    allowed = [row["model_id"] for row in rows if row["decision"] == VERDICT_ALLOW]
    return public_view({
        "schema": CAPABILITY_SCHEMA,
        "executor": executor,
        "identity": identity or evidence_identity(executor=executor),
        "engine": engine,
        "default_verdict": VERDICT_DENY,
        "allowed_model_ids": allowed,
        "models": rows,
    })
