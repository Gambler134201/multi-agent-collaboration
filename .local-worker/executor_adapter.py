"""Executor capability adaptation: one public interface, thin DSH implementation.

D1 goal: give the panel and the worker a single, honest way to ask *what can
this executor actually do?* instead of inferring ability from a tool name.

Scope boundaries that this module deliberately keeps:

* There is no second supervisor and no engine replacement.  The DSH path still
  runs the pinned ``acpx`` runtime exactly as before; this module only names the
  capabilities and gates one of them (subagents).
* Every capability state is one of ``supported`` / ``unsupported`` /
  ``unverified``.  ``supported`` is only returned for a capability whose real
  transport call exists in this repository and whose runtime evidence was
  measured.  Everything else fails closed.
* Public payloads never contain keys, tokens or raw logs.

The capability identifiers follow the D1 acceptance list:
``dispatch`` / ``session`` / ``discover`` / ``events`` / ``cancel`` / ``outcome``
/ ``usage`` / ``subagent``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import model_capabilities
import runtime_paths

HERE = Path(__file__).resolve().parent

CAPABILITY_KEYS = ("dispatch", "session", "discover", "events", "cancel", "outcome", "usage", "subagent")

STATE_SUPPORTED = model_capabilities.STATE_SUPPORTED
STATE_UNSUPPORTED = model_capabilities.STATE_UNSUPPORTED
STATE_UNVERIFIED = model_capabilities.STATE_UNVERIFIED

DEFAULT_STATE = STATE_UNSUPPORTED
UNKNOWN_ENGINE_STATE = STATE_UNSUPPORTED

# How this adapter exposes each engine capability.  Declaring an engine
# capability ``supported`` is a statement about the executor chain; the binding
# says which object actually performs the call.  Keeping the two apart is what
# stops "declared supported" from hiding a bare NotImplementedError:
#   direct      - this adapter performs a real, offline-safe operation
#   delegated   - the worker owns process execution; the adapter forwards when a
#                 transport is bound and otherwise refuses with the delegate name
#   gated       - a decision, not a transport operation (subagent)
#   unavailable - this executor has no binding at all
BINDING_DIRECT = "direct"
BINDING_DELEGATED = "delegated"
BINDING_GATED = "gated"
BINDING_UNAVAILABLE = "unavailable"

# One DSH location for the whole product: the same environment variable the
# worker reads (and the same PATH fallback) decides both the install identity
# below and the runtime the worker spawns, so capability evidence can never be
# measured against a different DSH than the one that actually runs.
DSH_EXECUTABLE_ENV = runtime_paths.DSH_EXE_ENV
DSH_RUNTIME_MANIFEST = "dsh/desktop-runtime.json"
DSH_ELECTRON_VERSION_FILE = "version"

# Frozen declaration of what each engine currently implements.  DSH is the only
# engine wired to a real transport in this repository; the others answer
# "unsupported" instead of pretending.  ``subagent`` is never ``supported`` here:
# the decision layer adds the per-model evidence and the user policy on top.
ENGINE_METHODS = {
    "dsh": {
        "dispatch": STATE_SUPPORTED,
        "session": STATE_SUPPORTED,
        "discover": STATE_SUPPORTED,
        "events": STATE_SUPPORTED,
        "cancel": STATE_SUPPORTED,
        "outcome": STATE_SUPPORTED,
        "usage": STATE_SUPPORTED,
        "subagent": STATE_UNVERIFIED,
    },
    "aider": {key: STATE_UNSUPPORTED for key in CAPABILITY_KEYS},
    "direct": {key: STATE_UNSUPPORTED for key in CAPABILITY_KEYS},
}

ENGINE_BINDINGS = {
    "dsh": {
        "dispatch": BINDING_DELEGATED,
        "session": BINDING_DELEGATED,
        "discover": BINDING_DIRECT,
        "events": BINDING_DIRECT,
        "cancel": BINDING_DIRECT,
        "outcome": BINDING_DIRECT,
        "usage": BINDING_DIRECT,
        "subagent": BINDING_GATED,
    },
    "aider": {key: BINDING_UNAVAILABLE for key in CAPABILITY_KEYS},
    "direct": {key: BINDING_UNAVAILABLE for key in CAPABILITY_KEYS},
}

# The worker entrypoint each delegated capability forwards to.  These are real
# attributes of dsh_worker; the tests assert that, so a delegate cannot drift
# into a name that does not exist.
ENGINE_DELEGATES = {
    "dsh": {
        "dispatch": "dsh_worker.run_task",
        "session": "dsh_worker._run_task",
    },
}

ENGINE_NOTES = {
    "dsh": {
        "dispatch": "delegated to dsh_worker.run_task -> acpx startTurn with the relay-backed provider patch",
        "session": "delegated to dsh_worker._run_task -> acpx ensureSession persistent + session/resume on the same session key",
        "discover": "adapter reads its own frozen capability table; install identity comes from the read-only ASAR probe",
        "events": "dsh_worker streams turn.events live; the adapter reads the recorded task.json transcript (no live start)",
        "cancel": "adapter writes the same cancelled.flag run-dsh.mjs polls, then turn.cancel({reason})",
        "outcome": "adapter reads result.json plus the executor-completion record (acceptance stays with the controller)",
        "usage": "adapter reads budget_relay request records; response usage comes from the upstream provider, never estimated",
        "subagent": "DSH ships the subagent plugins and publishes observe-only subagent/start + subagent/end lifecycle edges; the controlled probe mounts one in-process spawn child pinned to the parent route and records those edges through the executor-side bridge, while per-child token billing stays unprovable and is reported as aggregate only",
    },
    "aider": {key: "执行器未接入能力接口" for key in CAPABILITY_KEYS},
    "direct": {key: "执行器未接入能力接口" for key in CAPABILITY_KEYS},
}

# Pi uses the same controller records/accounting with its own RPC transport.
ENGINE_METHODS['pi'] = {**ENGINE_METHODS['dsh'], 'subagent': STATE_UNSUPPORTED}
ENGINE_BINDINGS['pi'] = dict(ENGINE_BINDINGS['dsh'])
ENGINE_DELEGATES['pi'] = {'dispatch': 'pi_worker.run_task', 'session': 'pi_worker._run_task'}
ENGINE_NOTES['pi'] = {
    'dispatch': 'pi_worker.run_task -> isolated Pi RPC prompt through the accounting relay',
    'session': 'Pi --session resumes the recorded same-project session; get_state verifies exact identity',
    'discover': 'installed Pi 1.3.0 package identity and explicit model/provider verified before prompt',
    'events': 'Pi RPC JSONL events recorded by the shared controller',
    'cancel': 'run-pi.mjs polls cancelled.flag, clears queued prompts, then aborts',
    'outcome': 'agent_settled plus final assistant stop result; executor completion still requires independent acceptance',
    'usage': ENGINE_NOTES['dsh']['usage'],
    'subagent': 'Pi child agents and discovered extensions are disabled; DSH grants never apply',
}
IMPLEMENTED_ENGINES = ("dsh", "pi")

# The event contract the E2 UI can consume.  It is deliberately described here
# instead of being inferred from the model's answer text: the executor-side
# bridge (agent-runtime/subagent-bridge.cjs) writes one line per DSH lifecycle
# edge, and `subagent_runtime` pairs them into child lineage.
SUBAGENT_LIFECYCLE_SCHEMA = {
    "schema": 1,
    "source": "dsh-subagent-lifecycle",
    "events": ["subagent/start", "subagent/end"],
    "ledger": "JSONL: {schema, source, type, at, provider, child_id, run_id, local, child_model?, stop_reason?, output_chars?, output_preview?}",
    "identity_fields": ["provider", "child_id", "run_id", "local"],
    "terminal_fields": ["stop_reason", "output_chars", "output_preview"],
    "transport": "DSH plugin mounted by the worker patch (agent-runtime/subagent-bridge.cjs), observe-only",
    "limitations": [
        "lifecycle edges are observe-only; they are not a result or usage API",
        "per-child token billing is not exposed by DSH and is reported as aggregate only",
    ],
}


def _subagent_lifecycle_schema():
    return model_capabilities.public_view(SUBAGENT_LIFECYCLE_SCHEMA)


# ------------------------------------------------------------------- identity

_IDENTITY_CACHE = {}


def executor_path():
    """The configured DSH executable, or the ``UNSET`` marker when unconfigured.

    No default install location is invented: an unconfigured machine reports an
    unreadable identity (fail-closed ``unversioned``) instead of pretending a
    specific drive or user directory holds DSH.
    """
    return runtime_paths.dsh_executable() or runtime_paths.UNSET


def _read_text(path, limit=1_000_000):
    try:
        path = Path(path)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return None


def _asar_version(executable):
    """Release version recorded in the packaged DSH runtime manifest.

    The ASAR header is a small pickle followed by a JSON file table; the
    manifest itself is extracted lazily the same way the runtime already reads
    preset declarations.  When the file is absent we report nothing rather than
    making a version up.
    """
    archive = Path(executable).parent / "resources" / "app.asar"
    if not archive.is_file():
        return None
    cached = _IDENTITY_CACHE.get(str(archive))
    if cached is not None:
        return cached or None
    version = None
    try:
        with archive.open("rb") as handle:
            header = handle.read(16)
            if len(header) == 16:
                header_size = int.from_bytes(header[12:16], "little")
                if 16 < header_size < 64 * 1024 * 1024:
                    table = handle.read(header_size)
                    tree = json.loads(table.decode("utf-8", "replace"))
                    node = tree
                    for part in DSH_RUNTIME_MANIFEST.split("/"):
                        node = (node or {}).get("files", {}).get(part)
                    if isinstance(node, dict) and node.get("size"):
                        handle.seek(16 + header_size + int(node["offset"]))
                        manifest = json.loads(handle.read(int(node["size"])).decode("utf-8", "replace"))
                        version = str((manifest.get("release") or {}).get("version") or "") or None
    except (OSError, ValueError, KeyError, TypeError):
        version = None
    _IDENTITY_CACHE[str(archive)] = version or ""
    return version


def installed_executor_version(executable=None):
    """Release version of the installed DSH, or ``None`` when unreadable.

    The ASAR table is parsed once and cached, so both the public capability view
    and the worker can call this without repeating expensive I/O.
    """
    exe = Path(executable) if executable else executor_path()
    return _asar_version(exe)


# The panel and the worker must bind evidence to the same installed version, so
# the capability layer registers this single resolver instead of one side
# guessing ``unversioned-v1``.
model_capabilities.register_version_resolver(installed_executor_version)


def preset_content_revision(preset, executable=None):
    """Content revision of one Agent preset as actually mounted, or ``None``.

    Imported lazily so this module stays import-light: ``preset_runtime`` is the
    only place that knows how to read the installed ASAR table and the user's
    native bundle, and the panel and the worker both route through here.  A
    failure returns ``None`` so the identity decays to the fail-closed
    ``preset-unresolved-v1`` placeholder instead of matching any preset.
    """
    try:
        import preset_runtime
        # Read the preset out of the same configured install the worker runs.
        # preset_runtime's own module-level default must not decide which DSH the
        # capability evidence was measured against; unconfigured means unknown.
        target = Path(executable) if executable else runtime_paths.dsh_executable()
        return None if target is None else preset_runtime.content_revision(preset, target)
    except Exception:
        return None


model_capabilities.register_preset_resolver(preset_content_revision)


def _fingerprint(path, limit=32 * 1024 * 1024):
    """sha256 when affordable, otherwise a size+mtime tag.

    The DSH executable is hundreds of megabytes; hashing it on every panel
    request would be wasteful, so the cheap tag is used and the full digest is
    kept for small files and tests.
    """
    try:
        path = Path(path)
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size <= limit:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        return f"size:{size}:mtime:{path.stat().st_mtime_ns}"
    except OSError:
        return None


def dsh_identity(version=None, model=None, preset=None, model_version=None, executable=None,
                 endpoint=None, preset_revision=None):
    """Executable/install identity for DSH, without touching user secrets.

    D2 adds the route/endpoint and the Agent-preset *content* revision, so a
    grant cannot be replayed after the upstream route or the mounted preset
    changed while the release version stayed the same.
    """
    executable = Path(executable) if executable else executor_path()
    version = version or _asar_version(executable)
    electron = _read_text(executable.parent / DSH_ELECTRON_VERSION_FILE, 64)
    revision = preset_revision or model_capabilities.resolved_preset_revision(preset)
    return {
        "executor": "dsh",
        "version": version or model_capabilities.UNVERSIONED,
        "version_source": ("app.asar:" + DSH_RUNTIME_MANIFEST) if version else "unavailable",
        "electron": (electron or "").strip() or None,
        "executable": str(executable),
        "executable_fingerprint": _fingerprint(executable),
        "model": str(model or ""),
        "agent_preset": str(preset or model_capabilities.DEFAULT_PRESET),
        "model_version": str(model_version or model_capabilities.UNVERSIONED),
        "endpoint": str(endpoint or ""),
        "preset_revision": revision,
    }


def model_identity(model, version=None, executable=None, preset=None):
    """Identity tuple (route + model + preset content) for one row."""
    model = model if isinstance(model, dict) else {}
    revision = preset.get("revision") if isinstance(preset, dict) else model.get("preset_revision")
    info = dsh_identity(version=version, model=model.get("model"), preset=model.get("agent_preset"),
                        model_version=model.get("model_version"), executable=executable,
                        endpoint=model.get("api_base"), preset_revision=revision)
    return model_capabilities.evidence_identity(
        executor="dsh", model=model.get("model") or "",
        preset=model.get("agent_preset") or model_capabilities.DEFAULT_PRESET,
        version=info["version"], model_version=info.get("model_version"),
        endpoint=info.get("endpoint"), preset_revision=info.get("preset_revision"))


# ---------------------------------------------------------------- capability

def engine_capabilities(engine="dsh"):
    """Capability map, binding map and honest notes for one executor name.

    ``methods`` states what the executor chain supports; ``bindings`` states how
    this adapter exposes it, so a ``supported`` declaration can never hide a
    bare ``NotImplementedError``: every ``delegated`` entry names its real worker
    entrypoint in ``delegates``.  ``implemented`` means this repository wires a
    real transport for the engine; only DSH qualifies today.  ``known`` merely
    means the name is recognised in the frozen declaration table so callers can
    tell "aider, not implemented" from "typo, does not exist".
    """
    name = str(engine or "dsh").strip().lower()
    known = name in ENGINE_METHODS
    if known:
        methods = dict(ENGINE_METHODS[name])
        notes = dict(ENGINE_NOTES.get(name, {}))
        bindings = dict(ENGINE_BINDINGS.get(name, {}))
        delegates = dict(ENGINE_DELEGATES.get(name, {}))
    else:
        methods = {key: UNKNOWN_ENGINE_STATE for key in CAPABILITY_KEYS}
        notes = {key: "未知执行器，明确未实现" for key in CAPABILITY_KEYS}
        bindings = {key: BINDING_UNAVAILABLE for key in CAPABILITY_KEYS}
        delegates = {}
    bindings = {key: bindings.get(key, BINDING_UNAVAILABLE) for key in CAPABILITY_KEYS}
    return {"engine": name, "known": known, "implemented": name in IMPLEMENTED_ENGINES,
            "methods": methods, "bindings": bindings, "delegates": delegates, "notes": notes}


def executor_report(engine="dsh", model=None, version=None, executable=None, identity=None):
    """Public, secret-free report for one executor and (optionally) one model."""
    engine = str(engine or "dsh").strip().lower()
    base = engine_capabilities(engine)
    report = {
        "schema": model_capabilities.CAPABILITY_SCHEMA,
        "engine": engine,
        "implemented": base["implemented"],
        "capabilities": model_capabilities.public_view(base["methods"]),
        "bindings": model_capabilities.public_view(base["bindings"]),
        "delegates": model_capabilities.public_view(base["delegates"]),
        "notes": model_capabilities.public_view(base["notes"]),
        "subagent_ready": False,
        "subagent": None,
        "subagent_lifecycle_schema": _subagent_lifecycle_schema(),
        "identity": None,
        "evidence_command": "python .local-worker/tests/test_executor_adapter.py",
        "probe_command": "python .local-worker/subagent_runtime.py --probe --model-id <registered-non-local-model-id> --evidence .local-worker/tasks/subagent-probe-evidence.json",
    }
    if engine == 'pi':
        import pi_worker
        info = pi_worker.availability()
        report['identity'] = {'executor': 'pi', 'version': info['version'], 'executable': info['executable']}
        report['reason'] = 'Pi 已接入；额外扩展及子代理未启用'
        report['subagent_lifecycle_schema'] = None
        report['probe_command'] = None
        report['evidence_command'] = 'python .local-worker/tests/test_pi_runtime.py'
        return model_capabilities.public_view(report)
    if engine != "dsh":
        report["reason"] = "该执行器尚未实现能力接口，子代理一律禁止"
        return model_capabilities.public_view(report)
    report["identity"] = model_capabilities.public_view(dsh_identity(
        version=version, executable=executable,
        model=(model or {}).get("model") if isinstance(model, dict) else None,
        preset=(model or {}).get("agent_preset") if isinstance(model, dict) else None,
        endpoint=(model or {}).get("api_base") if isinstance(model, dict) else None))
    if isinstance(model, dict):
        decision = model_capabilities.evaluate(
            model, executor="dsh", identity=identity or model_identity(model, version=version, executable=executable))
        report["subagent"] = decision
        report["subagent_ready"] = decision["decision"] == model_capabilities.VERDICT_ALLOW
    return model_capabilities.public_view(report)


def capabilities_view(models, engine="dsh", version=None, executable=None):
    """Aggregate read-only view used by the panel endpoint.

    The base identity carries only fields that are shared by every row (engine
    version); the route endpoint and the preset content revision are resolved
    per row, so two models on different endpoints or presets never share a
    grant by accident.
    """
    engine = str(engine or "dsh").strip().lower()
    identity = None
    if engine == "dsh":
        base = dsh_identity(version=version, executable=executable)
        identity = {key: value for key, value in base.items()
                    if key not in {"endpoint", "preset_revision", "model", "agent_preset"}}
        identity["agent_preset"] = None
    rows = model_capabilities.capabilities_view(models, executor=engine, identity=identity,
                                                engine=engine_capabilities(engine))
    return model_capabilities.public_view(rows)


# ------------------------------------------------------------- subagent gate

class SubagentDenied(PermissionError):
    """Raised when delegation is not proven allowed for this model/executor."""


def subagent_gate(model, engine="dsh", version=None, executable=None, identity=None, preset=None, mode=None):
    """Single enforcement point for starting subagents.

    Returns the decision when delegation is allowed; raises
    :class:`SubagentDenied` otherwise.  The worker calls this before touching
    any DSH subagent plugin, so an unverified or denied model can never spawn a
    child by accident.  ``preset`` (the loaded declaration) pins the preset
    content revision into the identity, so a changed preset invalidates the
    grant instead of reusing it.  ``mode`` is the requested task mode; the
    stored evidence must have measured it, so a read-only ``chat`` probe cannot
    authorise full-access ``edit`` delegation.
    """
    engine = str(engine or "dsh").strip().lower()
    if engine != "dsh":
        raise SubagentDenied(f"执行器 {engine} 未实现子代理能力接口")
    model = model if isinstance(model, dict) else {}
    decision = model_capabilities.evaluate(
        model, executor="dsh", mode=mode,
        identity=identity or model_identity(model, version=version, executable=executable, preset=preset))
    if decision["decision"] != model_capabilities.VERDICT_ALLOW:
        reason = "；".join(decision.get("reasons") or ["未证实"])
        raise SubagentDenied(f"子代理未获允许：{reason}")
    return decision


def subagent_enabled(model, **kwargs):
    """Boolean helper for callers that only need to disable plugin rows."""
    try:
        subagent_gate(model, **kwargs)
        return True
    except SubagentDenied:
        return False


# --------------------------------------------------------------- adapter API

class CapabilityNotWired(RuntimeError):
    """The engine chain provides the capability, but this adapter has no delegate.

    Deliberately *not* a bare ``NotImplementedError``: the executor does
    implement the operation (``dsh_worker`` owns it).  The error carries the
    exact delegate name so a caller can bind a transport instead of guessing,
    and it is raised *before* anything that could dispatch paid work.
    """

    def __init__(self, capability, delegate=None, engine="dsh"):
        self.capability = capability
        self.delegate = delegate
        self.engine = engine
        detail = f"，由 {delegate} 执行" if delegate else ""
        super().__init__(f"执行器 {engine} 的 {capability} 能力未绑定传输委托{detail}")


class ExecutorCapabilityAdapter:
    """Uniform capability interface for the executor currently in use.

    The methods describe *intent* (dispatch a turn, stream events, cancel, read
    the outcome, read usage, ask about subagents) instead of exposing DSH
    objects.  The worker owns process execution; a caller that wants the unified
    interface binds ``transport={'dispatch': dsh_worker.run_task, ...}``.  Every
    operation either performs real offline-safe work (reads / the cancel flag),
    forwards exactly once to a bound delegate, or refuses with
    :class:`CapabilityNotWired` while naming the delegate it needs.  No method
    ever retries itself, so an error can never cause a second paid dispatch.
    """

    name = "base"
    DELEGATES = {}

    def __init__(self, engine="dsh", model=None, version=None, executable=None, transport=None):
        self.engine_name = str(engine or "dsh").strip().lower()
        self.model = model if isinstance(model, dict) else {}
        self.version = version
        self.executable = executable
        self._transport = dict(transport) if isinstance(transport, dict) else None

    # -- capability discovery ------------------------------------------------
    def capabilities(self):
        base = engine_capabilities(self.engine_name)
        ready = {}
        for key in CAPABILITY_KEYS:
            binding = base["bindings"].get(key)
            if binding == BINDING_DIRECT:
                ready[key] = True
            elif binding == BINDING_DELEGATED:
                ready[key] = self.binds(key)
            else:
                ready[key] = False
        return {**base, "transport_bound": bool(self._transport), "ready": ready,
                "transport_keys": sorted(self._transport) if self._transport else []}

    def binds(self, capability):
        """True when a callable delegate is bound for this capability."""
        return bool(self._transport) and callable(self._transport.get(capability))

    def _handler(self, capability):
        if self._transport:
            handler = self._transport.get(capability)
            if callable(handler):
                return handler
        return None

    def _delegate(self, capability, **kwargs):
        handler = self._handler(capability)
        if handler is not None:
            return handler(**kwargs)
        raise CapabilityNotWired(capability, self.DELEGATES.get(capability), self.engine_name)

    def identity(self):
        if self.engine_name == 'pi':
            return executor_report('pi')['identity']
        if self.engine_name != "dsh":
            return {"executor": self.engine_name, "version": model_capabilities.UNVERSIONED}
        return dsh_identity(version=self.version, executable=self.executable,
                            model=self.model.get("model"), preset=self.model.get("agent_preset"),
                            model_version=self.model.get("model_version"))

    def report(self):
        return executor_report(self.engine_name, model=self.model, version=self.version, executable=self.executable)

    def supports(self, capability):
        """Engine-level support (the chain provides the capability)."""
        return self.capabilities()["methods"].get(capability) == STATE_SUPPORTED

    def callable_now(self, capability):
        """Adapter-level readiness: direct, or delegated with a bound transport."""
        return bool(self.capabilities()["ready"].get(capability))

    # -- operations ----------------------------------------------------------
    def dispatch(self, **kwargs):
        raise NotImplementedError(f"执行器 {self.engine_name} 未实现派发接口")

    def new_session(self, **kwargs):
        raise NotImplementedError(f"执行器 {self.engine_name} 未实现新会话接口")

    def stream_events(self, **kwargs):
        raise NotImplementedError(f"执行器 {self.engine_name} 未实现事件流接口")

    def cancel(self, **kwargs):
        raise NotImplementedError(f"执行器 {self.engine_name} 未实现取消接口")

    def outcome(self, **kwargs):
        raise NotImplementedError(f"执行器 {self.engine_name} 未实现成果读取接口")

    def usage(self, **kwargs):
        raise NotImplementedError(f"执行器 {self.engine_name} 未实现 usage 读取接口")

    def subagent(self):
        return subagent_gate(self.model, engine=self.engine_name, version=self.version, executable=self.executable)


class DshCapabilityAdapter(ExecutorCapabilityAdapter):
    """Thin adapter over the existing acpx-based DSH worker.

    There is exactly one execution engine: ``dispatch``/``new_session`` forward
    to the worker's real entrypoints through a bound transport (and refuse with
    :class:`CapabilityNotWired` naming that entrypoint when none is bound), while
    ``events``/``cancel``/``outcome``/``usage`` do real offline-safe work here
    (read the recorded transcript/outcome/usage, write the same cancel flag the
    runtime polls).  Each declared transport is asserted against
    ``agent-runtime/run-dsh.mjs`` by the offline tests.
    """

    name = "dsh"

    TRANSPORT = {
        "dispatch": "runtime.startTurn({handle, text, mode:'prompt', requestId, timeoutMs})",
        "session": "runtime.ensureSession({sessionKey, agent:'dsh', mode:'persistent', cwd})",
        "discover": "runtime.getStatus({handle}).models.availableModelIds",
        "events": "for await (const event of turn.events)",
        "cancel": "turn.cancel({reason})",
        "outcome": "await turn.result",
        "usage": "budget_relay request records (upstream usage, not estimated)",
    }

    DELEGATES = ENGINE_DELEGATES["dsh"]

    def dispatch(self, **kwargs):
        return self._delegate("dispatch", **kwargs)

    def new_session(self, **kwargs):
        return self._delegate("session", **kwargs)

    def stream_events(self, run=None, **kwargs):
        handler = self._handler("events")
        if handler is not None:
            return handler(run=run, **kwargs)
        return read_events(run)

    def cancel(self, run=None, **kwargs):
        handler = self._handler("cancel")
        if handler is not None:
            return handler(run=run, **kwargs)
        return request_cancel(run, reason=kwargs.get("reason"))

    def outcome(self, run=None, **kwargs):
        handler = self._handler("outcome")
        if handler is not None:
            return handler(run=run, **kwargs)
        return read_outcome(run)

    def usage(self, run=None, record=None, **kwargs):
        handler = self._handler("usage")
        if handler is not None:
            return handler(run=run, record=record, **kwargs)
        return read_usage(run, record)


class PiCapabilityAdapter(DshCapabilityAdapter):
    """Shared record operations, separate Pi RPC dispatch/session bindings."""
    name = 'pi'
    DELEGATES = ENGINE_DELEGATES['pi']
    TRANSPORT = {'dispatch': 'RPC prompt', 'session': 'Pi --session + RPC get_state',
                 'discover': 'RPC get_state exact provider/model/baseUrl',
                 'events': 'RPC JSONL events', 'cancel': 'RPC clear_queue then abort',
                 'outcome': 'agent_settled + assistant stopReason', 'usage': 'budget_relay upstream usage'}


def resolve(engine="dsh"):
    """Return the adapter class for an engine name without instantiating it."""
    name = str(engine or "dsh").strip().lower()
    return {'dsh': DshCapabilityAdapter, 'pi': PiCapabilityAdapter}.get(name, ExecutorCapabilityAdapter)


def adapter_for(engine="dsh", model=None, version=None, executable=None, transport=None):
    name = str(engine or "dsh").strip().lower()
    return resolve(name)(name, model=model, version=version, executable=executable, transport=transport)


# ------------------------------------------------------- result/usage reading

def request_cancel(run, reason=""):
    """Thin delegate to the exact cancel mechanism the runtime already polls.

    Writes only ``cancelled.flag`` inside an existing run directory.  It never
    starts a turn, never retries and returns ``redispatched: False`` so a caller
    cannot mistake a cancellation for a new paid dispatch.
    """
    if not run:
        raise ValueError("cancel 需要已存在的 run 目录")
    run = Path(run)
    if not run.is_dir():
        raise ValueError(f"run 目录不存在：{run}")
    flag = run / "cancelled.flag"
    flag.write_text(json.dumps({"reason": str(reason or "")[:200], "by": "executor_adapter",
                                "redispatched": False}, ensure_ascii=False), encoding="utf-8")
    try:
        is_pi = json.loads((run / 'task.json').read_text(encoding='utf-8')).get('engine') == 'pi'
    except (OSError, ValueError):
        is_pi = False
    return {"cancelled": True, "cancelled_flag": str(flag), "redispatched": False,
            "mechanism": ("run-pi.mjs polls cancelled.flag then clear_queue + abort" if is_pi else
                          "run-dsh.mjs polls cancelled.flag then calls turn.cancel({reason})")}


def read_events(run):
    """Read the recorded turn transcript for one run; never starts or re-runs it."""
    if not run:
        return None
    run = Path(run)
    path = run / "task.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    events = []
    if data.get("status") or data.get("phase"):
        events.append({"type": "phase", "status": data.get("status"), "phase": data.get("phase")})
    for tool in (data.get("tool_calls") or [])[-50:]:
        if isinstance(tool, dict):
            events.append({"type": "tool", "title": tool.get("title"),
                           "status": tool.get("status"), "file": tool.get("file")})
    # D2: parent/child association comes from the executor-side lifecycle
    # ledger, never from parsing the answer text.
    subagent_events = data.get("subagent_events")
    for row in subagent_events if isinstance(subagent_events, list) else []:
        if isinstance(row, dict):
            events.append({"type": "subagent", "schema": row.get("schema"), "source": row.get("source"),
                           "event": row.get("type"), "child_id": row.get("child_id"),
                           "run_id": row.get("run_id"), "provider": row.get("provider"),
                           "stop_reason": row.get("stop_reason"), "at": row.get("at")})
    answer = data.get("answer")
    if answer:
        events.append({"type": "text", "text": str(answer)[-2000:]})
    if data.get("ended_at"):
        events.append({"type": "result", "status": data.get("status"), "ended_at": data.get("ended_at")})
    return model_capabilities.public_view({
        "source": f"{data.get('engine') or 'dsh'}_worker task.json recorded transcript",
        "run": str(run), "status": data.get("status"), "events": events,
        "subagent_schema": (subagent_events[0].get("schema") if subagent_events else None),
    })


def read_outcome(run):
    """Read the recorded executor outcome; never re-runs or charges anything."""
    if not run:
        return None
    run = Path(run)
    path = run / "result.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return model_capabilities.public_view({
        "task_id": data.get("task_id"),
        "status": data.get("status"),
        "engine": data.get("engine"),
        "model": data.get("model"),
        "changed_files": data.get("changed_files") or [],
        "summary": (data.get("summary") or "")[-2000:],
        "test": data.get("test"),
        "usage": read_usage(run),
    })


def read_usage(run, record=None):
    """Usage facts recorded for one run.

    Numbers always come from the upstream response usage (via the relay) and are
    settled with the unit price that applied when the request was sent.  When no
    record exists the answer is ``unknown`` rather than an estimate, so a caller
    can never treat a guess as a billable fact.  ACP ``used``/``size`` context
    occupancy is never used as a bill.
    """
    facts = {"source": "budget-relay/upstream-usage", "requests": 0, "prompt_tokens": 0,
             "completion_tokens": 0, "total_tokens": 0, "estimated": False, "unknown": True,
             "settled_cny": None, "unknown_calls": 0, "settled_calls": 0, "per_child": "unavailable"}
    rows = record if isinstance(record, list) else None
    if rows is None and run:
        path = Path(run) / "gateway-usage.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rows = data if isinstance(data, list) else data.get("requests")
            except (OSError, ValueError):
                rows = None
        if not isinstance(rows, list):
            rows = task_cost_rows(run)
            if rows is not None:
                facts["source"] = "budget-relay/task.json cost_requests (upstream usage, request-time rates)"
    if not isinstance(rows, list):
        return model_capabilities.public_view(_subagent_usage(facts, run))
    settled = 0.0
    has_settled = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else row
        facts["requests"] += 1
        for source, target in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
                               ("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                               ("total_tokens", "total_tokens")):
            value = usage.get(source)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                facts[target] += int(value)
        if usage.get("estimated") is True:
            facts["estimated"] = True
        state = row.get("state")
        if state == "settled" and row.get("cost_cny") is not None:
            try:
                settled += float(row["cost_cny"])
                facts["settled_calls"] += 1
                has_settled = True
            except (TypeError, ValueError):
                facts["unknown_calls"] += 1
        elif state in {"unknown", "pending"} or row.get("billable", True):
            facts["unknown_calls"] += 1
    if facts["requests"]:
        facts["unknown"] = False
    if not facts["total_tokens"]:
        facts["total_tokens"] = facts["prompt_tokens"] + facts["completion_tokens"]
    if has_settled:
        facts["settled_cny"] = round(settled, 8)
    return model_capabilities.public_view(_subagent_usage(facts, run))


def task_cost_rows(run):
    """The relay's per-request records for one run, or ``None`` when absent."""
    if not run:
        return None
    path = Path(run) / "task.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = data.get("cost_requests") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return None
    return [row for row in rows.values() if isinstance(row, dict)]


def _subagent_usage(facts, run):
    """Attach the child attribution the task recorded, without inventing any."""
    if not run:
        return facts
    path = Path(run) / "task.json"
    if not path.is_file():
        return facts
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return facts
    block = data.get("subagent_usage") if isinstance(data, dict) else None
    if isinstance(block, dict):
        facts["subagent"] = block
        facts["per_child"] = block.get("per_child", "unavailable")
    return facts
