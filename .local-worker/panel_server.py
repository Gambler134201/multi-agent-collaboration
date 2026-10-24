"""Loopback-only dashboard for Codex's local and online execution models."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlparse, unquote
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import model_registry as registry
import deepseek_billing
import dsh_worker
import pi_worker
import execution_engines
import model_agent_defaults
import workflow_settings
import model_router
import model_scope
import budget_relay
import recovery_retry
import thinking_config
import codex_usage
import host_balance
import delegation_policy
import stage_store
import task_handover
import executor_adapter
import model_capabilities
import failure_classification
import project_scope
import project_views
import project_storage
import folder_picker
import task_history
import task_timing
import record_protection

HERE = Path(__file__).resolve().parent
WEB = HERE / "panel"
PYTHON = HERE / "venv" / "Scripts" / "python.exe"
TOKEN = secrets.token_urlsafe(32)


class PanelSessionExpired(PermissionError):
    """Authentication guard refusal: the mutation handler has not run."""


class ReviewRefused(ValueError):
    """A delegation-policy validation refusal of a review/return/block request.

    These checks run before the policy writes the review record, so the brain
    journal may treat this as a proven rejection instead of an unknown outcome.
    """


class DispatchRefused(ValueError):
    """A proven refusal before task creation/queue submission, never mid-effect."""


EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-task")
ACTIVE = {}
ACTIVE_LOCK = threading.Lock()


def history_idle_guard():
    with ACTIVE_LOCK:
        if ACTIVE:raise ValueError('执行器尚未退出，请等待队列空闲后删除记录')
    import psutil
    worker_scripts=[str(registry.HERE / name).replace('/', '\\').casefold()
                    for name in ('dsh_worker.py','pi_worker.py','chat_worker.py','run_worker.py')]
    run_paths=[str(root).replace('/', '\\').casefold() for _project, root in project_storage.task_roots()]
    for process in psutil.process_iter(['name']):
        if process.info['name'] not in {'python.exe','node.exe','DeepSeek Harness.exe'}:continue
        try:command=' '.join(process.cmdline()).replace('/', '\\').casefold()
        except psutil.NoSuchProcess:continue
        except psutil.AccessDenied:raise ValueError('无法核对执行进程，暂不删除记录') from None
        if any(root in command for root in run_paths) or any(script in command for script in worker_scripts):
            raise ValueError('仍有工作台执行进程，请等待退出后删除记录')


def execution_activity():
    """Global live executor status, separately labelled from scoped task history."""
    tasks=[];complete=True
    for entry in project_storage.iter_task_dirs():
        path = entry['run']
        try:
            row=registry.read_task(path)
            if row.get('status') in {'running','queued','dispatching'}:
                tasks.append({key:row.get(key,'') for key in ('id','title','status','project','model_id','model_name')})
        except (OSError,ValueError,RuntimeError):complete=False
    return {'scope':'global','tasks':tasks,'total':len(tasks),'complete':complete}


def capability_rows():
    """Capability rows enriched with the route endpoint the worker bills against.

    ``registry.capability_models`` intentionally whitelists fields and does not
    carry ``api_base``; the panel adds it from the public model view so the
    aggregate identity (endpoint included) matches the worker's gate instead of
    silently denying every model that has a route.
    """
    rows = registry.capability_models()
    try:
        live = {row.get("id"): row for row in registry.list_models().get("models", [])}
    except Exception:
        live = {}
    for row in rows:
        source = live.get(row.get("id")) or {}
        if not row.get("api_base") and source.get("api_base"):
            row["api_base"] = source.get("api_base")
    return rows


# ------------------------------------------------- observability / capability
# Thin, read-only adapters over records other stages already persist.  Nothing
# here dispatches, retries or charges anything; every missing value stays
# explicit ("未取得" / unknown) instead of being rendered as a precise 0.

SINGLE_REQUEST_KEY = "single_request_output"
OBSERVABILITY_NOTE = ('费用、用量、计量与上下文来自真实记录：已结算费用与未知费用分开，'
                      'provider usage 是服务商返回的实际用量，meter_* 是发出前计数及其可信度，'
                      'context_* 是字节审计不是 token；缺失一律标未取得/未知，不把空值当 0 或精确值。')


def subagent_capabilities():
    """Capability rows enriched with the recorded evidence version/time.

    ``model_capabilities.public_block`` carries the decision, policy, proven
    modes and reasons but not the evidence identity/time.  The panel edit dialog
    needs those to explain *why* a status is unknown/stale and when it was
    measured, so this adapter re-reads the raw block and adds a redacted summary.
    The enforcement decision itself is untouched.
    """
    engine, _ = execution_engines.public_selection(workflow_settings.snapshot())
    view = executor_adapter.capabilities_view(capability_rows(), engine=engine)
    raw = {row.get("id"): row for row in registry.capability_models()}
    for row in view.get("models", []):
        source = raw.get(row.get("model_id")) or {}
        block = source.get("subagent") if isinstance(source.get("subagent"), dict) else {}
        records = block.get("evidence") if isinstance(block.get("evidence"), list) else []
        records = [r for r in records if isinstance(r, dict) and
                   (r.get('identity') or {}).get('executor', 'dsh') == engine]
        record = records[-1] if records else {}
        identity = record.get("identity") if isinstance(record.get("identity"), dict) else {}
        row["evidence_detail"] = model_capabilities.public_view({
            "state": record.get("state"),
            "measured": record.get("measured"),
            "modes": record.get("modes") or [],
            "modes_source": record.get("modes_source"),
            "read_only_probe": record.get("read_only_probe"),
            "tested_at": record.get("tested_at"),
            "executor_version": record.get("executor_version"),
            "version": identity.get("version"),
            "model_version": identity.get("model_version"),
            "test_id": record.get("test_id"),
            "source": record.get("source"),
        }) if record else None
        row["evidence_updated_at"] = block.get("updated_at")
        row["backend_implemented"] = row.get("executor") == model_capabilities.DEFAULT_EXECUTOR
        # A stored allow policy can only become effective on top of a same-identity
        # measured "supported" state; a local/home row stays denied even then.
        row["allowable"] = bool(row.get("state") == model_capabilities.STATE_SUPPORTED
                                and row.get("proven_modes")
                                and not row.get("stale") and not row.get("expired")
                                and not row.get("local"))
        row["worker_usable"] = bool(row.get("enabled"))
    view["note"] = ('子代理支持状态（三态 + 证据版本/时间）与“允许派生”策略分开呈现；'
                    '没有同身份实测证据时不可允许，且不因付费可启用；本地/home 禁止派生但仍可作为 worker 执行。')
    return model_capabilities.public_view(view)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int_or_none(value):
    number = _number(value)
    return int(number) if number is not None else None


def _child_rows(task):
    """Real parent/child rows from the recorded DSH lifecycle ledger only.

    There is no per-child invoice: DSH exposes no per-child token field, so a
    child's cost stays ``unavailable`` and the parent aggregate is shown
    separately.  ``local`` means an in-process provider, never a free local API.
    """
    events = task.get("subagent_events") if isinstance(task.get("subagent_events"), list) else []
    order = []
    children = {}
    for row in events:
        if not isinstance(row, dict) or row.get("type") == "parent":
            continue
        child_id = str(row.get("child_id") or "").strip()
        if not child_id:
            continue
        entry = children.setdefault(child_id, {"start": None, "end": None, "verified": False})
        if child_id not in order:
            order.append(child_id)
        if row.get("type") == "start":
            entry["start"] = row
            entry["verified"] = row.get("source") == "dsh-subagent-lifecycle"
        elif row.get("type") == "end":
            entry["end"] = row
    rows = []
    for child_id in order:
        entry = children[child_id]
        start = entry.get("start") or {}
        end = entry.get("end") or {}
        first = start or end
        child_model = first.get("child_model") or end.get("child_model") or task.get("model")
        same_route = not task.get("model") or child_model == task.get("model")
        rows.append({
            "child_id": child_id,
            "run_id": first.get("run_id"),
            "provider": first.get("provider"),
            "local": first.get("local"),
            "local_flag_note": "local=true 指进程内 provider，不代表免费本地 API 模型",
            "model_id": task.get("model_id") if same_route else None,
            "model": child_model,
            "model_name": (task.get("model_name") if same_route else None) or child_model,
            "started_at": start.get("at"),
            "ended_at": end.get("at"),
            "stop_reason": end.get("stop_reason"),
            "output_chars": _int_or_none(end.get("output_chars")),
            "verified": bool(entry.get("verified")),
            "state": "已结束" if end else "未验证 · 缺少结束事件",
            "cost": "不可证实（DSH 无 per-child 账单，仅父级聚合）",
        })
    return rows


def _children_summary(task, children, review_state=None):
    decision = task.get("subagent_capability") if isinstance(task.get("subagent_capability"), dict) else {}
    allowed = decision.get("decision") == "allow"
    provenance = task.get("probe_mode_provenance") if isinstance(task.get("probe_mode_provenance"), dict) else {}
    # A probe round records what it *measured*; the formal cap still lives in the
    # main-accepted evidence.  Historical probe rows carried a provisional
    # ``proven_modes`` label, so the real measured mode is shown from provenance.
    probe = (bool(decision.get("probe")) or decision.get("state") == "probe"
             or bool(provenance) or bool(task.get("subagent_edit_probe_evidence")))
    measured = decision.get("probe_target_mode") or provenance.get("mode_measured")
    probe_accepted = review_state in ("accepted", "final_accepted")
    verified = sum(1 for row in children if row["verified"])
    if children:
        state = "有实测生命周期记录"
    elif allowed and probe and probe_accepted:
        state = "受控探测已完成：测量目标 %s（已独立验收）" % (measured or "未标注")
    elif allowed and probe:
        state = "受控探测：本轮测量目标 %s，结果待主脑独立验收" % (measured or "未标注")
    elif probe:
        state = "受控探测未获允许：测量目标 %s（原因见下）" % (measured or "未标注")
    elif allowed:
        state = "已允许派生 · 本次运行未记录子代理"
    else:
        state = "未启用/未验证（未获允许或无实测证据）"
    reasons = list(decision.get("reasons") or [])
    labelled = decision.get("proven_modes") or []
    if measured and labelled and measured not in labelled:
        # A genuine provisional-label mismatch (historical rows wrote a chat
        # label for an edit round); an unproven new probe has no such label.
        reasons.append("该记录只是测量目标；已证实模式以已接受的证据为准，本轮实际 kind=%s" % measured)
    return {
        "state": state,
        "allowed": allowed,
        "probe": probe,
        "probe_accepted": probe_accepted,
        "capability": {
            "decision": decision.get("decision"),
            "state": decision.get("state"),
            "policy": decision.get("policy"),
            "proven_modes": decision.get("proven_modes") or [],
            "probe_target_mode": measured,
            "pending_acceptance": bool(decision.get("pending_acceptance")) if decision.get("pending_acceptance") is not None
                                  else (probe and not probe_accepted),
            "probe_mode_provenance": provenance or None,
            "mode": decision.get("mode"),
            "reasons": reasons[:6],
        },
        "count": len(children),
        "verified": verified,
        "unverified": len(children) - verified,
        "per_child_billing": "unavailable",
    }


def _provider_usage(task):
    live = task.get("usage_live")
    live_usage = live.get("usage") if isinstance(live, dict) and isinstance(live.get("usage"), dict) else None
    usage = live_usage or (task.get("usage") if isinstance(task.get("usage"), dict) else {})
    fields = {
        "input_tokens": _int_or_none(usage.get("prompt_tokens")),
        "cache_hit_tokens": _int_or_none(usage.get("prompt_cache_hit_tokens")),
        "cache_miss_tokens": _int_or_none(usage.get("prompt_cache_miss_tokens")),
        "output_tokens": _int_or_none(usage.get("completion_tokens")),
        "total_tokens": _int_or_none(usage.get("total_tokens")),
    }
    available = any(value is not None for value in fields.values())
    settled_calls = _int_or_none(task.get("cost_settled_calls")) or 0
    unknown_calls = _int_or_none(task.get("cost_unknown_calls")) or 0
    # The provider returns usage per request.  When some requests never returned
    # usage, the tokens above are only the returned part; the unknown call count
    # must be visible so nobody reads them as the whole goal's total tokens.
    partial = bool(available and unknown_calls > 0)
    if not available:
        scope = "未取得（服务商未返回 usage）"
    elif partial:
        scope = ("已返回部分用量：仅已结算 %d 次请求的 usage；另有 %d 次请求未返回 usage（未知）。"
                 "这不是全部调用或整个目标的总 tokens。" % (settled_calls, unknown_calls))
    else:
        scope = "已返回 %d 次已结算请求的 usage；未记录未返回 usage 的请求。" % settled_calls
    return {
        "available": available,
        "live": bool(live_usage),
        "source": ("provider usage（服务商返回，实时）" if live_usage else
                   "provider usage（服务商返回）" if available else
                   "未取得（服务商未返回 usage）"),
        "partial": partial,
        "known_calls": settled_calls,
        "unknown_calls": unknown_calls,
        "total_calls": settled_calls + unknown_calls,
        "scope": scope,
        **fields,
    }


def _meter_facts(task):
    record = task.get("meter_record") if isinstance(task.get("meter_record"), dict) else {}
    sources = record.get("sources") if isinstance(record.get("sources"), dict) else {}
    window = record.get("window") if isinstance(record.get("window"), dict) else {}
    fit = window.get("fit") if isinstance(window.get("fit"), dict) else {}
    verified = task.get("meter_window_verified")
    if verified is None:
        verified = window.get("semantics_verified")
    return {
        "source": task.get("meter_source") or sources.get("used") or "未取得",
        "confidence": task.get("meter_precision") or sources.get("precision") or "unavailable",
        "input_tokens": _int_or_none(task.get("meter_input_tokens")),
        "input_origin": task.get("meter_input_origin"),
        "output_target": _int_or_none(task.get("meter_output_target")),
        "output_origin": task.get("meter_output_origin"),
        "window_kind": task.get("meter_window_kind") or window.get("kind"),
        "window_verified": bool(verified) if verified is not None else None,
        "fit_state": task.get("meter_fit_state") or fit.get("state"),
        "guard": task.get("meter_guard") or record.get("guard"),
        "warnings": list(task.get("meter_warnings") or record.get("warnings") or [])[:8],
        "identity": task.get("meter_identity") or record.get("identity"),
    }


def _context_facts(task):
    return {
        "note": task.get("context_note") or "context_* 是字节审计，不等于 token 精确节省",
        "bytes_before": _int_or_none(task.get("context_bytes_before")),
        "bytes_after": _int_or_none(task.get("context_bytes_after")),
        "bytes_saved": _int_or_none(task.get("context_bytes_saved")),
        "offloaded_count": _int_or_none(task.get("context_offloaded_count")),
        "offloaded_bytes": _int_or_none(task.get("context_offloaded_bytes")),
        "preview_bytes": _int_or_none(task.get("context_preview_bytes")),
        "reused_count": _int_or_none(task.get("context_reused_count")),
        "created_count": _int_or_none(task.get("context_created_count")),
        "not_trimmed_count": _int_or_none(task.get("context_not_trimmed_count")),
        "already_offloaded_count": _int_or_none(task.get("context_already_offloaded_count")),
        "skipped_reason": task.get("context_skipped_reason") or "",
        "error": task.get("context_error") or "",
        "outputs": list(task.get("context_outputs") or [])[-8:],
    }


def _billing_boundary(task):
    block = task.get("subagent_usage") if isinstance(task.get("subagent_usage"), dict) else {}
    aggregate = block.get("aggregate") if isinstance(block.get("aggregate"), dict) else None
    parent_settled = _number(task.get("cost_settled_cny"))
    parent_calls = _int_or_none(task.get("cost_settled_calls"))
    parent_known = parent_settled is not None and ((parent_calls or 0) > 0 or parent_settled > 0)
    return {
        "parent": {
            # Unknown/pending amounts stay null instead of a fake ¥0.00.
            "settled_cny": parent_settled if parent_known else None,
            "settled_calls": parent_calls,
            "unsettled_cny": _number(task.get("cost_unsettled_cny")),
            "reserved_is_real_cost": False,
            "unknown_calls": _int_or_none(task.get("cost_unknown_calls")),
            "schema": _int_or_none(task.get("cost_schema")),
        },
        "children": {
            "per_child": block.get("per_child") or "unavailable",
            "authoritative": block.get("authoritative") or "unavailable",
            "aggregate": aggregate,
            "reason": block.get("reason") or "无子代理聚合记录",
        },
        "note": ("父级账单来自本任务 cost_*（按真实 usage 与请求时费率结算，未知另列）；"
                 "子代理只有聚合窗口，per-child 账单不可证实，不伪造子账单。"),
    }


def _calibration_summary(task):
    calibration = task.get("usage_calibration")
    if not isinstance(calibration, dict):
        return None
    model = str(task.get("model") or "")
    summary = calibration.get("summary") if isinstance(calibration.get("summary"), dict) else {}
    match = None
    for key, value in summary.items():
        if isinstance(value, dict) and model and model in str(key):
            match = value
            break
    if match is None and summary:
        first = next((value for value in summary.values() if isinstance(value, dict)), None)
        match = first
    match = match if isinstance(match, dict) else {}
    return {
        "estimate_tokens": _int_or_none(calibration.get("estimate_tokens")),
        "actual_prompt_tokens": _int_or_none(calibration.get("actual_prompt_tokens")),
        "precision": calibration.get("precision"),
        "source": calibration.get("source"),
        "observed": _int_or_none(match.get("observed")),
        "estimate_total": _int_or_none(match.get("estimate_total")),
        "actual_total": _int_or_none(match.get("actual_total")),
        "ratio": _number(match.get("ratio")),
        "note": calibration.get("note") or "事后校准观测，不回灌发出前估计。",
    }


def _forecast(task):
    route = task.get("route") if isinstance(task.get("route"), dict) else {}
    requirements = route.get("requirements") if isinstance(route.get("requirements"), dict) else {}
    cost = route.get("cost") if isinstance(route.get("cost"), dict) else {}
    prediction = cost.get("prediction") if isinstance(cost.get("prediction"), dict) else None
    observed = cost.get("observed") if isinstance(cost.get("observed"), dict) else None
    pricing = cost.get("pricing") if isinstance(cost.get("pricing"), dict) else None
    risk = route.get("failure_risk") if isinstance(route.get("failure_risk"), dict) else {}
    single = route.get(SINGLE_REQUEST_KEY) if isinstance(route.get(SINGLE_REQUEST_KEY), dict) else None
    return {
        "mode": route.get("mode"),
        "reason": route.get("reason"),
        "estimated_cost_cny": _number(route.get("estimated_cost_cny")),
        "prediction": prediction,
        "observed_cost": observed,
        "pricing": pricing,
        "retry_interval": risk.get("interval"),
        "retry_confidence": risk.get("confidence"),
        "retry_samples": _int_or_none(risk.get("verified_samples")),
        "retry_rate": _number(risk.get("quality_rework_rate")),
        "retry_basis": risk.get("basis"),
        "requirements": {
            "category": requirements.get("category"),
            "complexity": requirements.get("complexity"),
            "input_tokens": _int_or_none(requirements.get("input_tokens")),
            "output_tokens": _int_or_none(requirements.get("output_tokens")),
            "expected_calls": _int_or_none(requirements.get("expected_calls")),
            "estimate_semantics": (requirements.get("capacity") or {}).get("estimate_semantics")
            if isinstance(requirements.get("capacity"), dict) else None,
        },
        "single_request_output": single,
        "calibration": _calibration_summary(task),
    }


def _cost_facts(task):
    if task.get('engine') in ('workbuddy', 'zcode'):
        return {'schema': None, 'known': False, 'settled_cny': None,
                'recorded_settled_cny': None, 'settled_calls': None,
                'unsettled_cny': None, 'reserved_cny': None, 'reserved_is_real_cost': False,
                'unknown_calls': None, 'billing_source': task['engine']+'-native-account',
                'label': execution_engines.LABELS[task['engine']]+(' 原生积分未返回' if task['engine']=='workbuddy' else ' 原生额度用量未返回'),
                'note': '原生积分、实际底层请求次数及人民币费用未知；不并入 API 人民币账单，不显示为零费用。'}
    settled = _number(task.get("cost_settled_cny"))
    settled_calls = _int_or_none(task.get("cost_settled_calls")) or 0
    unknown_calls = _int_or_none(task.get("cost_unknown_calls")) or 0
    known = settled is not None and (settled_calls > 0 or settled > 0)
    reserved = _number(task.get("cost_unsettled_cny"))
    return {
        "schema": _int_or_none(task.get("cost_schema")),
        "known": bool(known),
        # A pending/unknown amount is never rendered as a precise ¥0.00 real fee.
        # The raw recorded value stays separately visible for audit.
        "settled_cny": settled if known else None,
        "recorded_settled_cny": settled,
        "settled_calls": settled_calls,
        "unsettled_cny": reserved,
        "reserved_cny": reserved,
        "reserved_is_real_cost": False,
        "unknown_calls": unknown_calls,
        "label": ("按用量计算费用 " + ("待核对" if not known else "")) if not known else "按用量计算费用",
        "note": ("已结算费用来自真实 usage 与请求时费率；未返回 usage 的请求计未知，不当作 0 元。"
                 "未结算预留只是本次请求的审计预留，不是真实费用；金额未知时显示为待核对，"
                 "不把预留 0 显示成未知真实费用 0。"),
    }


def review_facts(task_id, attached=None):
    """The one real review source shared by single, batch and tree views.

    ``review.json`` (``delegation_policy``) is authoritative.  A raw
    ``task.json`` field such as ``review_state`` is only a cache, so it is never
    trusted on its own: a task that claims ``accepted`` with no real review
    record stays explicitly unknown, and a corrupt record stays unreadable
    instead of being promoted to accepted.
    """
    if isinstance(attached, dict):
        return attached
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    try:
        run = registry.task_dir(task_id)
    except ValueError:
        return None
    if not run.is_dir():
        return None
    record, error = delegation_policy.safe_load(run)
    if record is not None:
        return delegation_policy.panel_summary(record)
    if error:
        return {"state": "unreadable", "label": "验收记录无法读取", "error": error,
                "rework_count": None, "max_auto_rework": delegation_policy.MAX_AUTO_REWORK}
    return None


def observability_view(task):
    """Normalized, read-only observability for one real task record."""
    task = task if isinstance(task, dict) else {}
    route = task.get("route") if isinstance(task.get("route"), dict) else {}
    children = _child_rows(task)
    # Per-item, batch and tree all read the same real review record; an attached
    # summary is only a fallback for synthetic rows that have no task id.
    review = review_facts(task.get("id"))
    if review is None and not str(task.get("id") or "").strip():
        review = task.get("review") if isinstance(task.get("review"), dict) else None
    readonly_files = [str(name) for name in (task.get("readonly_files") or []) if str(name).strip()]
    scope_files = [str(name) for name in (task.get("files") or []) if str(name).strip()]
    if task.get("kind") == "chat" and scope_files and not readonly_files:
        # Legacy chat tasks recorded the scope before the read-only marker
        # existed; a chat file is still read-only context, never modifiable.
        readonly_files = list(scope_files)
    return model_capabilities.public_view({
        "task_id": task.get("id"),
        "title": task.get("title"),
        "kind": task.get("kind"),
        "status": task.get("status"),
        "phase": task.get("phase"),
        "stage_id": task.get("stage_id"),
        "goal_id": task.get("goal_id"),
        "model_id": task.get("model_id"),
        "model": task.get("model"),
        "model_name": task.get("model_name"),
        "review": review,
        "review_state": (review or {}).get("state"),
        "review_label": (review or {}).get("label"),
        "file_scope": {
            "mode": ("readonly_context" if (task.get("kind") == "chat" and readonly_files)
                     else "modifiable" if task.get("kind") == "edit" else "none"),
            "files": scope_files,
            "readonly_files": readonly_files,
            "note": ("只读上下文文件由 chat 阶段保存，用于代码索引与交接文件范围；"
                     "只读权限由 kind/engine 沙箱保证，它们不是可修改文件。"
                     if readonly_files else "无只读上下文文件。"),
        },
        "cost": _cost_facts(task),
        "provider_usage": _provider_usage(task),
        "meter": _meter_facts(task),
        "context": _context_facts(task),
        "failure": failure_classification.classify(task),
        "forecast": _forecast(task),
        "children": _children_summary(task, children, review_state=(review or {}).get("state")),
        "child_rows": children,
        "billing_boundary": _billing_boundary(task),
        "request_output": route.get(SINGLE_REQUEST_KEY),
        "note": OBSERVABILITY_NOTE,
    })


BATCH_OBSERVABILITY_LIMIT = 25
TREE_MAX_GOALS = 20
TREE_MAX_STAGES = 50
TREE_MAX_TASKS = 20
TREE_MAX_CHILDREN = 30


def observability_batch(tasks=None, view=None):
    rows = tasks if isinstance(tasks, list) else public_tasks(view)
    return {"tasks": [observability_view(row) for row in rows[:BATCH_OBSERVABILITY_LIMIT]],
            "stats": {'tasks': len(rows), 'shown': min(len(rows), BATCH_OBSERVABILITY_LIMIT)},
            "note": OBSERVABILITY_NOTE}


def task_observability(task_id):
    run = registry.task_dir(task_id)
    if not run.is_dir():
        raise ValueError("任务不存在")
    record = registry.read_task(run) if (run / "task.json").is_file() else None
    if not isinstance(record, dict):
        raise ValueError("任务记录暂时不可读，请重试")
    return observability_view(record)


def tree_view(project_view=None):
    """Real goal -> stage -> task -> child tree from persisted records only."""
    # A bound project request reads that project's own store directly; the
    # global aggregate keeps the historical behaviour for internal callers.
    project = project_views.explicit_project(project_view)
    goals = stage_store.list_goals(project)
    if project_view is not None:
        goals = project_scope.filter_records(goals,project_view)
    goals = goals[:TREE_MAX_GOALS]
    out = []
    for goal in goals:
        stages = stage_store.list_stages(goal["id"], project)
        if project_view is not None:
            stages=project_scope.filter_records(stages,project_view)
        stages=stages[:TREE_MAX_STAGES]
        stage_rows = []
        for stage in stages:
            view = stage_store.stage_view(stage["id"], conn_stages=stages, goal_status=goal["status"],
                                          project=project)
            tasks = []
            for entry in list(view.get("tasks") or [])[:TREE_MAX_TASKS]:
                task_id = entry.get("task_id")
                record = None
                try:
                    run = registry.task_dir(task_id, project)
                    if (run / "task.json").is_file():
                        record = registry.read_json(run / "task.json")
                except (OSError, ValueError):
                    record = None
                record = record if isinstance(record, dict) else {}
                if project_view is not None and not project_scope.belongs_to_project(record,project_view):
                    continue
                children = _child_rows(record)[:TREE_MAX_CHILDREN]
                # The real review record is authoritative; the task's cached
                # review_state is never used to claim an accepted result.
                review = review_facts(task_id)
                scope_files = [str(name) for name in (record.get("files") or []) if str(name).strip()]
                readonly_files = [str(name) for name in (record.get("readonly_files") or []) if str(name).strip()]
                if record.get("kind") == "chat" and scope_files and not readonly_files:
                    readonly_files = list(scope_files)
                tasks.append({
                    "task_id": task_id,
                    "title": record.get("title") or entry.get("title"),
                    "kind": record.get("kind"),
                    "status": record.get("status") or entry.get("status"),
                    "phase": record.get("phase") or entry.get("phase"),
                    "model_id": record.get("model_id") or entry.get("model_id"),
                    "model": record.get("model") or entry.get("model"),
                    "model_name": record.get("model_name"),
                    "created_at": record.get("created_at") or entry.get("created_at"),
                    "ended_at": record.get("ended_at"),
                    "dsh_session_id": record.get("dsh_session_id") or entry.get("dsh_session_id"),
                    "dsh_session_key": record.get("dsh_session_key"),
                    "engine": record.get("engine"),
                    "pi_session_id": record.get("pi_session_id"),
                    "pi_session_file": record.get("pi_session_file"),
                    "parent_task_id": record.get("parent_task_id"),
                    "rework_of": record.get("rework_of"),
                    "changed_files": record.get("changed_files") or entry.get("changed_files") or [],
                    "file_hashes": record.get("file_hashes") or {},
                    "files": scope_files,
                    "readonly_files": readonly_files,
                    "files_readonly": bool(readonly_files),
                    "request_output_tokens": _int_or_none(record.get("request_output_tokens")),
                    "review_state": (review or {}).get("state"),
                    "review_label": (review or {}).get("label"),
                    "review_error": (review or {}).get("error"),
                    "children": children,
                    "children_state": _children_summary(record, children, review_state=(review or {}).get("state")),
                })
            stage_rows.append({**view, "tasks": tasks})
        out.append({**goal, "stages": stage_rows})
    return {"goals": out, "stats": stage_store.stats(), "recovery": stage_store.pending_recovery(),
            "note": stage_store.STAGES_NOTE}





def public_daily_cost(settings):
    """Retired estimates stay on disk, never in a live UI response.

    Already-open MCP Apps keep their older JavaScript across service reloads.
    Omitting the retired fields also stops those clients rendering old amounts
    on their next normal state poll, without changing the original ledger.
    """
    return {key: value for key, value in budget_relay.daily_state(settings).items()
            if key not in {'reserved_cny', 'legacy_reserved_cny'}}


def public_task_cost(row):
    return {key: value for key, value in row.items()
            if key not in {'budget_reserved_cny', 'cost_legacy_unsettled_cny', 'legacy_unsettled_cny'}}


def panel_snapshot_tasks(view=None):
    """Polling carries display data; full audit histories stay on task detail.

    Do not trim records/public_tasks: routing, accounting and independent review
    still need the original evidence. The UI renders only the last eight tools.
    """
    audit_fields = {'cost_requests', 'context_outputs', 'meter_record',
                    'usage_calibration', 'preset_proof', 'context_index'}
    rows = []
    for task in public_tasks(view):
        row = {key: value for key, value in task.items() if key not in audit_fields}
        if isinstance(row.get('tool_calls'), list):
            row['tool_calls'] = row['tool_calls'][-8:]
        rows.append(row)
    return rows


def public_tasks(view=None):
    # Iteration itself is scoped: a project request reads only that project's
    # own store (plus its unmigrated legacy records), so the global locator is
    # never a precondition for showing the project's history.
    rows = []
    for entry in project_storage.iter_task_dirs(view):
        folder = entry['run']
        if not folder.is_dir():
            continue
        try:
            task_file = folder / "task.json"
            if task_file.exists():
                row = json.loads(task_file.read_text(encoding="utf-8"))
            elif (folder / "result.json").exists():
                result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
                title = (folder / "task.txt").read_text(encoding="utf-8").splitlines()[0][:80]
                row = {"id": folder.name, "title": title, "kind": "edit", "source": "codex",
                       "model_id": "home", "model_name": "家里的 Qwen", "model": result.get("model"),
                       "status": "completed" if result.get("exit_code") == 0 else "failed",
                       "phase": "历史任务", "project": result.get("project", ""),
                       "changed_files": result.get("changed_selected_files", []),
                       "summary": result.get("log_summary", result.get("log_tail", ""))[-1800:],
                       "created_at": datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat(),
                       "ended_at": datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat()}
            else:
                continue
            # Ownership was decided by the scoped enumeration above: a project
            # view returns exactly that project's own store (plus its unmigrated
            # legacy records) and refuses a real cross-project conflict.  A row
            # physically stored in the project is not dropped just because its
            # cached ``project`` field is empty.
            row.pop("worker_pid", None)
            row.pop("process_pid", None)
            row.pop("launcher_pid", None)
            # Same real review source as the per-item and tree views; a task's
            # own cached review_state is never trusted on its own.
            if isinstance(row.get("delegation"), dict) or row.get("review_state"):
                row["review"] = review_facts(folder.name)
            else:
                row["review"] = None
            import execution_result
            row['execution_result'] = execution_result.project(row, row['review'])
            rows.append(public_task_cost(row))
        except (OSError, ValueError, IndexError):
            continue
        if len(rows) >= 150:
            break
    def task_time(row):
        value = row.get("created_at") or row.get("started_at") or row.get("ended_at")
        try:
            return datetime.fromisoformat(value).timestamp() if value else 0
        except ValueError:
            return 0
    rows.sort(key=task_time, reverse=True)
    return rows


def remote_request(model, path, body=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if model.get("api_key"):
        headers["Authorization"] = "Bearer " + model["api_key"]
    request = Request(model["api_base"].rstrip("/") + path,
                      data=json.dumps(body).encode("utf-8") if body is not None else None,
                      headers=headers)
    opener = build_opener(ProxyHandler({})) if model.get("bypass_proxy") else build_opener()
    return opener.open(request, timeout=timeout)


def safe_error(exc, key=""):
    if isinstance(exc, HTTPError):
        detail = exc.read(2048).decode("utf-8", "replace")
        message = "HTTP " + str(exc.code) + ": " + detail
    else:
        message = str(exc)
    if key:
        message = message.replace(key, "[已隐藏]")
    return message[:1500]


def probe_model(model_id):
    model = registry.get_model(model_id, require_enabled=False)
    if model.get('provider') in ('workbuddy', 'zcode'):
        raise ValueError('WorkBuddy 不使用 API 连接测试；请派发明确授权的原生账户测试任务')
    start = time.monotonic()
    try:
        # Actual generation test: /models alone doesn't establish inference availability.
        payload = {"model": model["model"], "messages": [{"role": "user", "content": "Reply OK only."}], "max_tokens": 64, "stream": False}
        thinking_config.apply(payload, model)
        with remote_request(model, "/chat/completions", payload, timeout=45) as response:
            data = json.load(response)
        if not data.get("choices"):
            raise ValueError("接口没有返回有效的 choices")
        check = {"ok": True, "kind": "generation", "inference": True, "at": registry.now(),
                 "latency_ms": round((time.monotonic() - start) * 1000),
                 "message": "生成接口连接成功"}
    except Exception as exc:
        check = {"ok": False, "kind": "generation", "inference": True, "at": registry.now(),
                 "latency_ms": round((time.monotonic() - start) * 1000),
                 "message": safe_error(exc, model.get("api_key", ""))}
    registry.update_check(model_id, check)
    return check


REACHABILITY_TIMEOUT = 10
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback(api_base):
    try:
        return (urlparse(str(api_base or "")).hostname or "").lower() in LOOPBACK_HOSTS
    except ValueError:
        return False


def reachability_check(model, timeout=REACHABILITY_TIMEOUT):
    """Bounded, read-only reachability recheck; performs no inference.

    The result is labelled ``kind='reachability'`` and ``inference=False`` so it
    can never be read as a successful generation or a measured capability.  Only
    the safe exception category and HTTP status are recorded, never a body.
    """
    start = time.monotonic()
    check = {"kind": "reachability", "inference": False, "method": "GET /models",
             "at": registry.now()}
    try:
        with remote_request(model, "/models", timeout=timeout) as response:
            json.load(response)
        check.update({"ok": True, "latency_ms": round((time.monotonic() - start) * 1000),
                      "message": "只读可达性检查通过（未做生成，不代表模型能力或质量）"})
    except Exception as exc:
        info = recovery_retry.classify_exception(exc)
        label = recovery_retry.category_label(info["category"])
        if isinstance(info["http_status"], int):
            label += f"（HTTP {info['http_status']}）"
        check.update({"ok": False, "category": info["category"], "http_status": info["http_status"],
                      "latency_ms": round((time.monotonic() - start) * 1000),
                      "message": f"只读可达性检查未通过：{label}。这是可达性结论，"
                                 "请确认模型引擎已启动并监听该地址后再派发。"})
    return check


def connectivity_gate(model):
    """Recheck an old failed connection check before a real dispatch.

    Runs at most one bounded, inference-free probe, and only when the stored check
    actually failed, so a healthy card never gains extra traffic.  It never
    changes the model identity, the paid gate or the complexity gate.
    """
    stored = model.get("last_check")
    if not isinstance(stored, dict) or stored.get("ok") is not False:
        return {"checked": False, "blocked": False, "message": "", "verdict": None, "check": None}
    verdict = model_router.connectivity_verdict(model)
    check = reachability_check(model)
    try:
        registry.update_check(model["id"], check)
    except (OSError, ValueError, KeyError):
        pass
    if check["ok"]:
        return {"checked": True, "blocked": False, "message": "", "verdict": verdict, "check": check}
    definitive = check.get("category") in {"auth", "dns", "connect", "tls", "reset"}
    hint = " 请确认模型引擎已启动并监听该地址，然后重试；本次未发送任何推理请求。"
    if is_loopback(model.get("api_base")) or definitive:
        # A refused loopback connection or an auth rejection is definitive: refuse
        # with an actionable, recoverable diagnosis instead of sending a doomed
        # paid/inference request.
        return {"checked": True, "blocked": True, "verdict": verdict, "check": check,
                "message": "派发前只读可达性检查未通过：" + check["message"] + hint}
    # A cloud card may simply not implement GET /models; keep it usable and record
    # the failure as advisory evidence instead of a capability verdict.
    return {"checked": True, "blocked": False, "message": check["message"],
            "verdict": verdict, "check": check}


def scope_gate(model, settings, task_id=None, engine=None):
    """Enforce the global execution-model scope on one already-chosen model.

    The routing gate in ``model_router.choose`` is the primary enforcement; this
    gate protects the paths that reuse a *stored* model id without re-routing:
    the queued task that starts later, an explicit recovery, and the workers
    started directly from the CLI.  It never swaps the model, never falls back to
    the other class and never touches paid authorization, budgets or the call
    limit.  A caller must treat a raised ``ValueError`` as "do not run".
    """
    if not isinstance(settings, dict):
        settings = workflow_settings.snapshot()
    import execution_endpoints
    execution_endpoints.gate(engine or execution_engines.resolve({}, settings, model=model), model, settings)
    raw = settings.get("model_scope", model_scope.DEFAULT_SCOPE)
    scope = model_scope.normalize(raw)
    if model_scope.matches(scope, model_router.is_paid(model)):
        return {"scope": scope, "label": model_scope.LABELS[scope], "model_id": model.get("id"),
                "candidate": model_scope.candidate_kind(model_router.is_paid(model))}
    prefix = ("排队任务 %s 启动前" % task_id) if task_id else "本次执行启动前"
    raise ValueError("%s：执行模型范围“%s”已排除%s %s；不会自动换模型、回退或重派，"
                     "请改用范围内的模型，或在面板调整全局执行模型范围后重试"
                     % (prefix, model_scope.LABELS[scope],
                        model_scope.candidate_label(model_router.is_paid(model)), model.get("id")))


def discover(body):
    if body.get("id") and not body.get("api_base"):
        model = registry.get_model(body["id"], require_enabled=False)
        if model.get('provider') in ('workbuddy', 'zcode'):
            raise ValueError('WorkBuddy 原生模型固定为 DeepSeek V4.1 Flash，不调用 API 模型列表')
    else:
        base = str(body.get("api_base", "")).strip().rstrip("/")
        url = urlparse(base)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("请填写有效的接口 Base URL")
        key = str(body.get("api_key", ""))
        if not key and body.get("id"):
            key = registry.get_model(body["id"], require_enabled=False)["api_key"]
        model = {"api_base": base, "api_key": key, "bypass_proxy": body.get("bypass_proxy", False)}
    try:
        with remote_request(model, "/models") as response:
            data = json.load(response)
        return {"models": [str(row["id"]) for row in data.get("data", []) if row.get("id")]}
    except Exception as exc:
        raise ValueError("读取模型列表失败，可以手动填写模型 ID。" + safe_error(exc, model.get("api_key", "")))


def execute_task(task_id):
    run = registry.task_dir(task_id)
    state = json.loads((run / "task.json").read_text(encoding="utf-8"))
    if (run / "cancelled.flag").exists():
        return
    import execution_result
    tracker = execution_result.Tracker(state.get('engine') or 'unknown')
    tracker.mark('prepare', 'running', '控制器派发前校验')
    try:
        registry.patch_task(run, execution_evidence=tracker.snapshot())
        settings = workflow_settings.snapshot()
        if state.get('route', {}).get('mode') == 'auto' and not settings['enabled']:
            raise ValueError('协作总开关已关闭，排队中的自动任务未启动；请由 Codex 按默认方式继续')
        model = registry.get_model(state["model_id"])
        # A task queued earlier is checked against the *latest* global scope right
        # before its worker starts.  An out-of-scope model is recorded and refused;
        # no provider is called and nothing is re-routed or re-dispatched.
        scope_gate(model, settings, task_id=task_id, engine=state.get('engine'))
        if state.get("engine") == "dsh":
            command = [str(PYTHON), str(HERE / "dsh_worker.py"), "--run-id", task_id]
        elif state.get("engine") == "pi":
            command = [str(PYTHON), str(HERE / "pi_worker.py"), "--run-id", task_id]
        elif state.get('engine') == 'workbuddy':
            command = [str(PYTHON), str(HERE / 'workbuddy_worker.py'), '--run-id', task_id]
        elif state.get('engine') == 'zcode':
            command = [str(PYTHON), str(HERE / 'zcode_worker.py'), '--run-id', task_id]
        elif state["kind"] == "chat":
            command = [str(PYTHON), str(HERE / "chat_worker.py"), "--run-id", task_id]
        else:
            command = [str(PYTHON), str(HERE / "run_worker.py"), "--model-id", state["model_id"],
                       "--run-id", task_id, "--source", "panel", "--title", state["title"],
                       "--project", state["project"], "--task-file", str(run / "prompt.txt"),
                       "--timeout", str(state["timeout"])]
            if state.get("test_command"):
                command += ["--test-cmd", state["test_command"]]
            command += ["--files", *state["files"]]
        with (run / "launcher.log").open("w", encoding="utf-8") as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            with ACTIVE_LOCK:
                ACTIVE[task_id] = process
            registry.patch_task(run, launcher_pid=process.pid)
            if (run / "cancelled.flag").exists():
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            process.wait()
        current = json.loads((run / "task.json").read_text(encoding="utf-8"))
        if current["status"] in {"queued", "running"}:
            message = (run / "launcher.log").read_text(encoding="utf-8", errors="replace")[-1500:]
            # A crashed child did not publish a definitive stage. Retain its
            # existing evidence when present instead of guessing from log prose.
            evidence = current.get('execution_evidence') or tracker.snapshot()
            registry.patch_task(run, status="failed", phase="执行失败", ended_at=registry.now(), error=message,
                                execution_evidence=evidence)
    except Exception as exc:
        if not (run / "cancelled.flag").exists():
            tracker.fail('prepare', 'STARTUP_FAILED', '控制器启动执行端失败；原始原因见任务错误')
            current = registry.read_task(run)
            registry.patch_task(run, status="failed", phase="执行失败", ended_at=registry.now(),
                                error=current.get('error') or safe_error(exc),
                                execution_evidence=current.get('execution_evidence') if
                                (current.get('execution_evidence') or {}).get('stages', {}).get('execute', {}).get('state') not in (None, 'not_run')
                                else tracker.snapshot())
    finally:
        with ACTIVE_LOCK:
            ACTIVE.pop(task_id, None)


def run_chat(run, state, model):
    # Last guard before a direct-consult request: the same global execution-model
    # scope as routing, so a task record created earlier cannot call a provider
    # outside the current scope.  It refuses only; no model substitution.
    scope_gate(model, workflow_settings.snapshot(), task_id=state.get("id"))
    if model_router.is_paid(model):
        raise ValueError("付费任务请使用 DSH，以执行逐次请求预算检查")
    registry.patch_task(run, status="running", phase="等待模型响应", started_at=registry.now(), worker_pid=os.getpid())
    prompt = (run / "prompt.txt").read_text(encoding="utf-8")
    body = {"model": model["model"], "messages": [{"role": "user", "content": prompt}],
            "max_tokens": model.get("max_output_tokens", 4096), "stream": True,
            "stream_options": {"include_usage": True}}
    thinking = thinking_config.apply(body, model)
    registry.patch_task(run, thinking_runtime=thinking, thinking_selected_level=thinking['selected'])
    answer = ""
    usage = {}
    last_write = 0
    deadline = task_timing.deadline(state.get('timeout',0))
    try:
        with remote_request(model, "/chat/completions", body, timeout=120) as response:
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                data = json.load(response)
                answer = data["choices"][0]["message"].get("content", "") or ""
                usage = data.get("usage", {})
            else:
                for raw in response:
                    if (run / "cancelled.flag").exists():
                        break
                    if task_timing.expired(deadline):
                        raise TimeoutError("任务超过设定时间")
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    data = json.loads(payload)
                    if data.get("error"):
                        raise ValueError(str(data["error"]))
                    for choice in data.get("choices", []):
                        answer += choice.get("delta", {}).get("content", "") or ""
                    if data.get("usage"):
                        usage = data["usage"]
                    if time.monotonic() - last_write > .3:
                        registry.patch_task(run, phase="正在生成回复", answer=answer, usage=usage)
                        last_write = time.monotonic()
        if (run / "cancelled.flag").exists():
            registry.patch_task(run, status="cancelled", phase="已停止接收", ended_at=registry.now(), answer=answer, usage=usage)
        elif not answer.strip():
            raise ValueError("模型没有返回正文，请提高输出预算或检查模型配置")
        else:
            (run / "answer.txt").write_text(answer, encoding="utf-8")
            registry.patch_task(run, status="completed", phase="回复完成", ended_at=registry.now(), answer=answer, usage=usage,
                                summary="回复已完成", exit_code=0)
    except Exception as exc:
        if not (run / "cancelled.flag").exists():
            registry.patch_task(run, status="failed", phase="请求失败", ended_at=registry.now(), answer=answer,
                                error=safe_error(exc, model.get("api_key", "")))


def _clean_readonly_files(project_path, requested):
    """Validate read-only context files for a chat task (project-relative, real).

    These files only feed the code index and the handover scope.  They are never
    writable scope, and an invalid or missing path is refused instead of being
    silently dropped so the documented context is what actually reaches the run.
    """
    if requested in (None, ""):
        return []
    if not isinstance(requested, list) or len(requested) > 30:
        raise ValueError("只读上下文文件需要是最多 30 个的相对路径")
    cleaned = []
    for name in requested:
        text = str(name or "").strip()
        if not text or text in cleaned:
            continue
        path = (project_path / text).resolve()
        if not path.is_relative_to(project_path) or not path.is_file():
            raise ValueError("只读上下文文件需要是项目文件夹内已存在的文件：" + text)
        cleaned.append(text)
    return cleaned


@task_history.serialized
def submit_task(body, trusted=None, authority=None, request_id=''):
    prompt = str(body.get("prompt", "")).strip()
    if not prompt or len(prompt) > 50000:
        raise ValueError("请填写任务，最多 50000 个字符")
    kind = body.get("kind", "chat")
    if kind not in {"chat", "edit"}:
        raise ValueError("任务类型不正确")
    settings = workflow_settings.snapshot()
    body = dict(body)
    # The main-brain broker passes its immutable context internally.  A model
    # tool body can never provide this argument, and the trusted branch refuses
    # any attempt to override project/owner/role/executor here.
    trusted_context = None
    brain_request_id = ''
    brain_authority = authority
    if trusted is not None:
        if not settings.get('enabled') or not settings.get('readable', True):
            raise ValueError('协作总开关已关闭，主脑接口不会派发新任务或整改')
        import brain_context
        try:
            brain_context.require_operation(trusted, 'dispatch')
            brain_request_id = brain_context.clean_request_id(request_id or '')
        except brain_context.ContextError as error:
            raise ValueError(str(error)) from None
        trusted_context = trusted
        if body.get('project') and project_scope.project_key(body['project']) != trusted.project_key:
            raise ValueError('主脑接口不能通过任务参数切换项目')
        for field in ('owner', 'actor', 'role', 'host', 'session', 'brain',
                      'brain_owner', 'session_binding'):
            if field in body:
                raise ValueError('主脑接口不能通过任务参数覆盖受信身份：' + field)
        if execution_engines.requested_engine(body) is not None:
            raise ValueError('主脑接口不能指定执行器；请使用模型已保存的默认 Agent')
        if body.get('stage_id') or body.get('goal_id'):
            raise ValueError('阶段 A 主脑接口不提供阶段/目标派发')
        body['project'] = trusted.project
        body['source'] = 'brain'
        if brain_authority is None:
            brain_authority = brain_context.issue_authority(trusted)
        if not brain_context.authority_matches(brain_authority, trusted.owner,
                                                trusted.project, trusted.binding_key):
            raise ValueError('连接授权与主脑责任会话不匹配')
    # Only a real task override is explicit.  The global default is never copied
    # into the body here; otherwise a form/CLI would turn inheritance into a
    # permanent per-task override and hide the model preference.
    explicit_engine = execution_engines.requested_engine(body)
    engine = explicit_engine
    continue_id = str(body.get("continue_task") or "").strip()
    rework_id = str(body.get("rework_of") or "").strip()
    reuse_session = bool(body.get("reuse_session"))
    if continue_id and rework_id:
        raise ValueError("整改不能同时作为手动续接")
    if reuse_session and not (rework_id or continue_id):
        raise ValueError("reuse_session 只能用于整改或明确的续接")
    rework_parent = None
    rework_run = None
    if rework_id:
        rework_run = registry.task_dir(rework_id)
        if not rework_run.is_dir():
            raise ValueError("整改原任务不存在")
        record = delegation_policy.load(rework_run)
        if record is None:
            raise ValueError("这个任务没有验收记录，不能派发整改")
        if record.get("state") != delegation_policy.RETURNED:
            raise ValueError("任务当前不是退回整改状态，不能派发整改")
        if (record.get("rework") or {}).get("dispatched_task_id"):
            raise ValueError("整改已经派发为任务 " + str(record["rework"]["dispatched_task_id"]))
        if body.get("route_mode") == "auto":
            raise ValueError("整改保持原模型，不能自动重新路由")
        rework_parent = json.loads((rework_run / "task.json").read_text(encoding="utf-8"))
        owned, owner, parent_project = delegation_policy.brain_identity(rework_parent)
        if trusted_context is not None and not owned:
            raise ValueError('主脑接口不能收养没有主脑归属的旧任务；请创建新的主脑任务')
        if owned:
            # Phase A: a brain-owned parent may only be reworked by a trusted
            # same-owner/same-project connection.  A plain panel/CLI request must
            # not adopt it, even if it knows the task id.
            if trusted_context is None:
                raise ValueError('这是主脑任务；普通面板/CLI 不能续接或整改，也不会自动收养。请使用主脑接口')
            if (not owner or owner != trusted_context.owner or
                    (rework_parent.get('brain') or {}).get('binding_key') != trusted_context.binding_key or
                    (parent_project and project_scope.project_key(parent_project) != trusted_context.project_key)):
                raise ValueError('主脑任务属于另一个拥有者或项目；拒绝跨拥有者整改')
        if explicit_engine is not None and explicit_engine != rework_parent.get('engine'):
            raise ValueError('整改保持原执行 Agent；切换 Agent 请创建新任务')
        for field in ('stage_id', 'goal_id'):
            if body.get(field) and body.get(field) != rework_parent.get(field):
                raise ValueError('整改不能更换原目标或阶段')
        rework_routing = dict(rework_parent.get('route', {}).get('requirements', {}))
        if isinstance(body.get('routing'), dict):
            rework_routing.update(body['routing'])
        rework_routing['acceptance'] = [criterion['text'] for criterion in record.get('criteria', [])]
        body = {**body, "engine": rework_parent.get("engine") or "dsh",
                "kind": rework_parent.get("kind") or "edit",
                "model_id": rework_parent["model_id"],
                "project": rework_parent.get("project") or body.get("project") or "",
                "files": rework_parent.get("files") or [],
                "route_mode": "manual", 'routing': rework_routing,
                'stage_id': rework_parent.get('stage_id'), 'goal_id': rework_parent.get('goal_id')}
        kind = body["kind"]
        engine = body["engine"]
        if reuse_session:
            # Explicit exception only; the default corrective round is a new session.
            continue_id = rework_id
    routing_body = dict(body)
    if continue_id:
        original = json.loads((registry.task_dir(continue_id) / 'task.json').read_text(encoding='utf-8'))
        owned, owner, parent_project = delegation_policy.brain_identity(original)
        if trusted_context is not None and not owned:
            raise ValueError('主脑接口不能收养没有主脑归属的旧任务；请创建新的主脑任务')
        if owned:
            if trusted_context is None:
                raise ValueError('这是主脑任务；普通面板/CLI 不能续接，也不会自动收养。请使用主脑接口')
            if (not owner or owner != trusted_context.owner or
                    (original.get('brain') or {}).get('binding_key') != trusted_context.binding_key or
                    (parent_project and project_scope.project_key(parent_project) != trusted_context.project_key)):
                raise ValueError('主脑任务属于另一个拥有者或项目；拒绝跨拥有者续接')
        if explicit_engine is not None and explicit_engine != original.get('engine'):
            raise ValueError('继续会话不能切换执行 Agent；请创建新任务')
        engine = original.get('engine') or 'dsh'
        if body.get('route_mode') == 'auto':
            raise ValueError('继续会话保持原模型；升级模型请创建新任务')
        routing_body['model_id'] = body.get('model_id') or original['model_id']
        # A continuation keeps the original engine snapshot even if the model's
        # preference or the global default changed after the parent was created.
        routing_body['engine'] = engine
    elif routing_body.get('route_mode', 'manual') == 'manual' and not body.get('model_id'):
        routing_body['model_id'] = registry.list_models()['default_model']
    routing_data = deepseek_billing.enrich_models(registry.list_models())
    daily_cost = budget_relay.daily_state(settings)
    route = model_router.choose(routing_body, routing_data['models'], settings, public_tasks(),
                                daily_cost['remaining_cny'], daily_uncertain=daily_cost.get('unresolved', False))
    # The router resolves explicit > model preference > global per final model.
    # Use that exact engine for every following capability/project check.
    engine = route['engine']
    model = registry.get_model(route['model_id'])
    if engine not in execution_engines.ENGINES or (engine == 'direct' and kind != 'chat') or (engine == 'aider' and kind != 'edit'):
        raise ValueError('执行器与任务类型不匹配')
    if engine in execution_engines.AGENTS and not execution_engines.availability(engine)['ready']:
        raise ValueError(execution_engines.LABELS[engine] + ' 执行器未就绪；不会切换到其他执行器')
    body['engine'] = engine
    native_connection = None
    native_tool_mode = None
    native_tool_revision = None
    native_access_mode = None
    zcode_snapshot = {}
    if engine == 'zcode':
        import zcode_worker, zcode_tools
        zcode_worker.validate_task(body, settings)
        native_parent=rework_parent or (original if continue_id else None)
        zcode_snapshot = ({key: native_parent.get(key) for key in ('zcode_connection', 'zcode_selection', 'zcode_tool_revision', 'zcode_access_mode')}
                          if native_parent else {'zcode_connection': zcode_worker.configuration(),
                            'zcode_selection': model['native_selection'], 'zcode_tool_revision': zcode_tools.revision(),
                            'zcode_access_mode': zcode_worker.configuration()['access_mode']})
        if native_parent and native_parent.get('zcode_tool_revision')!=zcode_tools.revision():raise ValueError('原 ZCode 任务保护版本已变化，请明确新建任务；不自动升级旧任务权限')
    if engine == 'workbuddy':
        import workbuddy_worker
        workbuddy_worker.validate_task(body, settings)
        native_connection = workbuddy_worker.configuration()
        import workbuddy_native_tools
        native_tool_mode = workbuddy_native_tools.mode(rework_parent) if rework_parent else workbuddy_native_tools.MODE
        native_tool_revision = (rework_parent.get('workbuddy_tool_revision') if rework_parent else workbuddy_native_tools.revision())
        native_access_mode = (rework_parent.get('workbuddy_access_mode', 'project') if rework_parent
                              else workbuddy_native_tools.access_mode())
        if native_tool_mode == workbuddy_native_tools.LEGACY and body.get('test_command'):
            raise ValueError('旧 WorkBuddy 任务的整改不能新增终端权限')
    thinking_config.validate(model)
    # A stale failed connection check must not permanently exclude a capable
    # local model: recheck reachability read-only (no inference) before the real
    # dispatch.  Model/paid/complexity/identity gates above stay authoritative.
    connectivity = connectivity_gate(model)
    if connectivity['blocked']:
        raise ValueError(connectivity['message'])
    if engine == 'aider' and model.get('thinking_level', 'default') != 'default':
        raise ValueError('可调思考等级目前适用于 DSH 与直接咨询，Aider 请使用默认等级')
    timeout = task_timing.normalize(body.get('timeout',0))
    # Optional task-level per-request output goal for the main brain.  It records
    # a request target only; the model's configured max_output preset is untouched.
    request_output_tokens = body.get("request_output_tokens")
    if request_output_tokens in (None, ""):
        request_output_tokens = None
    elif isinstance(request_output_tokens, bool) or not isinstance(request_output_tokens, int) or not 1 <= request_output_tokens <= 131072:
        raise ValueError("单请求输出额度需要是 1 到 131072 的整数")
    project = ""
    files = []
    readonly_files = []
    if kind == "chat" and engine in execution_engines.AGENTS:
        # A chat task may carry read-only context files for the code index and
        # the handover file scope.  They are never modifiable files: the read-only
        # permission comes from kind/engine sandbox, and ``readonly_files`` keeps
        # the distinction visible in the record and the UI.  Requested context
        # files are never silently dropped.
        requested = body.get("readonly_files") or body.get("files") or []
        if body.get("project"):
            project_path = Path(str(body["project"]))
            if not project_path.is_absolute() or not project_path.is_dir():
                raise ValueError("项目文件夹需要是已存在的绝对路径")
            project = str(project_path.resolve())
            readonly_files = _clean_readonly_files(project_path, requested)
            files = list(readonly_files)
        elif requested:
            raise ValueError("只读上下文文件需要与项目文件夹一起提供")
    if kind == "edit":
        project_path = Path(str(body.get("project", ""))).resolve()
        if not body.get("project") or not project_path.is_dir():
            raise ValueError("请输入笔记本上存在的项目文件夹绝对路径")
        if not Path(body["project"]).is_absolute():
            raise ValueError("项目文件夹需要使用绝对路径")
        project = str(project_path)
        files = body.get("files", [])
        if not isinstance(files, list) or not 1 <= len(files) <= 30 or any(not isinstance(f, str) or not f.strip() for f in files):
            raise ValueError("请填写 1 到 30 个待修改文件，每行一个")
        for name in files:
            path = (project_path / name).resolve()
            if not path.is_relative_to(project_path) or (path.exists() and not path.is_file()):
                raise ValueError("待修改文件需要位于项目文件夹内")
        # The workbench record directory is control-plane evidence and is never a
        # writable dispatch target, even when a caller names it explicitly.
        record_protection.validate_files(files, project_path)
        running = [t for t in public_tasks()
                   if t.get('project') == project and t.get('status') == 'running']
        # The existing single-worker executor safely queues B behind A. Only
        # this controller's in-process ACTIVE map proves that a running task is
        # already owned by that queue. Unmanaged work must still block edits.
        with ACTIVE_LOCK:
            unmanaged = any(t.get('id') not in ACTIVE for t in running)
        if unmanaged:
            raise DispatchRefused('这个项目有不属于当前工作台队列的运行任务，请等待结束后再分派；未创建新任务')
    parent = None
    if continue_id:
        parent = json.loads((registry.task_dir(continue_id) / "task.json").read_text(encoding="utf-8"))
        if not execution_engines.has_session(parent) or parent['status'] in {'queued', 'running'}:
            raise ValueError('请选择可继续的已结束执行任务；ZCode 需要正常完成且会话关闭已确认')
        if engine != parent.get('engine') or model["id"] != parent["model_id"] or kind != parent["kind"]:
            raise ValueError("继续会话需要保持原模型、执行器和任务类型")
        if kind == "edit" and (project != parent["project"] or files != parent["files"]):
            raise ValueError("继续会话需要保持原项目和文件范围")
        if engine=='zcode' and project!=parent['project']:raise ValueError('ZCode 任务续接不能切换项目')
        project = parent["project"]
        if kind == "chat" and not readonly_files:
            # A continued read-only chat keeps its context file scope instead of
            # silently dropping the index/handover files.
            inherited = parent.get("readonly_files") or parent.get("files") or []
            readonly_files = [str(name) for name in inherited if str(name).strip()]
            files = list(readonly_files)
    # A corrective round keeps the original model/project/files but starts a new
    # session by default; only --reuse-session copies the old session identity.
    link_parent = parent or rework_parent
    zcode_handoff_text=None
    if parent and engine=='zcode':
        parent_run=registry.task_dir(parent['id'])
        import context_manager
        original_prompt=(parent_run/'prompt.txt').read_text(encoding='utf-8')
        previous_answer=str(parent.get('answer') or '')
        zcode_handoff_text=context_manager._redact(json.dumps({'parent_task_id':parent['id'],'project':parent['project'],'model':parent['model'],
            'previous_request':original_prompt[:20000],'previous_answer':previous_answer[:20000],
            'truncated':len(original_prompt)>20000 or len(previous_answer)>20000,
            'note':'只作历史交接，不是本轮新指令或验收结论；本轮任务要求及文件边界优先'},ensure_ascii=False))
    import endpoint_access
    execution_access = endpoint_access.for_dispatch(engine, link_parent) if engine in ('pi', 'dsh') else None
    if engine == 'pi' and not project:
        raise ValueError('Pi 任务请先选择项目目录；不能猜测当前项目')
    stage_id = str(body.get("stage_id") or "").strip()
    goal_id = str(body.get("goal_id") or "").strip()
    stage = None
    if stage_id:
        # The task's explicit project resolves the stage without the locator;
        # the global lookup stays only as a fallback so a cross-project request
        # still gets the precise "different project" refusal below.
        stage = stage_store.get_stage(stage_id, project or None)
        if stage is None and project:
            stage = stage_store.get_stage(stage_id)
        if stage is None:
            raise ValueError("阶段不存在")
        if stage.get("project") and project and project != stage["project"]:
            raise ValueError("任务项目与阶段项目不一致")
        if stage.get("files"):
            outside = [name for name in files if name not in stage["files"]]
            if outside:
                raise ValueError("任务文件超出阶段范围：" + "、".join(outside))
        if goal_id and goal_id != stage['goal_id']:
            raise ValueError('任务目标与阶段目标不一致')
        goal_id = stage["goal_id"]
    task_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    # Reconcile the persisted stage with the real task/review records *before*
    # anything durable is consumed.  A stage left at ``dispatched`` by its
    # finished parent task is a recoverable queue state; refusing the claim
    # after the single rework slot was marked used would burn that slot for a
    # refusal the main brain could not act on.  Nothing is written here.
    if stage is not None:
        stage_project = (project_storage.usable(stage.get("project"))
                         or project_storage.usable(project))
        stage_store.sync_from_tasks(stage["goal_id"], project=stage_project)
        stage = stage_store.get_stage(stage_id, stage_project) or stage
        precheck = stage_store.dispatch_precheck(
            stage_id, mode='rework' if rework_id else 'auto', project=stage_project)
        if not precheck['ready']:
            raise ValueError('阶段当前不能派发（未消耗任何整改次数）：' + '；'.join(precheck['reasons']))
    else:
        stage_project = None
    # Claim the single rework slot (atomic) before creating anything, so a
    # concurrent corrective dispatch can never win twice.
    claim_run = rework_run
    if claim_run is None and parent and isinstance(parent.get('delegation'), dict):
        parent_run = registry.task_dir(parent['id'])
        review = delegation_policy.load(parent_run)
        if review and review['state'] == delegation_policy.RETURNED:
            claim_run = parent_run
    marked_rework = False
    if claim_run is not None:
        delegation_policy.mark_rework_dispatched(claim_run, task_id, authority=brain_authority,
                                                request_id=brain_request_id or None)
        marked_rework = True
    try:
        # Stage claim is also atomic and enforces prerequisite acceptance.
        if stage is not None:
            stage_store.claim_dispatch(stage_id, task_id, mode='rework' if rework_id else 'auto',
                                       project=stage_project)
        if project:
            # Explicit first write for this project: it now owns its records.db/runs.
            run = registry.prepare_task_dir(task_id, project)
        else:
            run = registry.task_dir(task_id)
            run.mkdir(parents=True)
    except Exception as error:
        # The worker never started, so the one automatic rework slot must stay
        # usable: release only the un-started dispatch marker (the rework count,
        # the review state and every acceptance/cancel guard stay untouched).
        if marked_rework:
            try:
                delegation_policy.release_rework_dispatch(claim_run, task_id, reason=str(error),
                                                          authority=brain_authority)
            except (OSError, ValueError):
                pass
        raise
    (run / "prompt.txt").write_text(prompt, encoding="utf-8")
    brain_meta = None
    if trusted_context is not None:
        brain_meta = {
            'schema': 1,
            'owner': trusted_context.owner,
            'binding_key': trusted_context.binding_key,
            'project': trusted_context.project,
            'host': trusted_context.host,
            'role': trusted_context.role,
            'session': {
                'binding': trusted_context.session_binding,
                'reference': trusted_context.session_reference,
                'operator_bound': bool(trusted_context.operator_session),
                'native': trusted_context.native_session.public(),
            },
            'request_id': brain_request_id,
            'adopted': False,
            'declared_at': trusted_context.declared_at,
            'host_version': trusted_context.host_version.public(),
            'model_identity': trusted_context.model.public(),
        }
    first_fields = {
        **zcode_snapshot,
        'id': task_id, 'title': str(body.get("title") or prompt.splitlines()[0])[:100],
        'kind': kind, 'engine': engine, 'model_id': model["id"], 'model_name': model["name"],
        'model': model["model"], 'status': "queued", 'phase': "等待执行",
        'source': 'brain' if trusted_context is not None else (
            "codex" if body.get("source") == "codex" else "panel"),
        'project': project, 'files': files, 'created_at': registry.now(),
        'readonly_files': (readonly_files or None), 'files_readonly': bool(readonly_files),
        'timeout': timeout, 'test_command': str(body.get("test_command", "")).strip() or None,
        'stage_id': stage_id or None, 'goal_id': goal_id or None,
        'request_output_tokens': request_output_tokens,
        'workbuddy_connection': native_connection,
        'workbuddy_tool_mode': native_tool_mode, 'workbuddy_tool_revision': native_tool_revision,
        'workbuddy_access_mode': native_access_mode,
        'execution_access_mode': execution_access,
        'connectivity_check': {'source': 'pre_dispatch',
                               'rechecked': bool(connectivity['checked']),
                               'blocked': bool(connectivity['blocked']),
                               'message': connectivity['message'],
                               'verdict': connectivity['verdict'],
                               'check': connectivity['check']},
    }
    if brain_meta is not None:
        # Public owner/request metadata is persisted before the task is queued.
        first_fields.update({'owner': trusted_context.owner,
                             'request_id': brain_request_id, 'brain': brain_meta})
    row = registry.patch_task(run, **first_fields)
    row = registry.patch_task(run, route=route, model_calls=0, budget_reserved_cny=0,
                              cost_settled_cny=0, cost_settled_calls=0, cost_unsettled_cny=0,
                              cost_unknown_calls=0, cost_legacy_unsettled_cny=0, cost_requests={}, cost_schema=2)
    delegation = None
    link_delegated = bool(link_parent and isinstance(link_parent.get("delegation"), dict))
    if engine in execution_engines.AGENTS and (route.get("mode") == "auto" or link_delegated
                                               or body.get('source') == 'codex'
                                               or trusted_context is not None):
        inherited = 0
        if link_delegated:
            try:
                inherited = max(0, int(link_parent["delegation"].get("rework_count") or 0))
            except (TypeError, ValueError):
                inherited = 0
        delegation = {"schema": delegation_policy.SCHEMA, "mode": "outcome",
                      "created_at": registry.now(),
                      "max_auto_rework": delegation_policy.MAX_AUTO_REWORK,
                      "rework_count": inherited, "inherited_rework_count": inherited,
                      "parent_task_id": link_parent["id"] if link_parent else None,
                      "brain_owner": trusted_context.owner if trusted_context is not None else None}
    row = registry.patch_task(run, delegation=delegation)
    if parent:
        row = registry.patch_task(run, parent_task_id=parent['id'], **execution_engines.session_fields(parent))
        if engine=='zcode':
            (run/'continuation.txt').write_text(zcode_handoff_text,encoding='utf-8')
            row=registry.patch_task(run,zcode_handoff={'parent_task_id':parent['id'],'sha256':hashlib.sha256(zcode_handoff_text.encode()).hexdigest()},
                session_policy='fresh_with_handoff')
        if parent.get('relay_port'):
            row = registry.patch_task(run, relay_port=parent['relay_port'])
    elif rework_parent:
        # New session on purpose: keep the lineage but do not inherit the session.
        row = registry.patch_task(run, parent_task_id=rework_parent.get("id"),
                                  rework_of=rework_parent.get("id"))
    EXECUTOR.submit(execute_task, task_id)
    return row


def cancel_task(task_id, reason="", actor="main"):
    run = registry.task_dir(task_id)
    state = json.loads((run / "task.json").read_text(encoding="utf-8"))
    if state["status"] not in {"queued", "running"}:
        raise ValueError("任务已经结束")
    (run / "cancelled.flag").touch()
    # A cancellation is a queryable record, never a silent state change.
    registry.patch_task(run, cancel_record={"at": registry.now(), "actor": str(actor)[:40],
                                            "reason": str(reason or "")[:1000]})
    with ACTIVE_LOCK:
        process = ACTIVE.get(task_id)
    if state.get('engine') in ('workbuddy', 'zcode'):
        # The CLI is owned by WorkBuddy's host, not a child of this Python
        # process. The worker observes cancelled.flag and closes its exact lease.
        # Never taskkill the GUI or pretend the native cleanup already finished.
        if state['status'] == 'queued':
            registry.patch_task(run, status='cancelled', ended_at=registry.now())
        else:
            registry.patch_task(run, phase='正在取消原生执行会话')
        return {'ok': True, 'cancellation_pending': state['status'] == 'running',
                'cancel_record': registry.read_task(run).get('cancel_record')}
    if process and process.poll() is None:
        if state.get("engine") in {"dsh", "pi"}:
            # Let acpx send ACP cancellation and flush session persistence first.
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        else:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        process.wait(timeout=10)
    elif state.get("worker_pid"):
        # Only stop a process whose identity is positively ours; a reused PID, a
        # foreign job or an unreadable process table is never killed.
        if _recorded_process_verdict(state["worker_pid"], state) is True:
            subprocess.run(["taskkill", "/PID", str(int(state["worker_pid"])), "/T", "/F"],
                           capture_output=True)
    registry.patch_task(run, status="cancelled", phase="已停止接收" if state["kind"] == "chat" else "已停止执行",
                        ended_at=registry.now())
    return {"ok": True, "cancel_record": registry.read_task(run).get("cancel_record")}


def update_review(task_id, body, authority=None, request_id=''):
    """Record or update an acceptance decision through the delegated policy.

    Executor completion and independent acceptance stay separate; this path
    requires main-mind evidence for acceptance and enforces the rework cap.
    A brain-owned task additionally needs the broker's signed owner authority;
    a plain panel request carrying only ``actor`` is refused by the policy.
    """
    run = registry.task_dir(task_id)
    if not run.is_dir():
        raise ValueError("任务不存在")
    action = str(body.get("action", "")).strip()
    actor = body.get("actor") or "main"
    if authority is not None and getattr(authority, 'owner', None):
        # The broker-validated owner is the audit actor; a model-supplied actor
        # string can never impersonate it.
        actor = 'brain:' + str(authority.owner)
    review_before = [(run / name).read_bytes() if (run / name).exists() else None
                     for name in ('review.json', 'task.json')]
    try:
        if action == "init":
            record = delegation_policy.initialize(run, body.get("criteria"), actor,
                                                 authority=authority, request_id=request_id)
        elif action == "accept":
            record = delegation_policy.accept(run, actor, body.get("criteria") or [], body.get("notes", ""),
                                             authority=authority, request_id=request_id)
        elif action == "final_accept":
            # Independent main-mind intervention result; the executor failure stays.
            record = delegation_policy.final_accept(run, actor, body.get("criteria") or [], body.get("notes", ""),
                                                   authority=authority, request_id=request_id)
        elif action in {"return", "rework"}:
            record = delegation_policy.request_rework(run, actor, body.get("failed_criteria") or [],
                                                      body.get("problems", ""), body.get("boundaries", ""),
                                                      body.get("notes", ""),
                                                      authority=authority, request_id=request_id)
        elif action == "block":
            record = delegation_policy.block(run, actor, body.get("reason", ""),
                                            authority=authority, request_id=request_id)
        else:
            raise ValueError("验收操作不正确")
    except ValueError as error:
        # Only unchanged durable records prove that this was a validation
        # refusal. A later failure may follow a successful write.
        review_after = [(run / name).read_bytes() if (run / name).exists() else None
                        for name in ('review.json', 'task.json')]
        if review_after == review_before:
            raise ReviewRefused(str(error)) from None
        raise
    return {"task_id": task_id, "review": record, "summary": delegation_policy.panel_summary(record)}


# ------------------------------------------------------- goals / stages / handover

def goal_list(view=None):
    project = project_views.explicit_project(view)
    if view is not None and project_views.scope_of(view)[0] == project_scope.MODE_UNBOUND:
        return {"goals": [], "stats": stage_store.stats(), "recovery": [], "note": stage_store.STAGES_NOTE}
    return {"goals": stage_store.list_goals(project), "stats": stage_store.stats(project),
            "recovery": stage_store.pending_recovery(project), "note": stage_store.STAGES_NOTE}


def goal_detail(goal_id, view=None):
    project = project_views.explicit_project(view)
    goal = stage_store.get_goal(goal_id, project)
    goal["recovery"] = [row for row in stage_store.pending_recovery(project) if row["goal_id"] == goal_id]
    goal["log"] = stage_store.log_entries(goal_id=goal_id, project=project)
    return goal


def stage_list(goal_id=None, view=None):
    project = project_views.explicit_project(view)
    if view is not None and project_views.scope_of(view)[0] == project_scope.MODE_UNBOUND:
        return {"stages": [], "stats": stage_store.stats(), "note": stage_store.STAGES_NOTE}
    return {"stages": [stage_store.stage_view(stage["id"], project=project)
                       for stage in stage_store.list_stages(goal_id, project)],
            "stats": stage_store.stats(project), "note": stage_store.STAGES_NOTE}


def create_goal(body):
    # An explicit project via the panel is the first project write: it creates the
    # project store so goals/stages belong to the real directory, not to the
    # installation's global database.
    return stage_store.create_goal(body.get("title"), body.get("objective", ""),
                                   body.get("project", ""), actor=body.get("actor") or "main",
                                   store='project' if body.get("project") else 'auto')


def set_goal_status(goal_id, body, project=None):
    stages = stage_store.list_stages(goal_id, project)
    goal = stage_store.set_goal_status(goal_id, body.get("status"), actor=body.get("actor") or "main",
                                       project=project)
    if goal["status"] == stage_store.GOAL_CANCELLED:
        cancelled = []
        for stage in stages:
            if stage["status"] not in stage_store.ACCEPTED_STATES:
                result = cancel_stage_endpoint(stage["id"], body, project=project)
                if result.get("cancelled_task"):
                    cancelled.append(result["cancelled_task"])
        goal["cancel_requested_tasks"] = cancelled
        goal["cancellation_note"] = "本地阶段和进程已请求取消；远端生成是否立即停止取决于服务端。"
    return goal


def create_stage(body):
    stage_kind = body.get("kind") or stage_store.KIND_DEFAULT
    files = list(body.get("files") or [])
    if stage_kind == stage_store.KIND_EDIT:
        # A stage's persisted file range becomes the writable scope of every
        # task it dispatches; the record directory can never enter that range.
        record_protection.validate_files(files, body.get("project"))
        if not body.get("project"):
            # The usual stage form carries only goal_id. Resolve aliases against
            # that persisted goal's project, never the panel service's cwd.
            goal = stage_store.get_goal(body.get("goal_id") or body.get("goal"))
            if goal:
                record_protection.validate_files(files, goal.get("project"))
    return stage_store.add_stage(body.get("goal_id") or body.get("goal"), body.get("title"),
                                 body.get("objective", ""), depends_on=body.get("depends_on") or [],
                                 files=files, acceptance=body.get("acceptance") or [],
                                 candidates=body.get("candidates") or [], fixed_model=body.get("fixed_model", ""),
                                 project=body.get("project"),
                                 session_policy=body.get("session_policy") or stage_store.SESSION_NEW,
                                 kind=body.get("kind") or stage_store.KIND_DEFAULT,
                                 actor=body.get("actor") or "main")


def stage_dispatch_policy(stage, body):
    """Authoritative acceptance/candidate policy for one stage dispatch.

    The stage store is the single source of truth: a caller-supplied routing
    dict can never replace the persisted acceptance conditions, and automatic
    candidates can only come from the persisted stage candidates.  An explicit
    manual ``model_id`` stays allowed, but it is an explicit, recorded exception
    instead of a silent expansion of the automatic candidate set.
    """
    request = body.get("routing") if isinstance(body.get("routing"), dict) else {}
    fixed = str(stage.get("fixed_model") or "").strip()
    explicit = str(body.get("model_id") or "").strip()
    routing = {key: value for key, value in request.items()
               if key not in {"acceptance", "eligible_model_ids"}}
    routing["acceptance"] = list(stage["acceptance"])
    ignored = [key for key in ("acceptance", "eligible_model_ids") if key in request]
    if fixed:
        routing["eligible_model_ids"] = [fixed]
        choice, manual = "stage_fixed_model", ""
        if explicit and explicit != fixed:
            # The stage's fixed model overrides the caller too; keep it visible.
            ignored.append("model_id")
    elif explicit:
        routing["eligible_model_ids"] = [explicit]
        choice, manual = "explicit_manual", explicit
    else:
        candidates = list(stage.get("candidates") or [])
        routing["eligible_model_ids"] = candidates or None
        choice, manual = ("stage_candidates" if candidates else "auto_router"), ""
    routing.setdefault("assessment", "阶段目标：" + stage["objective"][:500])
    policy = {
        "schema": 1,
        "acceptance_source": "stage_store",
        "acceptance_count": len(stage["acceptance"]),
        "candidate_source": choice,
        "candidate_model_ids": list(routing["eligible_model_ids"] or []),
        "manual_model": manual,
        "manual_reason": ("调用者显式指定模型；这是显式例外，验收条件仍取阶段保存值，"
                          "不会自动扩大候选集合" if choice == "explicit_manual" else ""),
        "ignored_request_fields": ignored,
        "note": ("阶段派发以阶段持久保存的验收条件与候选为准；被忽略的调用者字段在此列出，"
                 "不做静默替换或静默扩展。"),
    }
    return routing, policy


def dispatch_stage(stage_id, body, project=None):
    """Dispatch one stage task through the shared queue, routing and authorization."""
    stage = stage_store.get_stage(stage_id, project)
    if stage is None:
        raise ValueError("阶段不存在")
    settings = workflow_settings.snapshot()
    route_mode = str(body.get("route_mode") or ("manual" if stage["fixed_model"] else "auto"))
    explicit_engine = execution_engines.requested_engine(body)
    stage_engine = explicit_engine
    if stage_engine is None and stage["fixed_model"] and not body.get("reuse_session"):
        # The stage-fixed model is the final candidate here, so its saved default
        # (then the global default) is what a new stage task will execute with.
        fixed = registry.get_model(stage["fixed_model"], require_enabled=False)
        stage_engine, _ = execution_engines.resolution({}, settings, model=fixed)
    if stage_engine is not None and stage_engine not in execution_engines.AGENTS:
        raise ValueError('阶段任务请选择 DeepSeek Harness 或 Pi Agent')
    if route_mode == "auto" and not settings.get("enabled"):
        raise ValueError("协作总开关已关闭，不自动派发新阶段")
    # The stage's persisted kind is authoritative.  A request parameter can never
    # promote a read-only chat stage into a full-access edit stage: ``kind`` is
    # dropped from the caller body and replaced with the stored value.
    stage_kind = stage.get("kind") if stage.get("kind") in stage_store.STAGE_KINDS else stage_store.KIND_DEFAULT
    submit = {key: value for key, value in body.items()
              if key not in {"stage_id", "goal_id", "project", "files", "kind", "engine", "continue_task",
                             "routing", "stage_policy", "readonly_files", "model_id"}}
    # A stage-fixed model always wins; an explicit manual model is kept visible.
    explicit_model = str(body.get("model_id") or "").strip()
    if stage["fixed_model"]:
        submit["model_id"] = stage["fixed_model"]
    elif explicit_model:
        submit["model_id"] = explicit_model
    routing, stage_policy = stage_dispatch_policy(stage, body)
    submit.update({"stage_id": stage_id, "goal_id": stage["goal_id"], "kind": stage_kind,
                   "project": stage["project"] or body.get("project") or "",
                   # Chat keeps its configured files as read-only context so the
                   # code index and handover scope are not silently dropped; the
                   # read-only permission itself comes from kind/engine sandbox.
                   "files": list(stage["files"]),
                   "readonly_files": (list(stage["files"]) if stage_kind == stage_store.KIND_CHAT else []),
                   "routing": routing,
                   "route_mode": route_mode})
    if explicit_engine is not None:
        # Only a real caller override is explicit; otherwise submit_task resolves
        # the final model's saved agent before writing the task snapshot.
        submit["engine"] = explicit_engine
    submit.setdefault("title", stage["title"])
    if body.get("reuse_session"):
        # Explicit exception only; default is a new session per stage.  An old
        # session keeps the old task's engine/model/project when reused.
        # Without a deliberate override, reuse follows the original session,
        # not a preference which applies only to new sessions.
        session_engine = explicit_engine
        candidates = [task_id for task_id in reversed(stage["task_ids"])]
        for task_id in candidates:
            try:
                state = json.loads((registry.task_dir(task_id) / "task.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (session_engine is None or state.get('engine') == session_engine) and execution_engines.has_session(state) and state.get("status") not in {"queued", "running"}:
                submit["continue_task"] = task_id
                break
        else:
            raise ValueError("这个阶段还没有可复用的已结束会话")
    # A dependent stage receives its validated prerequisite handovers in the
    # fresh prompt; a missing, mismatched or stale required handover blocks the
    # dispatch instead of silently sending an under-contextualized request.
    if stage["depends_on"]:
        try:
            context = task_handover.prerequisite_prompt(stage_id, project=project or None)
        except task_handover.HandoverError as error:
            raise ValueError(str(error)) from None
        if context:
            submit["prompt"] = (str(submit.get("prompt") or "") + "\n\n" + context).strip()
    row = submit_task(submit)
    # Durable, inspectable record of what the caller tried to change and what the
    # stage store overrode.  Never a silent replacement.
    if isinstance(row, dict) and row.get("id"):
        row = registry.patch_task(registry.task_dir(row["id"]), stage_policy=stage_policy)
    return {"stage": stage_store.stage_view(stage_id, project=project), "task": row, "stage_policy": stage_policy}


def cancel_stage_endpoint(stage_id, body, project=None):
    current = stage_store.get_stage(stage_id, project)
    if current is None:
        raise ValueError("阶段不存在")
    active = current.get("active_task_id")
    reason = body.get("reason", "")
    stage = stage_store.cancel_stage(stage_id, reason, actor=body.get("actor") or "main", project=project)
    cancelled_task = None
    if active:
        try:
            cancel_task(active, reason="阶段取消：" + str(reason)[:200], actor=body.get("actor") or "main")
            cancelled_task = active
        except ValueError:
            cancelled_task = None
    return {"stage": stage, "cancelled_task": cancelled_task}


def requeue_stage_endpoint(stage_id, body, project=None):
    if stage_store.get_stage(stage_id, project) is None:
        raise ValueError("阶段不存在")
    return stage_store.requeue_stage(stage_id, body.get("reason", ""),
                                     actor=body.get("actor") or "main", project=project)


def sync_stages(body=None):
    body = body if isinstance(body, dict) else {}
    goal_id = body.get("goal_id")
    return stage_store.sync_from_tasks(goal_id, project=body.get("project") or None)


def pending_task_recovery(view=None):
    """Inspectable list of tasks awaiting an explicit manual recovery.

    Read-only: it never enqueues anything and never creates a record.
    """
    rows = []
    try:
        entries = list(project_storage.iter_task_dirs(view))
    except (OSError, ValueError):
        return rows
    for entry in entries:
        run = entry['run']
        try:
            state = json.loads((run / "task.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if state.get("status") != "recovery_pending":
            continue
        record = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
        rows.append({"task_id": state.get("id") or run.name, "title": state.get("title"),
                     "kind": state.get("kind"), "engine": state.get("engine"),
                     "model_name": state.get("model_name"), "project": state.get("project"),
                     "goal_id": state.get("goal_id"), "stage_id": state.get("stage_id"),
                     "phase": state.get("phase"), "reason": record.get("reason"),
                     "recovery_kind": record.get("kind"), "at": record.get("at"),
                     "auto_resend": False, "creates_new_record": False,
                     "resume": ("POST /api/tasks/%s/recover" % (state.get("id") or run.name))
                               + "（需 confirm=true；按原任务/模型/项目/会话继续，不创建新记录）"})
    rows.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
    return rows


def recovery_report(view=None):
    project = project_views.explicit_project(view)
    return {"pending": stage_store.pending_recovery(project),
            "tasks": pending_task_recovery(view), "stats": stage_store.stats(project),
            "note": "面板重启后未完成的派发只显示为待恢复；确认前不会重新发送任何请求，也不会自动重发。"}


def _claim_pending_task(run, **fields):
    """Atomic compare-and-set: only a still-pending recovery may be queued once.

    Concurrent confirmed recoveries of the same task race here; exactly one
    winner may transition ``recovery_pending`` -> ``queued`` and therefore
    enqueue, so a simultaneous double click can never submit the task twice.
    """
    path = run / "task.json"
    with registry.file_lock(run / "task.lock"):
        if (run / "cancelled.flag").exists() or not path.is_file():
            return None
        state = registry.read_json(path)
        if state.get("status") != "recovery_pending":
            return None
        state.update(fields)
        state["updated_at"] = registry.now()
        registry.write_json(path, state)
        return state


def _release_stage_claim(stage_id, task_id, actor, project):
    """Rollback for a stage claimed by a recovery whose enqueue then failed."""
    try:
        stage_store.mark_recovery_pending(
            stage_id, "恢复入队失败，释放阶段占位 " + str(task_id), actor=actor, project=project)
    except (OSError, ValueError):
        pass


def recover_task(task_id, body=None, actor="main"):
    """Explicit, scoped manual recovery of one ``recovery_pending`` task.

    Re-runs the *same* task record (same model, project, file scope, session and
    accounting) instead of creating a replacement.  It refuses when the original
    worker is still alive or its state is unreadable, when the task is not
    pending recovery, when the linked stage/goal/dependency/handover admission
    fails, or when another dispatch for the same task/stage is already queued.
    Admission and the state transition are atomic, so a repeated or simultaneous
    click can never enqueue a duplicate.
    """
    body = body if isinstance(body, dict) else {}
    run = registry.task_dir(task_id)
    if not run.is_dir():
        raise ValueError("任务不存在")
    if (run / "cancelled.flag").exists():
        # A durable cancellation stays authoritative; recovery must not revive it.
        raise ValueError("任务已取消，不能恢复；请新建任务")
    state = registry.read_task(run)
    if state.get("status") != "recovery_pending":
        raise ValueError("只有待恢复的任务需要显式恢复；当前状态：" + str(state.get("status")))
    if state.get('engine') in ('workbuddy', 'zcode'):
        import importlib
        importlib.import_module(state['engine']+'_worker').recovery_guard(state)
    review = delegation_policy.load(run)
    if review and review.get("state") in {"accepted", "final_accepted"}:
        raise ValueError("任务已通过独立验收，不能恢复原执行；请新建任务")
    # An explicit recovery reuses the stored model, so it is re-checked against the
    # latest global scope *before* any stage claim or state write.  The original
    # record, session, files and accounting stay untouched.  A record whose model
    # cannot be resolved here is left to the worker-side scope guard instead of
    # changing the existing recovery failure paths.
    recovery_model_id = state.get("model_id")
    if recovery_model_id:
        try:
            recovery_model = registry.get_model(recovery_model_id, require_enabled=False)
        except (OSError, ValueError):
            recovery_model = None
        if recovery_model is not None:
            scope_gate(recovery_model, workflow_settings.snapshot(), task_id=task_id, engine=state.get('engine'))
    if body.get("confirm") is not True:
        raise ValueError("显式恢复需要 confirm=true；未完成的执行不会自动重发")
    verdict = recorded_worker_alive(state)
    if verdict is True:
        raise ValueError("原执行进程仍在运行，已拒绝恢复，避免重复执行")
    if verdict == WORKER_STATE_UNKNOWN:
        # Unknown process state is not proof of death: fail closed and ask for a
        # real identity check instead of risking a duplicate execution.
        raise ValueError("无法确认原执行进程状态（进程表不可读或被拒绝），已拒绝恢复以避免重复执行")
    with ACTIVE_LOCK:
        active = task_id in ACTIVE
    if active:
        raise ValueError("该任务已经在执行队列中，无需恢复")
    stage_id = state.get("stage_id")
    stage_project = state.get("project") or None
    stage_claimed = False
    if stage_id:
        # A linked stage is never silently treated as "unstaged": a missing,
        # unreadable or cross-project stage blocks recovery.
        busy = [row for row in public_tasks()
                if row.get("stage_id") == stage_id and row.get("id") != task_id
                and row.get("status") in {"queued", "running", "recovery_pending"}]
        if busy:
            raise ValueError("该阶段已有未结束的任务（%s），已拒绝恢复以避免重复执行" % busy[0].get("id"))
        try:
            stage = stage_store.get_stage(stage_id, stage_project)
        except (OSError, ValueError) as error:
            raise ValueError("任务关联的阶段记录不可读，拒绝恢复（不能当作无阶段任务）：" + str(error)) from None
        if stage is None:
            raise ValueError("任务关联的阶段不存在，拒绝恢复（不能当作无阶段任务）")
        stage_project = stage_project or (stage.get("project") or None)
        if stage.get("project") and state.get("project") and stage["project"] != state["project"]:
            raise ValueError("任务项目与阶段项目不一致，拒绝恢复")
        if stage.get("goal_id"):
            try:
                goal = stage_store.get_goal(stage["goal_id"], stage_project)
            except (OSError, ValueError) as error:
                raise ValueError("任务关联的阶段目标不可读，拒绝恢复：" + str(error)) from None
            if goal is None:
                raise ValueError("任务关联的阶段目标不存在，拒绝恢复")
            if goal.get("status") in {stage_store.GOAL_CANCELLED, stage_store.GOAL_COMPLETED}:
                raise ValueError("阶段目标已取消或已完成，拒绝恢复；请新建阶段或任务")
        if stage.get("status") in stage_store.ACCEPTED_STATES:
            raise ValueError("阶段已通过独立验收，不能恢复该任务；请新建阶段或任务")
        if stage.get("status") == stage_store.CANCELLED:
            raise ValueError("阶段已取消，不能恢复该任务；请新建阶段或任务（阶段取消记录保持权威）")
        if stage.get("status") in stage_store.ACTIVE_STATES:
            # The linked task is proven pending recovery, so the stage's active
            # marker is stale; mark exactly this stage before releasing it.
            stage_store.mark_recovery_pending(stage_id, "显式恢复待恢复任务 " + task_id,
                                              actor=actor, project=stage_project)
            stage = stage_store.get_stage(stage_id, stage_project)
        if stage is not None and stage.get("status") in {
                stage_store.RECOVERY_PENDING, stage_store.FAILED,
                stage_store.BLOCKED, stage_store.EXECUTOR_COMPLETED}:
            stage_store.requeue_stage(stage_id, "面板显式恢复待恢复任务 " + task_id,
                                      actor=actor, project=stage_project)
        # A dependent stage needs its validated prerequisite handovers; a
        # missing/stale handover blocks the recovery instead of re-sending.
        if stage is not None and stage.get("depends_on"):
            try:
                task_handover.prerequisite_prompt(stage_id, project=stage_project or None)
            except task_handover.HandoverError as error:
                raise ValueError("前置阶段成果交接无效，拒绝恢复：" + str(error)) from None
        # Atomic claim: another job can never be admitted alongside the recovered
        # task in the same stage, and prerequisite acceptance is re-checked.
        stage_store.claim_dispatch(stage_id, task_id, actor=actor, project=stage_project)
        stage_claimed = True
    previous = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    claimed = _claim_pending_task(
        run, status="queued", phase="等待恢复执行",
        recovery={**previous, "resumed_at": registry.now(), "resumed_by": str(actor)[:40],
                  "confirmed": True, "auto_resend": False,
                  "note": "按原任务记录继续；完成情况与工具副作用未知，请核对改动后再验收。"},
        started_at=None, ended_at=None, error=None)
    if claimed is None:
        # Another confirmed recovery won the race (or the record/cancel changed).
        if stage_claimed:
            _release_stage_claim(stage_id, task_id, actor, stage_project)
        raise ValueError("任务状态已变化（可能已被另一次恢复请求接管），本次未重复排队")
    try:
        EXECUTOR.submit(execute_task, task_id)
    except Exception as error:
        try:
            registry.patch_task(run, status="recovery_pending",
                                phase="待恢复（恢复入队失败，未重复执行）",
                                recovery={**previous, "confirmed": False,
                                          "submit_error": str(error)[:300]})
        except Exception:
            pass
        if stage_claimed:
            _release_stage_claim(stage_id, task_id, actor, stage_project)
        raise
    return {"ok": True, "task_id": task_id, "status": "queued", "creates_new_record": False,
            "stage_id": stage_id,
            "note": "同一个任务记录已重新排队执行；未创建新记录，也未自动接受任何成果。"}


def workbench_databases(view=None):
    """Every workbench records database, limited to the request's real scope.

    ``project`` reads exactly that project's store (read-only; an uninitialized
    project is reported as such and never created).  ``all`` is the explicit
    machine-wide aggregate.  ``unbound``/no view reports only the legacy global
    store, so a request that never named a project cannot look like it acted on
    every project.
    """
    mode, project = (project_views.scope_of(view) if view is not None
                     else (project_scope.MODE_ALL, None))
    rows = []
    if mode == project_scope.MODE_PROJECT:
        state = project_storage.store_state(project)
        row = {'path': state['records_db'], 'project': state['project'],
               'scope': 'project', 'initialized': state['initialized'],
               'exists': state['records_db_exists'], 'writable': state['writable'],
               'linked': state['linked'], 'reason': state['reason']}
        space = state['space']
        if space:
            row.update(space)
        rows.append(row)
    else:
        paths = []
        if mode == project_scope.MODE_ALL:
            paths += [project_storage.records_db(item)
                      for item in project_storage.indexed_projects(require_store=False)]
        legacy = registry.HERE / stage_store.DB_NAME
        if legacy.is_file():
            paths.append(legacy)
        for path in paths:
            space = project_storage.db_space(path)
            if space:
                rows.append({'path': str(path),
                             'scope': 'legacy' if path == legacy else 'project',
                             'initialized': True, 'exists': True, **space})
    return rows


def database_report(view=None):
    mode, project = (project_views.scope_of(view) if view is not None
                     else (project_scope.MODE_ALL, None))
    return {'scope': mode, 'project': project if mode == project_scope.MODE_PROJECT else None,
            'databases': workbench_databases(view),
            'note': ('freelist_bytes=page_size×freelist_count 是 SQLite 真实可回收空间；'
                     '记录数与可回收空间是两件事，不删除记录也不伪装释放。'
                     '未绑定项目时只报告旧全局库，不会顺手读取或压缩所有项目。')}


def _project_active_tasks(project):
    """Queued/running/dispatching task records inside one explicit project."""
    active = []
    for entry in project_storage.iter_task_dirs({'mode': 'project', 'project': project}):
        try:
            row = registry.read_task(entry['run'])
        except (OSError, ValueError, FileNotFoundError):
            continue
        if row.get('status') in {'queued', 'running', 'dispatching'}:
            active.append(entry['task_id'])
    return active


def compact_databases(view=None):
    """Idle-only VACUUM entry with real page-level feedback; never deletes records.

    The scope is enforced before anything is written: a project request compacts
    exactly that project, ``all`` is the explicit machine-wide aggregate, and an
    unbound request is refused instead of silently compacting every project.
    """
    mode, project = (project_views.scope_of(view) if view is not None
                     else (project_scope.MODE_UNBOUND, None))
    if mode == project_scope.MODE_UNBOUND:
        raise ValueError('请先明确选择要压缩的项目（或显式选择全部项目）；未绑定不会静默压缩所有数据库')
    targets = []
    if mode == project_scope.MODE_PROJECT:
        state = project_storage.store_state(project)
        if state['linked']:
            raise ValueError('项目记录目录是链接，拒绝压缩')
        if not state['records_db_exists']:
            return {'scope': 'project', 'project': state['project'], 'compacted': [],
                    'reclaimed_bytes': 0, 'initialized': state['initialized'],
                    'message': state['reason'] or '该项目尚无 records.db，无需压缩',
                    'note': '未初始化项目不会被创建，也不会压缩任何其他项目。'}
        if not state['writable']:
            raise ValueError('项目记录库不可写，拒绝压缩：' + state['records_db'])
        active = _project_active_tasks(state['project'])
        if active:
            raise ValueError('该项目仍有未结束任务，拒绝压缩：' + '、'.join(active))
        targets.append(project_storage.records_db(state['project']))
    else:
        legacy = registry.HERE / stage_store.DB_NAME
        if legacy.is_file():
            targets.append(legacy)
        for item in project_storage.indexed_projects(require_store=True):
            db = project_storage.records_db(item)
            if db.is_file():
                targets.append(db)
    results = []
    if targets:
        # A real cross-process/process-tree idle proof before any VACUUM; a
        # project-scoped request additionally proved its own queue is empty.
        history_idle_guard()
        for path in targets:
            results.append(project_storage.compact_db(path))
    return {'scope': mode, 'project': project if mode == project_scope.MODE_PROJECT else None,
            'compacted': results,
            'reclaimed_bytes': sum(row.get('reclaimed_bytes', 0) for row in results),
            'note': '仅在执行队列空闲时压缩；不删除记录，费用总账与项目文件不在这些库内。'}


def handover_view(task_id):
    run = registry.task_dir(task_id)
    if not run.is_dir():
        raise ValueError("任务不存在")
    record, error = task_handover.safe_load(run)
    if record is None:
        return {"task_id": task_id, "handover": None,
                "message": error or "尚无成果交接；仅在独立验收通过后可创建。"}
    return {"task_id": task_id, "handover": record, "freshness": task_handover.verify(run, record)}


def write_handover(task_id, body):
    run = registry.task_dir(task_id)
    if not run.is_dir():
        raise ValueError("任务不存在")
    record = task_handover.create(run, summary=body.get("summary", ""),
                                  interfaces=body.get("interfaces") or [],
                                  decisions=body.get("decisions") or [],
                                  remaining_issues=body.get("remaining_issues") or [],
                                  test_evidence=body.get("test_evidence") or [],
                                  context=body.get("context") or [],
                                  files=body.get("files"),
                                  actor=body.get("actor") or "main",
                                  stage_id=body.get("stage_id"), goal_id=body.get("goal_id"))
    return {"task_id": task_id, "handover": record, "freshness": task_handover.verify(run, record)}


def reference_handover(task_id, body):
    run = registry.task_dir(task_id)
    if not run.is_dir():
        raise ValueError("任务不存在")
    source = body.get("source_task") or body.get("source_task_id")
    if not source:
        raise ValueError("请用 source_task 指定来源任务")
    return task_handover.resolve_reference(run, source,
                                           consumer_stage_id=body.get("consumer_stage") or body.get("stage_id"),
                                           source_stage_id=body.get("source_stage"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, data, code=200):
        if code < 400:
            data=project_views.project_response(urlparse(self.path).path,data,getattr(self,'project_view',None))
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def guard(self, mutation=False):
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in allowed:
            raise PermissionError("仅允许本机访问")
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + item for item in allowed}:
            raise PermissionError("请求来源不正确")
        if mutation and not secrets.compare_digest(self.headers.get("X-Panel-Token", ""), TOKEN):
            raise PanelSessionExpired("请刷新面板后重试")
        # Missing HTTP scope is unbound, never the internal legacy/global view.
        self.project_view=(project_scope.decode_view_header(self.headers.get(project_views.HEADER))
                           or project_scope.parse_view(None))
        project_views.check_request(urlparse(self.path).path,self.project_view)

    def do_GET(self):
        try:
            self.guard()
            path = urlparse(self.path).path
            if path == "/api/session":
                return self.send_json({"token": TOKEN})
            if path == "/api/state":
                settings = workflow_settings.snapshot()
                models = registry.list_models()
                enriched = thinking_config.enrich(deepseek_billing.enrich_models(models))
                enriched['models'] = model_agent_defaults.public_rows(enriched.get('models', []), settings)
                import execution_endpoints
                agent_rows = execution_engines.public_agents()
                endpoint_rows = execution_endpoints.public_rows(enriched['models'], settings, agent_rows)
                return self.send_json({**enriched, "tasks": panel_snapshot_tasks(self.project_view), "server_time": registry.now(),
                                      "workspace": str(HERE.parent), "execution": "任务按顺序执行", "agent": execution_engines.public_selection(settings)[1],
                                      "execution_agents": agent_rows,
                                      "execution_endpoints": endpoint_rows,
                                      'execution_activity':execution_activity(),
                                      'workflow': {**settings, 'daily': public_daily_cost(settings)}, 'codex_usage': codex_usage.usage_state(),
                                      'host_account': host_balance.state(),
                                      'delegation': delegation_policy.aggregate(),
                                      'executor_capabilities': subagent_capabilities(),
                                      'stages': {'stats': stage_store.stats(), 'recovery': stage_store.pending_recovery()}})
            if path == '/api/executor/capabilities':
                return self.send_json(subagent_capabilities())
            if path == '/api/tree':
                return self.send_json(tree_view(self.project_view))
            if path == '/api/observability':
                return self.send_json(observability_batch(view=self.project_view))
            if path == '/api/projects':
                return self.send_json({'projects':project_views.project_catalog(public_tasks()),'recent_projects':project_views.project_bookmarks()})
            if path == '/api/task-records':
                return self.send_json(task_history.listing(self.project_view))
            if path == '/api/db/space':
                return self.send_json(database_report(self.project_view))
            if re.fullmatch(r'/api/projects/pick/[0-9a-f]{32}', path):
                return self.send_json(folder_picker.JOBS.get(path.rsplit('/', 1)[1]))
            if path == '/api/reviews':
                return self.send_json(delegation_policy.review_index())
            if path == '/api/goals':
                return self.send_json(goal_list(self.project_view))
            if path == '/api/stages':
                return self.send_json(stage_list(view=self.project_view))
            if path == '/api/recovery':
                return self.send_json(recovery_report(self.project_view))
            if path.startswith('/api/goals/'):
                parts = path.split('/')
                if len(parts) == 4 and parts[3]:
                    return self.send_json(goal_detail(parts[3], self.project_view))
            if path.startswith('/api/stages/'):
                parts = path.split('/')
                if len(parts) == 4 and parts[3]:
                    return self.send_json(stage_store.stage_view(
                        parts[3], project=project_views.explicit_project(self.project_view)))
            if path == '/api/workflow':
                settings = workflow_settings.snapshot()
                return self.send_json({**settings, 'daily': public_daily_cost(settings)})
            if path == '/api/codex-usage':
                return self.send_json(codex_usage.usage_state())
            if path == '/api/host-account':
                return self.send_json(host_balance.state())
            if path.startswith("/api/tasks/"):
                parts = path.split("/")
                run = registry.task_dir(parts[3], project_views.explicit_project(self.project_view))
                if not run.is_dir():
                    return self.send_json({"error": "任务不存在"}, 404)
                if len(parts) == 5 and parts[4] == "observability":
                    return self.send_json(task_observability(parts[3]))
                if len(parts) == 5 and parts[4] == "log":
                    log = next((run / name for name in ('pi.log','dsh.log','aider.log') if (run / name).exists()), run / 'answer.txt')
                    if not log.exists():
                        log = run / "answer.txt"
                    if not log.exists():
                        log = run / "launcher.log"
                    text = ""
                    if log.exists():
                        with log.open("rb") as handle:
                            handle.seek(max(0, log.stat().st_size - 50000))
                            text = handle.read().decode("utf-8", "replace")
                    state_file = run / "task.json"
                    if state_file.exists():
                        state = json.loads(state_file.read_text(encoding="utf-8"))
                        if state.get("kind") == "chat" and not text:
                            text = state.get("answer", "")
                        try:
                            key = registry.get_model(state["model_id"], False)["api_key"]
                            if key:
                                text = text.replace(key, "[已隐藏]")
                        except ValueError:
                            pass
                    return self.send_json({"text": text})
                if len(parts) == 5 and parts[4] == "review":
                    record, error = delegation_policy.safe_load(run)
                    payload = {"task_id": parts[3], "review": record}
                    if record is not None:
                        payload["summary"] = delegation_policy.panel_summary(record)
                    elif error:
                        payload["error"] = error
                    else:
                        task_state = delegation_policy.task_state(run)
                        payload["message"] = '旧任务或非成果委派任务没有验收记录，不能追认验收通过。'
                        if delegation_policy.is_delegated(task_state):
                            payload["message"] = '执行器尚未结束，暂无验收记录；可先记录验收条件。'
                    return self.send_json(payload)
                if len(parts) == 5 and parts[4] == "handover":
                    return self.send_json(handover_view(parts[3]))
                prompt = run / "prompt.txt"
                if not prompt.exists():
                    prompt = run / "task.txt"
                row = None
                for _ in range(5):
                    row = next((t for t in public_tasks() if t["id"] == parts[3]), None)
                    if row is not None:
                        break
                    time.sleep(.05)
                if row is None:
                    # The task list can transiently skip a record while task.json
                    # is being replaced. Fall back to the locked single read and,
                    # if even that fails, answer an explicit retryable error
                    # instead of a silent {"task": null} that crashed waiters.
                    try:
                        row = registry.read_task(run)
                    except (OSError, ValueError):
                        row = None
                    if isinstance(row, dict):
                        row.pop("worker_pid", None)
                        row.pop("process_pid", None)
                        row.pop("launcher_pid", None)
                        row['review'] = review_facts(parts[3])
                        import execution_result
                        row['execution_result'] = execution_result.project(row, row['review'])
                if not isinstance(row, dict):
                    return self.send_json({"task": None, "transient": True,
                                           "error": "任务记录暂时不可读，请重试"}, 503)
                return self.send_json({"task": public_task_cost(row), "prompt": prompt.read_text(encoding="utf-8") if prompt.exists() else ""})
            assets = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css",
                      '/project-map.js': 'project-map.js', '/project-map.css': 'project-map.css',
                      '/execution-result.js': 'execution-result.js', '/execution-result.css': 'execution-result.css'}
            if path not in assets:
                return self.send_json({"error": "页面不存在"}, 404)
            raw = (WEB / assets[path]).read_bytes()
            mime = {"/": "text/html", "/app.js": "text/javascript", "/style.css": "text/css",
                    '/project-map.js': 'text/javascript', '/project-map.css': 'text/css',
                    '/execution-result.js': 'text/javascript', '/execution-result.css': 'text/css'}[path]
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)
        except PermissionError as exc:
            self.send_json({"error": str(exc), **({'code': 'panel_session_expired'}
                           if isinstance(exc, PanelSessionExpired) else {})}, 403)
        except Exception as exc:
            self.send_json({"error": safe_error(exc)}, 400)

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            # The capability-authenticated broker call is the one POST route that
            # does not reuse the panel session token; it carries its own private
            # connection capability and is still loopback/origin guarded.
            self.guard(mutation=path != '/api/brain/call')
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 200000:
                raise ValueError("请求大小不正确")
            body = json.loads(self.rfile.read(length))
            project_views.check_request(path,self.project_view,body)
            if path == '/api/project-map':
                import project_map
                project = project_views.explicit_project(self.project_view)
                if not project:
                    raise ValueError('项目地图需要先选择一个明确项目，不支持全部项目视图')
                if not isinstance(body, dict) or set(body) - {'file', 'query', 'refresh', 'limit'}:
                    raise ValueError('项目地图参数不正确，不能覆盖项目身份')
                return self.send_json(project_map.query(project, **body))
            if path == '/api/host-account/snapshot':
                # The optional DSH main-brain plugin pushes one display-only
                # public snapshot here through the existing panel session token.
                # This is not a model tool, budget write or account credential
                # route; the loopback/origin/CSRF guard above is unchanged.
                try:
                    host_balance.record(body)
                except host_balance.SnapshotError as error:
                    raise ValueError(str(error)) from None
                return self.send_json({'ok': True})
            if path == '/api/brain/connect':
                import brain_gateway
                return self.send_json(brain_gateway.register_connection(body), 201)
            if path == '/api/brain/call':
                import brain_gateway
                if not isinstance(body, dict):
                    raise ValueError('主脑调用参数需要是对象')
                unknown = set(body) - {'operation', 'payload'}
                if unknown:
                    raise ValueError('主脑调用含未知字段')
                try:
                    result = brain_gateway.handle(
                        body.get('operation'), body.get('payload'),
                        self.headers.get(brain_gateway.CAPABILITY_HEADER, ''))
                except brain_gateway.AuthError as error:
                    return self.send_json({'error': str(error)}, 401)
                except Exception as error:
                    import brain_requests
                    if isinstance(error, brain_requests.UnknownOutcome):
                        return self.send_json({'outcome': 'unknown', 'error': str(error),
                                               'request_reference': error.reference}, 409)
                    raise
                return self.send_json(result)
            if path == '/api/projects/remember':
                return self.send_json(project_views.remember_project(self.project_view))
            if path in {'/api/task-records/preview','/api/task-records/delete'}:
                if not isinstance(body,dict):raise ValueError('记录清理参数需要是对象')
                if path.endswith('/preview'):
                    return self.send_json(task_history.preview(self.project_view,body.get('task_ids')))
                return self.send_json(task_history.delete(self.project_view,body.get('token'),idle_guard=history_idle_guard))
            if path == '/api/db/compact':
                return self.send_json(compact_databases(self.project_view))
            if path == '/api/projects/pick':
                initial = self.project_view.get('project') if self.project_view else None
                return self.send_json(folder_picker.JOBS.start(initial))
            if path == '/api/workflow':
                if isinstance(body, dict) and 'endpoint_settings' in body:
                    import execution_endpoints
                    block = body['endpoint_settings']
                    if set(body) != {'endpoint_settings'} or not isinstance(block, dict) or set(block) != {'id', 'values'}:
                        raise ValueError('执行端设置需要独立保存，不能混入范围、预算或授权')
                    return self.send_json(execution_endpoints.update(block['id'], block['values'], workflow_settings.snapshot()))
                return self.send_json(workflow_settings.save_settings(body))
            endpoint_match = re.fullmatch(r'/api/execution-endpoints/(pi|dsh|workbuddy|zcode)', path)
            if endpoint_match:
                import execution_endpoints
                return self.send_json(execution_endpoints.update(endpoint_match.group(1), body, workflow_settings.snapshot()))
            if path == '/api/codex-usage/refresh':
                return self.send_json(codex_usage.usage_state(force=True))
            if path == '/api/route-preview':
                settings = workflow_settings.snapshot()
                # Preview uses the same final-model -> agent resolution as a real
                # submission.  It must not bake the current global value into the
                # request body as an explicit override.
                data = deepseek_billing.enrich_models(registry.list_models())
                daily_cost = budget_relay.daily_state(settings)
                return self.send_json(model_router.choose(body, data['models'], settings, public_tasks(),
                                      daily_cost['remaining_cny'], daily_uncertain=daily_cost.get('unresolved', False)))
            if path == "/api/models":
                return self.send_json(registry.upsert_model(body), 201)
            if path == "/api/discover":
                return self.send_json(discover(body))
            if path == "/api/tasks":
                return self.send_json(submit_task(body), 202)
            if path == "/api/goals":
                return self.send_json(create_goal(body), 201)
            if path == "/api/stages":
                return self.send_json(create_stage(body), 201)
            match = re.fullmatch(r"/api/goals/([A-Za-z0-9_-]+)/status", path)
            if match:
                return self.send_json(set_goal_status(
                    match.group(1), body, project=project_views.explicit_project(self.project_view)))
            match = re.fullmatch(r"/api/stages/([A-Za-z0-9_-]+)/(dispatch|cancel|requeue|sync)", path)
            if match:
                stage_id, action = match.groups()
                explicit = project_views.explicit_project(self.project_view)
                if action == "dispatch":
                    return self.send_json(dispatch_stage(stage_id, body, project=explicit), 202)
                if action == "cancel":
                    return self.send_json(cancel_stage_endpoint(stage_id, body, project=explicit))
                if action == "requeue":
                    return self.send_json(requeue_stage_endpoint(stage_id, body, project=explicit))
                return self.send_json(sync_stages({**body, "stage_id": stage_id,
                                                   "project": explicit or body.get("project")}))
            match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]+)/handover", path)
            if match:
                return self.send_json(write_handover(match.group(1), body), 201)
            match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]+)/reference", path)
            if match:
                return self.send_json(reference_handover(match.group(1), body))
            match = re.fullmatch(r"/api/models/([A-Za-z0-9_-]+)/(save|delete|test|default|balance|thinking|agent-default)", path)
            if match:
                model_id, action = match.groups()
                preference_only = (action == "save" and isinstance(body, dict)
                                   and set(body) == {"default_agent"})
                if action in {"delete", "save"} and not preference_only and any(t.get("model_id") == model_id and t["status"] in {"queued", "running"} for t in public_tasks()):
                    raise ValueError("这个模型还有任务，请等待完成或先停止任务")
                if action == "agent-default" or preference_only:
                    # Preference-only atomic patch: no connection, secret,
                    # preset, price, scope or budget field can be touched.
                    if not isinstance(body, dict) or set(body) != {"default_agent"}:
                        raise ValueError("模型默认 Agent 需要单独保存，不能混入模型连接或其他参数")
                    return self.send_json(model_agent_defaults.update(model_id, body["default_agent"]))
                if action == "delete":
                    registry.delete_model(model_id)
                    return self.send_json({"ok": True})
                if action == "test":
                    return self.send_json(probe_model(model_id))
                if action == 'thinking':
                    return self.send_json(thinking_config.probe_and_store(model_id))
                if action == "balance":
                    row = registry.public_model(registry.get_model(model_id, require_enabled=False))
                    if not deepseek_billing.is_deepseek(row):
                        raise ValueError("当前模型不支持 DeepSeek 官方余额查询")
                    return self.send_json(deepseek_billing.balance_state(row, force=True))
                if action == "default":
                    return self.send_json(registry.set_default(model_id))
                if isinstance(body.get('subagent_policy'), str):
                    body = {**body, 'subagent': {**(body.get('subagent') if isinstance(body.get('subagent'), dict) else {}),
                                                 'policy': body['subagent_policy']}}
                if 'agent_settings' in body:
                    block = body['agent_settings']
                    if set(body) != {'agent_settings'} or not isinstance(block, dict) or set(block) != {'engine', 'values'}:
                        raise ValueError('Agent 设置需要独立保存，不能混入模型连接或其他参数')
                    return self.send_json(registry.update_agent_settings(model_id, block['engine'], block['values']))
                return self.send_json(registry.upsert_model(body, model_id))
            match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]+)/recover", path)
            if match:
                return self.send_json(recover_task(match.group(1), body,
                                                   body.get("actor") or "main"), 202)
            match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]+)/cancel", path)
            if match:
                return self.send_json(cancel_task(match.group(1), body.get("reason", ""),
                                                  body.get("actor") or "main"))
            match = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]+)/review", path)
            if match:
                return self.send_json(update_review(match.group(1), body))
            return self.send_json({"error": "操作不存在"}, 404)
        except PermissionError as exc:
            self.send_json({"error": str(exc), **({'code': 'panel_session_expired'}
                           if isinstance(exc, PanelSessionExpired) else {})}, 403)
        except Exception as exc:
            self.send_json({"error": safe_error(exc)}, 400)


WORKER_SCRIPTS = ("run_worker.py", "chat_worker.py", "dsh_worker.py", "pi_worker.py", "workbuddy_worker.py", "zcode_worker.py")
# A real worker process always starts *before* the record's ``started_at`` is
# written, so a process created later than the record cannot be that worker
# (a reused PID).  The tolerance only absorbs clock jitter; a slow startup
# metadata probe must never be mistaken for a duplicate worker.
WORKER_START_SKEW_SECONDS = 5.0
WORKER_STATE_UNKNOWN = 'unknown'


def _recorded_process_verdict(pid, state):
    """True/False/``WORKER_STATE_UNKNOWN`` for one recorded process id.

    True needs positive identity evidence: our worker script is in the command
    line *and* the process was created before the record's ``started_at`` (plus
    clock tolerance).  An unreadable/denied process table is ``unknown`` -- never
    proof that the worker died.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return WORKER_STATE_UNKNOWN
    try:
        import psutil
    except ImportError:
        return WORKER_STATE_UNKNOWN
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return WORKER_STATE_UNKNOWN
    except psutil.Error:
        return WORKER_STATE_UNKNOWN
    try:
        command = " ".join(process.cmdline())
        created = float(process.create_time())
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return WORKER_STATE_UNKNOWN
    if not any(str(HERE / script) in command for script in WORKER_SCRIPTS):
        # Foreign process or a reused PID: not ours, so not a live worker.
        return False
    started = state.get("started_at") if isinstance(state, dict) else None
    if started:
        try:
            started_ts = datetime.fromisoformat(str(started)).timestamp()
        except (ValueError, TypeError):
            started_ts = None
        if started_ts is not None and created > started_ts + WORKER_START_SKEW_SECONDS:
            # The live process is newer than our record: the recorded PID was
            # reused by something else after our worker ended.
            return False
    return True


def recorded_worker_alive(state):
    """True/False/``WORKER_STATE_UNKNOWN`` for the recorded worker identity.

    Every recorded identity (worker/launcher/child) is inspected; any positive
    match proves the worker is still ours.  Unknown (unreadable) beats "gone":
    an unreadable process table is never proof of death, so callers must refuse
    recovery explicitly instead of assuming the worker vanished.
    """
    if not isinstance(state, dict):
        return WORKER_STATE_UNKNOWN
    pids = [state.get(key) for key in ("worker_pid", "launcher_pid", "process_pid", "child_pid")]
    pids = [pid for pid in pids if pid not in (None, "", 0)]
    if not pids:
        return False
    verdicts = [_recorded_process_verdict(pid, state) for pid in pids]
    if any(verdict is True for verdict in verdicts):
        return True
    if any(verdict == WORKER_STATE_UNKNOWN for verdict in verdicts):
        return WORKER_STATE_UNKNOWN
    return False


def recovery_record(kind, reason):
    return {"schema": 1, "kind": kind, "reason": reason, "at": registry.now(),
            "auto_resend": False,
            "note": "未完成的执行不会自动重发；确认原执行进程已经结束并核对改动的真实影响后，"
                    "可从面板显式恢复同一个任务记录（不创建新记录）。"}


def recover_pending_tasks(entries=None):
    """Restart reconciliation: queued/unfinished tasks become ``recovery_pending``.

    Never a blanket failure and never an automatic re-dispatch: the original
    task/model/project/scope/progress stay untouched, and the user gets an
    explicit, inspectable recovery entry.
    """
    actions = []
    if entries is None:
        try:
            entries = project_storage.iter_task_dirs()
        except (OSError, ValueError):
            entries = []
    for entry in entries:
        run = entry['run']
        task_file = run / "task.json"
        if not task_file.is_file():
            continue
        try:
            state = json.loads(task_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (run / "cancelled.flag").exists():
            # A real cancellation is never turned into a pending recovery.
            continue
        status = state.get("status")
        try:
            if status == "queued" and (state.get("source") == "panel" or state.get("engine") in execution_engines.AGENTS):
                reason = "面板重启时任务尚未开始执行"
                record = recovery_record("queued_not_started", reason)
            elif status == "running":
                verdict = recorded_worker_alive(state)
                if verdict is True:
                    # A confirmed live worker keeps running; reconciliation must
                    # never turn it into a duplicate recovery.
                    continue
                if verdict == WORKER_STATE_UNKNOWN:
                    reason = ("面板重启时无法确认原执行进程状态（进程表不可读或被拒绝），"
                              "已标记为待恢复；不会自动重发，也不允许在状态确认前人工恢复")
                    record = recovery_record("worker_state_unknown", reason)
                else:
                    reason = ("面板重启时任务的执行进程已经不存在，未完成，也没有重新发送任何请求")
                    record = recovery_record("worker_gone", reason)
            else:
                continue
            registry.patch_task(run, status="recovery_pending",
                                phase="待恢复（面板重启，未自动重发）",
                                recovery=record)
            actions.append({"task_id": state.get("id") or run.name, "kind": record["kind"],
                            "reason": reason})
        except (OSError, ValueError):
            continue
    return actions


def main():
    task_history.recover()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    registry.list_models()
    # Tasks that had not started (or whose real worker disappeared) before a
    # server restart are shown as pending recovery instead of a blanket failure.
    recover_pending_tasks()
    # Persisted stages that were dispatched but not finished become
    # "recovery_pending"; nothing is re-sent automatically.
    try:
        stage_store.recover()
        stage_store.sync_from_tasks()
    except (OSError, ValueError):
        pass
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    registry.write_json(HERE / "panel-runtime.json", {"pid": os.getpid(), "url": f"http://127.0.0.1:{server.server_port}", "started_at": registry.now()})
    print(f"Model panel: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        EXECUTOR.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
