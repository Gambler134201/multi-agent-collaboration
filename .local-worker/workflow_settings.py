"""Workflow budget/switch settings stored next to the model registry.

Settings live in ``model_registry.HERE / workflow.json`` guarded by
``model_registry.HERE / workflow.lock``. Paths are resolved at call time so a
test can point ``model_registry.HERE`` at a temporary directory.
"""
import json
import math
import sys
from pathlib import Path

import model_registry as registry
import model_scope
import execution_engines

DEFAULTS = {
    "version": 1,
    "enabled": False,
    "paid_allowed": False,
    "enforce_paid_budget": True,
    "enforce_model_call_limit": True,
    "task_budget_cny": 1.0,
    "daily_budget_cny": 5.0,
    "max_model_calls": 6,
    # Global execution-model scope.  Absent in older files, where the default
    # "all" preserves the previous routing behaviour without user action.
    "model_scope": model_scope.DEFAULT_SCOPE,
    "execution_agent": execution_engines.DEFAULT,
}

SETTINGS_NAME = "workflow.json"
LOCK_NAME = "workflow.lock"

FIELDS = ("version", "enabled", "paid_allowed", "enforce_paid_budget", "enforce_model_call_limit", "task_budget_cny", "daily_budget_cny", "max_model_calls", "model_scope", "execution_agent")
BOOL_FIELDS = ("enabled", "paid_allowed", "enforce_paid_budget", "enforce_model_call_limit")
MONEY_FIELDS = ("task_budget_cny", "daily_budget_cny")
INT_FIELDS = ("max_model_calls",)
ENUM_FIELDS = {"model_scope": model_scope.SCOPES, "execution_agent": execution_engines.AGENTS}
KNOWN_FIELDS = FIELDS


def _settings_path():
    return Path(registry.HERE) / SETTINGS_NAME


def _lock():
    return registry.file_lock(Path(registry.HERE) / LOCK_NAME)


def _fallback(reason, raw=None):
    """Defaults with the risky switches forced off.

    A raw stored ``model_scope`` is carried through unchanged so the caller can
    see the illegal value and refuse explicitly instead of silently widening the
    scope to "all".  Everything else falls back to safe defaults.
    """
    settings = dict(DEFAULTS)
    settings["enabled"] = False
    settings["paid_allowed"] = False
    if isinstance(raw, dict) and "model_scope" in raw:
        settings["model_scope"] = raw["model_scope"]
    if isinstance(raw, dict) and "execution_agent" in raw:
        settings["execution_agent"] = raw["execution_agent"]
    return settings, str(reason)


def _validate_values(values):
    """Return a clean settings dict; raise ValueError on any bad field."""
    if not isinstance(values, dict):
        raise ValueError("内容需要是一个对象")
    out = {}

    version = values.get("version", DEFAULTS["version"])
    if isinstance(version, bool) or version != 1:
        raise ValueError("version 需要是 1")
    out["version"] = 1

    for field in BOOL_FIELDS:
        value = values.get(field, DEFAULTS[field])
        if not isinstance(value, bool):
            raise ValueError(f"{field} 需要是布尔值")
        out[field] = value

    for field in MONEY_FIELDS:
        value = values.get(field, DEFAULTS[field])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{field} 需要是 0 到 1000 之间的数字")
        if not 0 <= value <= 1000:
            raise ValueError(f"{field} 需要在 0 到 1000 之间")
        out[field] = float(value)

    for field in INT_FIELDS:
        value = values.get(field, DEFAULTS[field])
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} 需要是整数")
        if not 1 <= value <= 30:
            raise ValueError(f"{field} 需要在 1 到 30 之间")
        out[field] = int(value)

    # The scope enum is validated strictly: a typo or a non-string value is
    # refused instead of being silently widened to "all".  Only an absent field
    # (an older settings file) uses the backward-compatible default.
    for field, allowed in ENUM_FIELDS.items():
        value = values.get(field, DEFAULTS[field])
        if not isinstance(value, str):
            raise ValueError(f"{field} 需要是字符串；可选值：" + "、".join(allowed))
        text = value.strip().lower()
        if text not in allowed:
            raise ValueError(f"{field} 不是有效值；可选值：" + "、".join(allowed))
        out[field] = text

    return out


def _effective_unlocked():
    """Read the effective settings. Caller must already hold the lock."""
    path = _settings_path()
    if not path.exists():
        return dict(DEFAULTS), ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _fallback("工作流设置文件无法解析")
    if not isinstance(raw, dict):
        return _fallback("工作流设置内容不是对象")
    try:
        # Extra keys in the file are ignored, never trusted.
        return _validate_values({key: raw[key] for key in KNOWN_FIELDS if key in raw}), ""
    except ValueError as exc:
        return _fallback(f"工作流设置字段非法：{exc}", raw)


def _payload(settings, readable, error):
    payload = {field: settings[field] for field in FIELDS}
    payload["readable"] = bool(readable)
    payload["error"] = str(error or "")
    return payload


def snapshot():
    """Current settings plus readable/error markers. Never writes the file."""
    with _lock():
        settings, error = _effective_unlocked()
    return _payload(settings, not error, error)


def save_settings(body):
    """Merge known fields onto the effective settings and save atomically."""
    if not isinstance(body, dict):
        raise ValueError("设置需要是一个对象")
    unknown = sorted(str(key) for key in body if key not in KNOWN_FIELDS)
    if unknown:
        raise ValueError("不支持的设置字段：" + "、".join(unknown))
    with _lock():
        current, _ = _effective_unlocked()
        merged = dict(current)
        merged.update({key: body[key] for key in KNOWN_FIELDS if key in body})
        settings = _validate_values(merged)
        registry.write_json(_settings_path(), settings)
    # Built from memory: snapshot() would re-enter the same lock.
    return _payload(settings, True, "")


def _emit(payload):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    stream = getattr(sys, "stdout", None)
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write((text + "\n").encode("utf-8"))
        buffer.flush()
    else:
        print(text)
    return 0


def main(argv=None):
    return _emit(snapshot())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
