"""Shared, deterministic helpers for transient-fault recovery and bounded retry.

This module is deliberately pure: no network, no threads, no clock reads other
than the ones a caller passes in.  It turns an exception (or an HTTP status) into
a compact *safe* category, decides whether one transport retry is provably safe,
and keeps the append-only ``fault_events`` evidence that lets a finished outcome
distinguish a resolved transient hiccup from an unresolved fatal fault.

Nothing here stores a provider body, a prompt or a credential: the only detail
ever persisted is a short fixed label plus, at most, the HTTP status.
"""
import email.utils
import math
import socket
import ssl
from datetime import datetime, timezone

SCHEMA = 1

# --------------------------------------------------------------- fault categories

CATEGORY_CONNECT = 'connect'
CATEGORY_DNS = 'dns'
CATEGORY_TIMEOUT = 'timeout'
CATEGORY_RESET = 'reset'
CATEGORY_TLS = 'tls'
CATEGORY_RATE_LIMIT = 'rate_limit'
CATEGORY_UPSTREAM = 'upstream_unavailable'
CATEGORY_AUTH = 'auth'
CATEGORY_PAID = 'paid'
CATEGORY_PROTOCOL = 'protocol'
CATEGORY_IDENTITY = 'identity'
CATEGORY_CLIENT = 'client_disconnected'
CATEGORY_RECORD_IO = 'record_io'
CATEGORY_CANCELLED = 'cancelled'
CATEGORY_UNKNOWN = 'unknown'

# Policy categories that a later unrelated success must never silently clear.
HARD_CATEGORIES = frozenset({
    CATEGORY_AUTH, CATEGORY_PAID, CATEGORY_PROTOCOL, CATEGORY_IDENTITY,
    CATEGORY_CANCELLED, 'policy',
})
# Transport hiccups that a later proven success may resolve.
TRANSIENT_CATEGORIES = frozenset({
    CATEGORY_CONNECT, CATEGORY_DNS, CATEGORY_TIMEOUT, CATEGORY_RESET, CATEGORY_TLS,
    CATEGORY_RATE_LIMIT, CATEGORY_UPSTREAM, CATEGORY_RECORD_IO,
})
CLASS_HARD = 'hard'
CLASS_TRANSIENT = 'transient'
CLASS_UNKNOWN = 'unknown'

CATEGORY_LABELS = {
    CATEGORY_CONNECT: '无法连接模型接口',
    CATEGORY_DNS: '无法解析模型接口域名',
    CATEGORY_TIMEOUT: '模型接口等待超时',
    CATEGORY_RESET: '模型接口连接被重置',
    CATEGORY_TLS: '模型接口 TLS 握手失败',
    CATEGORY_RATE_LIMIT: '模型接口限流（HTTP 429）',
    CATEGORY_UPSTREAM: '模型接口暂时不可用',
    CATEGORY_AUTH: '模型接口认证被拒绝',
    CATEGORY_PAID: '付费或余额校验被拒绝',
    CATEGORY_PROTOCOL: '模型接口协议错误',
    CATEGORY_IDENTITY: '模型身份不匹配',
    CATEGORY_CLIENT: '本地调用方断开连接',
    CATEGORY_RECORD_IO: '本地任务记录暂时不可读或不可写',
    CATEGORY_CANCELLED: '任务已取消',
    CATEGORY_UNKNOWN: '未分类的模型接口故障',
}

# Only a connect-phase failure that provably happened *before* the provider
# accepted anything (DNS resolution / connection refused) or an explicit
# server-side refusal (HTTP 429 as a bounded ``rate_limit``) may be retried.
# A timeout, a connection reset or an HTTP 5xx is ambiguous: a proxy 504 or a
# reset may happen *after* the upstream already accepted, computed and charged
# the request, so those must never be replayed automatically.
RETRYABLE_TRANSPORT = frozenset({
    CATEGORY_CONNECT, CATEGORY_DNS, CATEGORY_RATE_LIMIT,
})
# Explicit acceptance evidence.  Absent evidence is conservative: no replay.
ACCEPTANCE_PRE_SEND = 'pre_send'      # no request byte ever left this host
ACCEPTANCE_REJECTED = 'rejected'      # provider explicitly refused the request
ACCEPTANCE_AMBIGUOUS = 'ambiguous'    # may already have been accepted/charged
INHERENT_ACCEPTANCE = {
    CATEGORY_CONNECT: ACCEPTANCE_PRE_SEND,
    CATEGORY_DNS: ACCEPTANCE_PRE_SEND,
    CATEGORY_RATE_LIMIT: ACCEPTANCE_REJECTED,
}
RETRY_AFTER_CAP_SECONDS = 10.0
DEFAULT_RETRY_DELAY_SECONDS = 1.0
MAX_TRANSPORT_ATTEMPTS = 2  # one logical request + at most one automatic retry

# Safe, user-facing explanations for a retry decision.  Never a raw provider body.
RETRY_REASON_LABELS = {
    'retry_after': '按服务端 Retry-After 有界等待后安全重试一次',
    'transient': '连接阶段未被接受，安全重试一次',
    'rejected': '服务端显式拒绝（未被接受），安全重试一次',
    'not_safe_to_retry': '该故障无法证明请求未被接受，不自动重试（避免重复计费）',
    'acceptance_unproven': '缺少"未被接受"的证据，不自动重放（避免重复计费）',
    'response_delivered': '已向调用方送出内容，不重放',
    'retry_used': '本逻辑请求已重试过一次，不再重试',
    'retry_after_too_long': 'Retry-After 超出允许上限，不自动重试',
    'retry_after_invalid': 'Retry-After 无法解析为有界等待，不自动重试',
    'cancelled': '任务已取消，不重试',
}


def retry_reason_label(reason):
    return RETRY_REASON_LABELS.get(reason, reason)

FAULT_EVENTS_KEY = 'fault_events'
MAX_FAULT_EVENTS = 20


def fault_class(category):
    if category in HARD_CATEGORIES:
        return CLASS_HARD
    if category in TRANSIENT_CATEGORIES:
        return CLASS_TRANSIENT
    return CLASS_UNKNOWN


def category_label(category):
    return CATEGORY_LABELS.get(category, CATEGORY_LABELS[CATEGORY_UNKNOWN])


def http_status(exc):
    """The HTTP status of an error, without touching its body."""
    code = getattr(exc, 'code', None)
    return code if isinstance(code, int) else None


def classify_exception(exc):
    """Return ``{'category', 'http_status'}`` for one provider-side exception.

    Only the exception *type*/status is inspected; the message and any body are
    ignored so a credential or prompt can never leak into the record.
    """
    status = http_status(exc)
    if status is not None:
        if status in (401, 403):
            return {'category': CATEGORY_AUTH, 'http_status': status}
        if status == 402:
            return {'category': CATEGORY_PAID, 'http_status': status}
        if status == 429:
            return {'category': CATEGORY_RATE_LIMIT, 'http_status': status}
        if status == 408:
            return {'category': CATEGORY_TIMEOUT, 'http_status': status}
        if status in (500, 502, 503, 504):
            return {'category': CATEGORY_UPSTREAM, 'http_status': status}
        return {'category': CATEGORY_PROTOCOL, 'http_status': status}
    if isinstance(exc, (ConnectionRefusedError,)):
        return {'category': CATEGORY_CONNECT, 'http_status': None}
    if isinstance(exc, socket.gaierror):
        return {'category': CATEGORY_DNS, 'http_status': None}
    if isinstance(exc, ssl.SSLError):
        return {'category': CATEGORY_TLS, 'http_status': None}
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return {'category': CATEGORY_TIMEOUT, 'http_status': None}
    if isinstance(exc, ConnectionResetError):
        return {'category': CATEGORY_RESET, 'http_status': None}
    if isinstance(exc, BrokenPipeError):
        return {'category': CATEGORY_CLIENT, 'http_status': None}
    # urllib wraps low-level connect errors in URLError(reason=...).
    reason = getattr(exc, 'reason', None)
    if reason is not None and reason is not exc:
        nested = classify_exception(reason)
        if nested['category'] != CATEGORY_UNKNOWN:
            return nested
    return {'category': CATEGORY_UNKNOWN, 'http_status': None}


def retry_after_seconds(value, cap=RETRY_AFTER_CAP_SECONDS, moment=None):
    """Parse a Retry-After header into a bounded delay, or ``(None, reason)``.

    ``reason`` is ``'too_long'`` when the server asked for more than the cap, so
    the caller can report an honest refusal instead of silently truncating it.
    """
    if value in (None, ''):
        return None, ''
    text = str(value).strip()
    if not text:
        return None, ''
    try:
        seconds = float(text)
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None, 'unparsable'
        if parsed is None:
            return None, 'unparsable'
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        now = moment or datetime.now(timezone.utc)
        seconds = (parsed - now).total_seconds()
    # NaN, +inf and -inf are never a bounded wait: refuse instead of sleeping an
    # unbounded or invalid interval.
    if not math.isfinite(seconds):
        return None, 'unparsable'
    if seconds < 0:
        return None, 'past'
    if seconds > cap:
        return None, 'too_long'
    return seconds, ''


def transport_retry_allowed(*, category, http_status=None, response_started=False,
                            retry_used=False, retry_after=None, retry_after_reason='',
                            cancelled=False, acceptance=None):
    """Decide the single bounded retry for one logical request.

    A retry is allowed only when nothing has been delivered to the caller yet
    (``response_started`` is False), the failure is a provably unaccepted
    transport failure (explicit ``acceptance`` evidence or an inherently
    pre-send category), and no retry was used for this logical request.  A
    connection reset, a timeout or an HTTP 5xx is *ambiguous* -- it may have
    reached the provider after acceptance -- so ``acceptance`` must say so
    explicitly and truthfully; the default is conservative.

    ``acceptance`` is one of ``ACCEPTANCE_PRE_SEND`` (no request byte left this
    host), ``ACCEPTANCE_REJECTED`` (the provider explicitly refused it) or
    ``ACCEPTANCE_AMBIGUOUS``/``None`` (unproven: never replayed).
    """
    if cancelled:
        return {'retry': False, 'delay': 0.0, 'reason': 'cancelled'}
    if retry_used:
        return {'retry': False, 'delay': 0.0, 'reason': 'retry_used'}
    if response_started:
        return {'retry': False, 'delay': 0.0, 'reason': 'response_delivered'}
    if category not in RETRYABLE_TRANSPORT:
        return {'retry': False, 'delay': 0.0, 'reason': 'not_safe_to_retry'}
    evidence = acceptance if acceptance is not None else INHERENT_ACCEPTANCE.get(category)
    if evidence not in (ACCEPTANCE_PRE_SEND, ACCEPTANCE_REJECTED):
        return {'retry': False, 'delay': 0.0, 'reason': 'acceptance_unproven'}
    if retry_after_reason in ('too_long', 'unparsable'):
        return {'retry': False, 'delay': 0.0,
                'reason': 'retry_after_too_long' if retry_after_reason == 'too_long' else 'retry_after_invalid'}
    if retry_after is not None:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            return {'retry': False, 'delay': 0.0, 'reason': 'retry_after_too_long'}
        if not math.isfinite(delay) or delay < 0 or delay > RETRY_AFTER_CAP_SECONDS:
            return {'retry': False, 'delay': 0.0, 'reason': 'retry_after_too_long'}
        return {'retry': True, 'delay': delay, 'reason': 'retry_after'}
    if evidence == ACCEPTANCE_REJECTED:
        return {'retry': True, 'delay': DEFAULT_RETRY_DELAY_SECONDS, 'reason': 'rejected'}
    return {'retry': True, 'delay': DEFAULT_RETRY_DELAY_SECONDS, 'reason': 'transient'}


# ------------------------------------------------------------- fault_events store

def compact_event(channel, category, at=None, detail=None, http=None):
    """A compact, safe fault record.  Never carries a message or a body."""
    event = {'channel': str(channel)[:24], 'category': category,
             'class': fault_class(category), 'at': at}
    if isinstance(http, int):
        event['http_status'] = http
    label = str(detail) if detail else category_label(category)
    event['detail'] = label[:120]
    event['resolved'] = False
    return event


def append_event(events, event, limit=MAX_FAULT_EVENTS):
    """Append one event, renumber ``seq`` and keep the newest ``limit`` rows.

    Two things are *pinned* so renumbering or history compaction can never change
    the verdict: the earliest unresolved hard/unknown event per channel+category
    (a fatal blocker must survive), and the latest resolved event per channel
    (so a legacy ``budget_error``/``gateway_error`` mirror on that channel stays
    correctly explained instead of turning into a false blocker).
    """
    rows = [dict(row) for row in events if isinstance(row, dict)] if isinstance(events, list) else []
    rows.append(dict(event))
    for index, row in enumerate(rows):
        row['seq'] = index + 1
    if len(rows) <= limit:
        return rows
    pinned = {}
    for row in rows:
        if fault_class(row.get('category')) == CLASS_TRANSIENT:
            if row.get('resolved'):
                # Latest resolved row per channel.
                pinned[('resolved', str(row.get('channel')))] = row
            continue
        if not row.get('resolved'):
            # Earliest unresolved hard/unknown blocker per channel+category.
            pinned.setdefault(('blocker', str(row.get('channel')), str(row.get('category'))), row)
    order = {id(row): index for index, row in enumerate(rows)}
    keep = rows[-limit:]
    keep_ids = {id(row) for row in keep}
    extra = [row for row in pinned.values() if id(row) not in keep_ids]
    if not extra:
        return keep
    kept = sorted(extra + keep, key=lambda row: order[id(row)])
    for index, row in enumerate(kept):
        row['seq'] = index + 1
    return kept


def resolve_transient(events, at, request_id=None):
    """Mark unresolved transient events resolved by a later proven success.

    Hard/policy events are never resolved here: an auth, paid, protocol,
    identity or cancellation violation stays visible no matter what succeeds
    afterwards.
    """
    out = []
    changed = 0
    for row in events if isinstance(events, list) else []:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        if not row.get('resolved') and fault_class(row.get('category')) == CLASS_TRANSIENT:
            row['resolved'] = True
            row['resolved_at'] = at
            if request_id:
                row['recovered_by'] = str(request_id)[:40]
            changed += 1
        out.append(row)
    return out, changed


def event_summary(event):
    """One short human-readable line for a single fault event."""
    label = category_label(event.get('category'))
    status = event.get('http_status')
    text = label + (f'（HTTP {status}）' if isinstance(status, int) else '')
    if event.get('resolved'):
        text += '，已由后续成功请求证明恢复'
    return text


def fault_summary(events, limit=4):
    rows = [event_summary(row) for row in events if isinstance(row, dict)][-limit:]
    return '；'.join(rows)


def unresolved_fault(state):
    """Return the active fatal fault of a task record, or None.

    ``fault_events`` is authoritative for the channels it still records: an old
    transient entry is ignored as soon as a later request proved the transport
    works again, while an unresolved entry of *any* class still fails the
    outcome.  A legacy ``budget_error``/``gateway_error`` mirror is only ignored
    when the surviving ledger actually covers that channel, or when the mirror is
    explicitly a recoverable request-level rejection (``*_error_scope`` ==
    ``'request'``); otherwise it still fails closed.
    """
    if not isinstance(state, dict):
        return None
    events = state.get(FAULT_EVENTS_KEY)
    rows = [row for row in events if isinstance(row, dict)] if isinstance(events, list) else []
    active = [row for row in rows if not row.get('resolved')]
    if active:
        first = active[0]
        return {'message': '模型接口故障未恢复：' + fault_summary(active),
                'events': active, 'category': first.get('category'),
                'class': fault_class(first.get('category')), 'legacy': False}
    channels = {str(row.get('channel')) for row in rows}
    for key, channel in (('budget_error', 'budget'), ('gateway_error', 'gateway')):
        text = state.get(key)
        if not text or channel in channels:
            continue
        if state.get(key + '_scope') == 'request':
            # A recoverable request-level guard rejection (never a provider fault):
            # a later successful request may supersede it, so it is not a blocker.
            continue
        return {'message': '模型接口故障未恢复：' + str(text)[:300],
                'events': [], 'category': CATEGORY_UNKNOWN, 'class': CLASS_HARD,
                'legacy': True, 'channel': channel}
    return None


def recovery_evidence(state):
    """Compact resolved/unresolved counters for a finished outcome."""
    events = state.get(FAULT_EVENTS_KEY) if isinstance(state, dict) else None
    rows = [row for row in events if isinstance(row, dict)] if isinstance(events, list) else []
    return {'schema': SCHEMA, 'total': len(rows),
            'resolved': sum(1 for row in rows if row.get('resolved')),
            'unresolved': sum(1 for row in rows if not row.get('resolved')),
            'hard_unresolved': sum(1 for row in rows
                                   if not row.get('resolved') and fault_class(row.get('category')) == CLASS_HARD),
            'categories': sorted({str(row.get('category')) for row in rows})}
