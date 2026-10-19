"""Panel-side, in-memory cache for the official DSH host account balance.

The cache accepts only a small public snapshot written by the optional DSH
main-brain plugin after it calls the official host account controller
(``account.getState/getBalance/watch``).  This module never reads DSH
credentials, profile data, API keys or account caches, and it never calls DSH,
Platform or a model provider itself.  The snapshot contains wallet amounts only;
unknown data stays unknown instead of falling back to an API-key balance or the
Codex/GPT quota.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import math
import re
import threading

SCHEMA = 1
HOST = 'dsh'
SOURCE = 'dsh-account-controller'
SOURCE_API = 'account.getBalance'
SOURCE_LABEL = 'DSH 官方登录账户'
ACCOUNT_SOURCE = 'native-dsh-login'
FRESH_SECONDS = 180
STALE_SECONDS = 900
SNAPSHOT_TTL_SECONDS = 300
MAX_WALLETS = 32
ERROR_LIMIT = 300
STATUSES = {'ready', 'signed-out', 'unavailable', 'expired', 'error', 'updating'}
WALLET_KINDS = {'normal', 'bonus'}
CURRENCIES = {'CNY', 'USD'}
DECIMAL = re.compile(r'^-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$', re.IGNORECASE)
SNAPSHOT_KEYS = {
    'schema', 'host', 'status', 'source', 'source_api',
    'fetched_at', 'expires_at', 'wallets', 'error',
}
WALLET_KEYS = {'kind', 'currency', 'balance'}
CONTROL = re.compile(r'[\x00-\x1f\x7f]+')

_LOCK = threading.RLock()
_SNAPSHOT = None
_RECEIVED_AT = None


class SnapshotError(ValueError):
    """A rejected public snapshot; the message never carries secret content."""


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_time(value, label):
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError(f'{label} 缺失')
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SnapshotError(f'{label} 格式不正确') from None
    if parsed.tzinfo is None:
        raise SnapshotError(f'{label} 需要带时区')
    return parsed.astimezone(timezone.utc)


def _clean_error(value):
    if value in (None, ''):
        return ''
    if not isinstance(value, str):
        raise SnapshotError('error 需要是字符串')
    safe = {'适配器已停用', '适配器已停用；未显示旧余额。',
            'DSH 官方账户服务尚未就绪，正在等待服务注册；未显示旧余额。',
            'DSH 登录凭据已失效，请重新登录；未显示旧余额。'}
    return value if value in safe else '账户接口暂不可用，请稍后重试。'


def _validate(snapshot, now):
    if not isinstance(snapshot, dict):
        raise SnapshotError('账户快照需要是对象')
    unknown = set(snapshot) - SNAPSHOT_KEYS
    if unknown:
        raise SnapshotError('账户快照含不允许的字段')
    if snapshot.get('schema') != SCHEMA:
        raise SnapshotError('账户快照 schema 不支持')
    if snapshot.get('host') != HOST:
        raise SnapshotError('账户快照宿主不正确')
    if snapshot.get('source') != SOURCE or snapshot.get('source_api') != SOURCE_API:
        raise SnapshotError('账户快照来源不是官方 DSH 账户服务')
    status = snapshot.get('status')
    if status not in STATUSES:
        raise SnapshotError('账户快照状态不正确')
    fetched_at = _parse_time(snapshot.get('fetched_at'), 'fetched_at')
    if fetched_at > now + timedelta(seconds=120):
        raise SnapshotError('账户快照时间在未来')
    expires_at = _parse_time(snapshot.get('expires_at'), 'expires_at')
    if expires_at < fetched_at:
        raise SnapshotError('账户快照过期时间早于读取时间')
    if expires_at > fetched_at + timedelta(seconds=3600):
        raise SnapshotError('账户快照有效期过长')
    error = _clean_error(snapshot.get('error'))
    wallets = snapshot.get('wallets')
    if not isinstance(wallets, list):
        raise SnapshotError('账户快照 wallets 需要是数组')
    if status != 'ready' and wallets:
        raise SnapshotError('非 ready 快照不能携带旧余额')
    if len(wallets) > MAX_WALLETS:
        raise SnapshotError('账户快照钱包数量异常')
    clean_wallets = []
    for row in wallets:
        if not isinstance(row, dict):
            raise SnapshotError('账户钱包需要是对象')
        if set(row) != WALLET_KEYS:
            raise SnapshotError('账户钱包含不允许的字段')
        if row.get('kind') not in WALLET_KINDS:
            raise SnapshotError('账户钱包类型不正确')
        if row.get('currency') not in CURRENCIES:
            raise SnapshotError('账户钱包币种不正确')
        balance = row.get('balance')
        if (not isinstance(balance, str) or len(balance) > 128
                or not DECIMAL.fullmatch(balance) or not math.isfinite(float(balance))):
            raise SnapshotError('账户钱包金额不是十进制字符串')
        clean_wallets.append({
            'kind': row['kind'],
            'currency': row['currency'],
            'balance': balance,
        })
    return {
        'status': status,
        'fetched_at': fetched_at,
        'expires_at': expires_at,
        'wallets': clean_wallets,
        'error': error,
    }


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if value else None


def record(snapshot, now=None):
    """Validate and store one public snapshot, returning the public state."""
    moment = _now(now)
    clean = _validate(snapshot, moment)
    global _SNAPSHOT, _RECEIVED_AT
    with _LOCK:
        if _SNAPSHOT is not None and clean['fetched_at'] < _SNAPSHOT['fetched_at']:
            raise SnapshotError('账户快照早于当前快照，已拒绝回退')
        _SNAPSHOT = copy.deepcopy(clean)
        _RECEIVED_AT = moment
    return state(now=moment)


def clear(reason='适配器已停用', now=None):
    """Clear amounts by writing an explicit unavailable snapshot."""
    moment = _now(now)
    snapshot = {
        'schema': SCHEMA,
        'host': HOST,
        'status': 'unavailable',
        'source': SOURCE,
        'source_api': SOURCE_API,
        'fetched_at': _iso(moment),
        'expires_at': _iso(moment + timedelta(seconds=SNAPSHOT_TTL_SECONDS)),
        'wallets': [],
        'error': CONTROL.sub(' ', str(reason or ''))[:ERROR_LIMIT],
    }
    return record(snapshot, now=moment)


def state(now=None):
    """Return the display-safe cache state; no credential or profile field."""
    moment = _now(now)
    with _LOCK:
        snapshot = copy.deepcopy(_SNAPSHOT)
        received = _RECEIVED_AT
    base = {
        'schema': SCHEMA,
        'host': HOST,
        'source': SOURCE,
        'source_api': SOURCE_API,
        'source_label': SOURCE_LABEL,
        'account_source': ACCOUNT_SOURCE,
        'refresh': 'snapshot-only',
        'refresh_note': '刷新只重新读取本机最近快照；不会向 DSH 或 Platform 发起请求。',
        'status': 'unavailable',
        'message': '尚未收到 DSH 官方账户适配器数据；未知余额不会回落到 API Key 账户或 GPT 额度。',
        'wallets': [],
        'error': '',
        'fetched_at': None,
        'received_at': None,
        'fresh': False,
        'stale': False,
        'expired': False,
    }
    if snapshot is None:
        return base
    age = max(0.0, (moment - snapshot['fetched_at']).total_seconds())
    hard_expired = age > STALE_SECONDS
    soft_stale = age > FRESH_SECONDS or moment > snapshot['expires_at']
    result = {
        **base,
        'status': snapshot['status'],
        'error': snapshot['error'],
        'fetched_at': _iso(snapshot['fetched_at']),
        'received_at': _iso(received),
    }
    if snapshot['status'] == 'ready':
        if hard_expired:
            result.update({
                'status': 'expired',
                'message': 'DSH 账户余额快照已过期（超过 15 分钟未更新），未显示旧金额。',
                'stale': True,
                'expired': True,
            })
        elif soft_stale:
            result.update({
                'message': '上次读取的 DSH 账户余额；数据已超过刷新间隔，正在等待适配器更新。',
                'wallets': snapshot['wallets'],
                'stale': True,
            })
        else:
            result.update({
                'message': '',
                'wallets': snapshot['wallets'],
                'fresh': True,
            })
    elif snapshot['status'] == 'signed-out':
        result.update({
            'message': 'DSH 当前未登录，未显示余额。',
            'stale': soft_stale,
        })
    elif snapshot['status'] == 'updating':
        result['message'] = '正在读取 DSH 官方登录账户余额…'
    elif snapshot['status'] == 'expired':
        result.update({
            'message': snapshot['error'] or 'DSH 登录凭据已失效，请重新登录；未显示旧余额。',
            'stale': True,
            'expired': True,
        })
    elif snapshot['status'] == 'error':
        result['message'] = 'DSH 账户余额读取失败；未显示旧余额。'
    else:
        result['message'] = snapshot['error'] or 'DSH 官方账户接口暂不可用；余额未知。'
    return result


def _reset_for_tests():
    """Clear the in-memory cache; tests only, never called by the panel."""
    global _SNAPSHOT, _RECEIVED_AT
    with _LOCK:
        _SNAPSHOT = None
        _RECEIVED_AT = None
