"""Shared persistent model registry and task records for the local panel."""
import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import math
import msvcrt
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse
from uuid import uuid4

import model_capabilities
import project_storage

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "models.json"
RUNS = HERE / "runs"


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.04)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    """Bounded local retries for transient Windows sharing violations."""
    for attempt in range(6):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(.04)


def read_task(run):
    run = Path(run)
    # A stale detail/reference request must not recreate a permanently deleted
    # run directory merely to open its lock file.
    if not (run / 'task.json').is_file():
        raise FileNotFoundError('任务记录已删除或不存在')
    with file_lock(run / 'task.lock'):
        return read_json(run / 'task.json')


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 10
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("模型配置正在保存，请稍后重试")
                time.sleep(.05)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def registry_lock():
    return file_lock(HERE / "models.lock")


def protect(text, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    data = base64.b64decode(text) if decrypt else text.encode("utf-8")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    output = Blob()
    method = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError()
    try:
        result = ctypes.string_at(output.data, output.size)
        return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def read_registry():
    if not REGISTRY.exists():
        legacy_path = HERE / "settings.json"
        if legacy_path.is_file():
            # A pre-registry installation keeps its single connection; the import
            # runs once and never touches an existing models.json.
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            row = {**legacy, "id": "home", "name": "家里的 Qwen", "provider": "local",
                   "enabled": True, "bypass_proxy": True, "secret": "", "last_check": None,
                   "created_at": now()}
            write_json(REGISTRY, {"default_model": "home", "models": [row]})
        else:
            # A fresh installation ships no model, no key and no remote endpoint.
            # The empty registry is only returned, never written, so starting the
            # service or listing models cannot create user configuration; the
            # model editor stores the first row when the user adds one.
            return {"default_model": None, "models": []}
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    data["models"] = [normalize_row(row) for row in data.get("models", [])]
    data.setdefault("default_model", None)
    return data


def normalize_row(row):
    """Fill defaults for rows saved before the subagent capability fields existed.

    The write is idempotent and only adds missing keys, so a legacy models.json
    keeps loading while every model starts out ``unverified`` and denied.
    """
    if not isinstance(row.get("subagent"), dict):
        row["subagent"] = model_capabilities.normalize_block(None, None)
    if "model_version" not in row:
        row["model_version"] = ""
    # Legacy rows inherit the global default without needing a migration.  An
    # explicitly invalid value is left untouched so the router can refuse it
    # instead of silently treating it as inheritance.
    if "default_agent" not in row or row.get("default_agent") is None:
        row["default_agent"] = "follow_global"
    return row


def capability_models():
    """Public-field rows that still carry the raw subagent block.

    The panel's aggregate capability view must evaluate the same evidence the
    worker does; :func:`public_model` deliberately strips the raw block, which
    previously made that view deny every model while the worker could allow one.
    The explicit whitelist below keeps secret keys out by construction.
    """
    with registry_lock():
        data = read_registry()
    rows = []
    for row in data.get("models", []):
        rows.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "provider": row.get("provider"),
            "api_base": row.get("api_base"),
            "model": row.get("model"),
            "agent_preset": row.get("agent_preset"),
            "default_agent": row.get("default_agent"),
            "enabled": bool(row.get("enabled", True)),
            "model_version": row.get("model_version"),
            "executor_version": row.get("executor_version"),
            "subagent": row.get("subagent") if isinstance(row.get("subagent"), dict) else {},
        })
    return rows


def current_capability_identity(row):
    """The same versioned identity the worker uses, so the panel agrees with it."""
    try:
        import executor_adapter
        return executor_adapter.model_identity(row if isinstance(row, dict) else {})
    except Exception:
        return None


def public_model(row):
    import model_agent_defaults
    row = normalize_row(dict(row))
    public = {k: v for k, v in row.items() if k not in {"secret", "api_key", "subagent"}}
    public["api_key_set"] = bool(row.get("secret") or os.environ.get(row.get("api_key_env", "")))
    try:
        public["default_agent_valid"] = model_agent_defaults.stored(row) in model_agent_defaults.VALUES
    except ValueError:
        public["default_agent_valid"] = False
    # Capability/policy decision is separated from the raw evidence so a public
    # payload never carries measurement internals or key material.  The identity
    # is resolved from the installation rather than defaulted to unversioned-v1,
    # so the panel and the worker cannot reach different verdicts.
    public["subagent"] = model_capabilities.public_block(row, identity=current_capability_identity(row))
    return public


def list_models():
    with registry_lock():
        data = read_registry()
    return {"default_model": data["default_model"], "models": [public_model(m) for m in data["models"]]}


def get_model(model_id=None, require_enabled=True):
    with registry_lock():
        data = read_registry()
    model_id = model_id or data["default_model"]
    row = next((dict(m) for m in data["models"] if m["id"] == model_id), None)
    if row is None:
        raise ValueError("请选择一个已配置的执行模型")
    if require_enabled and not row.get("enabled", True):
        raise ValueError("这个模型已停用")
    if row.get('provider') == 'workbuddy':
        if row.get('secret') or row.get('api_key_env'):
            raise ValueError('WorkBuddy 原生模型不能保存 API 密钥')
        row['api_key'] = ''
        return row
    row["api_key"] = protect(row["secret"], True) if row.get("secret") else os.environ.get(row.get("api_key_env", ""), "")
    return row


def upsert_model(body, model_id=None):
    if body.get('provider') == 'workbuddy':
        return upsert_native_model(body, model_id)
    name = str(body.get("name", "")).strip()
    api_base = str(body.get("api_base", "")).strip().rstrip("/")
    model = str(body.get("model", "")).strip()
    url = urlparse(api_base)
    if not name or len(name) > 100 or not model or len(model) > 200:
        raise ValueError("请填写模型名称和模型 ID")
    if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("接口地址需要是 http(s) Base URL，不能包含账号、查询参数或片段")
    provider = body.get("provider", "custom")
    if provider not in {"local", "deepseek", "glm", "custom"}:
        raise ValueError("不支持的服务类型")
    if not isinstance(body.get("enabled", True), bool) or not isinstance(body.get("bypass_proxy", False), bool):
        raise ValueError("开关参数必须是布尔值")
    limits = {}
    for field, default, upper in (("max_input_tokens", 32768, 1048576), ("max_output_tokens", 4096, 131072)):
        value = body.get(field, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value or not 64 <= value <= upper:
            raise ValueError("Token 预算需要是有效的正整数")
        limits[field] = int(value)
    env_name = str(body.get("api_key_env", "")).strip()
    if env_name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ValueError("环境变量名格式不正确")
    with registry_lock():
        data = read_registry()
        previous = next((m for m in data["models"] if m["id"] == model_id), None)
        if model_id and previous is None:
            raise ValueError("模型不存在")
        if previous and previous.get('provider') == 'workbuddy':
            raise ValueError('原生账户模型不能转换为 API 模型；请另行添加连接')
        model_id = model_id or uuid4().hex[:12]
        secret = previous.get("secret", "") if previous else ""
        if body.get("clear_key"):
            secret = ""
        elif body.get("api_key"):
            secret = protect(str(body["api_key"]))
        capabilities = body.get("preferred_tasks", previous.get("preferred_tasks", []) if previous else [])
        notes = str(body.get("capability_notes", previous.get("capability_notes", "") if previous else "")).strip()
        cost_preference = body.get("cost_preference", previous.get("cost_preference", "economy" if provider == "local" else "balanced") if previous else "economy" if provider == "local" else "balanced")
        if not isinstance(capabilities, list) or len(capabilities) > 12 or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 40 for item in capabilities):
            raise ValueError("适合的任务最多 12 项，每项 1 到 40 字")
        if len(notes) > 3000 or cost_preference not in {"economy", "balanced", "quality"}:
            raise ValueError("请检查模型能力说明和成本定位")
        complexity = body.get('max_task_complexity', previous.get('max_task_complexity', 'simple' if provider == 'local' else 'complex') if previous else 'simple' if provider == 'local' else 'complex')
        if complexity not in {'simple', 'medium', 'complex'}:
            raise ValueError('任务复杂度上限不正确')
        agent_preset = body.get('agent_preset', previous.get('agent_preset', 'native' if provider == 'local' else 'standard') if previous else 'native' if provider == 'local' else 'standard')
        if agent_preset not in {'native', 'standard'}:
            raise ValueError('请选择原生或标准 Agent 预设')
        import model_agent_defaults
        if 'default_agent' in body:
            # An explicitly supplied value must be one of the declared choices;
            # an empty/missing field is only legacy inheritance on stored rows.
            default_agent = model_agent_defaults.normalize_update(body['default_agent'])
        else:
            default_agent = model_agent_defaults.normalize(
                previous.get('default_agent') if previous else None)
        import execution_engines
        if default_agent != model_agent_defaults.FOLLOW_GLOBAL:
            execution_engines.validate_model(default_agent, {'provider': provider})
        import thinking_config
        thinking_level = body.get('thinking_level', previous.get('thinking_level', 'default') if previous else 'default')
        thinking_info = previous.get('thinking_info') if previous and previous['api_base'] == api_base and previous['model'] == model else None
        thinking_config.validate({'provider': provider, 'api_base': api_base, 'model': model, 'thinking_level': thinking_level, 'thinking_info': thinking_info})
        prices = body.get('pricing_cny', previous.get('pricing_cny') if previous else None)
        if prices is not None:
            if not isinstance(prices, dict) or set(prices) != {'cache_hit_input', 'cache_miss_input', 'output'}:
                raise ValueError('单价需要完整填写输入、缓存输入和输出价格')
            for price in prices.values():
                if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or not 0 <= price <= 10000:
                    raise ValueError('单价需要是 0 到 10000 的人民币金额')
        # A capability measurement may only be recorded by a caller that marks it
        # as measured; the panel/UI keeps it read-only.
        if isinstance(body.get('subagent_evidence'), dict) and body['subagent_evidence'].get('state') in {'supported', 'unsupported'}:
            if body['subagent_evidence'].get('measured') is not True:
                raise ValueError('子代理能力实测记录必须带有 measured=true')
        subagent_input = body.get('subagent') if isinstance(body.get('subagent'), dict) else {}
        if isinstance(body.get('subagent_policy'), str):
            subagent_input = {**subagent_input, 'policy': body['subagent_policy']}
        if 'evidence' not in subagent_input:
            if isinstance(body.get('subagent_evidence'), dict):
                subagent_input = {**subagent_input, 'evidence': [body['subagent_evidence']]}
            elif isinstance(body.get('subagent_evidence'), list):
                subagent_input = {**subagent_input, 'evidence': body['subagent_evidence']}
        subagent_block = model_capabilities.normalize_block(subagent_input, previous.get('subagent') if previous else None)
        if body.get('subagent_evidence') and not subagent_block.get('evidence'):
            # Never store an allow policy next to a silently dropped claim.
            raise ValueError('子代理能力实测记录不完整（需要 measured=true、identity.model 与可解析 tested_at），已拒绝')
        row = {"id": model_id, "name": name, "provider": provider, "api_base": api_base,
               "model": model, "secret": secret, "api_key_env": env_name,
               "enabled": body.get("enabled", True), "bypass_proxy": body.get("bypass_proxy", False),
               "last_check": None, "created_at": previous["created_at"] if previous else now(), **limits,
               "preferred_tasks": list(dict.fromkeys(item.strip() for item in capabilities)),
               "capability_notes": notes, "cost_preference": cost_preference,
               'max_task_complexity': complexity, 'pricing_cny': prices, 'agent_preset': agent_preset,
               'default_agent': default_agent,
               'thinking_level': thinking_level, 'thinking_info': thinking_info,
               'model_version': str(body.get('model_version', previous.get('model_version', '') if previous else '')).strip()[:120],
               'subagent': subagent_block}
        data["models"] = [row if m["id"] == model_id else m for m in data["models"]] if previous else data["models"] + [row]
        if body.get("make_default"):
            if not row["enabled"]:
                raise ValueError("默认模型需要处于启用状态")
            data["default_model"] = model_id
        elif data["default_model"] == model_id and not row["enabled"]:
            data["default_model"] = next((m["id"] for m in data["models"] if m["enabled"]), None)
        if not data["default_model"] and row["enabled"]:
            data["default_model"] = model_id
        write_json(REGISTRY, data)
    return public_model(row)


def upsert_native_model(body, model_id=None):
    import workbuddy_worker
    if (set(body) - {'provider', 'name', 'model', 'enabled', 'default_agent'} or
            body.get('model', workbuddy_worker.MODEL) != workbuddy_worker.MODEL or
            body.get('default_agent', 'workbuddy') != 'workbuddy' or
            type(body.get('enabled', True)) is not bool):
        raise ValueError('WorkBuddy 只接受固定原生模型与启用状态，不接受 API/价格/子代理设置')
    ident = model_id or workbuddy_worker.MODEL_ROW
    if ident != workbuddy_worker.MODEL_ROW:
        raise ValueError('WorkBuddy 原生模型标识固定；不能改用已有 API 模型')
    with registry_lock():
        data = read_registry()
        previous = next((m for m in data['models'] if m['id'] == ident), None)
        if previous and previous.get('provider') != 'workbuddy':
            raise ValueError('这个标识已属于 API 模型，不能转换账户来源')
        row = {'id': ident, 'name': str(body.get('name') or 'WorkBuddy · DeepSeek V4.1 Flash')[:100],
               'provider': 'workbuddy', 'model': workbuddy_worker.MODEL, 'default_agent': 'workbuddy',
               'api_base': '', 'secret': '', 'api_key_env': '',
               'enabled': body.get('enabled', previous.get('enabled', True) if previous else True),
               'bypass_proxy': False, 'created_at': previous['created_at'] if previous else now(),
               'max_input_tokens': 32768, 'max_output_tokens': 4096,
               'thinking_level': 'default', 'agent_preset': 'standard', 'last_check': None,
               'max_task_complexity': 'simple', 'preferred_tasks': ['单文件修改', '文案/文档', '信息整理'],
               'capability_notes': 'WorkBuddy 原生账户积分；限定文件读写，无命令。请主脑独立验收。',
               'cost_preference': 'balanced', 'pricing_cny': None,
               'subagent': model_capabilities.normalize_block(None, None)}
        data['models'] = [row if m['id'] == ident else m for m in data['models']] if previous else data['models'] + [row]
        # Never change the user's existing manual default as part of integration.
        write_json(REGISTRY, data)
    return public_model(row)


def update_model_agent_default(model_id, value):
    """Preference-only atomic patch; connection/preset/policy fields stay intact."""
    import model_agent_defaults
    return model_agent_defaults.update(model_id, value)


def update_agent_settings(model_id, engine, values):
    """Atomic, adapter-scoped patch; never round-trip connection/secret fields."""
    import execution_engines
    patch = execution_engines.validate_configuration(engine, values)
    with registry_lock():
        data = read_registry()
        row = next((m for m in data['models'] if m['id'] == model_id), None)
        if row is None:
            raise ValueError('模型不存在')
        row.update(patch)
        row['updated_at'] = now()
        write_json(REGISTRY, data)
    return public_model(row)


def delete_model(model_id):
    with registry_lock():
        data = read_registry()
        if not any(m["id"] == model_id for m in data["models"]):
            raise ValueError("模型不存在")
        data["models"] = [m for m in data["models"] if m["id"] != model_id]
        if data["default_model"] == model_id:
            data["default_model"] = next((m["id"] for m in data["models"] if m["enabled"]), None)
        write_json(REGISTRY, data)


def set_default(model_id):
    with registry_lock():
        data = read_registry()
        row = next((m for m in data["models"] if m["id"] == model_id), None)
        if row is None or not row["enabled"]:
            raise ValueError("请选择一个已启用的模型")
        data["default_model"] = model_id
        write_json(REGISTRY, data)
    return public_model(row)


def update_check(model_id, check):
    with registry_lock():
        data = read_registry()
        for row in data["models"]:
            if row["id"] == model_id:
                row["last_check"] = check
        write_json(REGISTRY, data)


def task_dir(task_id, project=None):
    """Resolve one task's run directory without a global "current project".

    With an explicit ``project`` the project's own runs directory is preferred,
    so the locator index is only an optimisation there.  Without one, the global
    locator index decides; an unindexed (legacy) id keeps the historical
    ``RUNS`` location.  An indexed task never falls back to a legacy backup
    copy, so a migrated record is not silently served twice.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
        raise ValueError("任务 ID 不正确")
    return project_storage.resolve_task_dir(task_id, project)


def prepare_task_dir(task_id, project):
    """Explicit first-time project initialization plus one reserved task dir."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
        raise ValueError("任务 ID 不正确")
    return project_storage.prepare_task_dir(task_id, project)


def project_of_run(run):
    """The owning project when ``run`` is inside a ``.model-desk/runs`` tree."""
    parts = Path(run).parts
    if project_storage.DESK_DIR not in parts:
        return None
    index = parts.index(project_storage.DESK_DIR)
    if index < 1 or len(parts) <= index + 1 or parts[index + 1] != project_storage.RUNS_NAME:
        return None
    return str(Path(*parts[:index]))


def patch_task(run, **fields):
    run = Path(run)
    path = run / "task.json"
    with file_lock(run / "task.lock"):
        state = read_json(path) if path.exists() else {}
        if (run / "cancelled.flag").exists() and "status" in fields:
            fields["status"] = "cancelled"
            fields["phase"] = "已停止接收" if state.get("kind") == "chat" else "已停止执行"
        state.update(fields)
        state["updated_at"] = now()
        write_json(path, state)
    project = project_of_run(run)
    if project and any(key in fields for key in ('goal_id', 'stage_id', 'kind')):
        # The project DB keeps only the derived association row; task.json above
        # stays the single authority for the stored content.
        try:
            project_storage.register_task_record(project, state.get("id") or run.name,
                                                 state.get("goal_id"), state.get("stage_id"),
                                                 state.get("kind"), state.get("created_at"))
        except (OSError, ValueError):
            pass
    return state
