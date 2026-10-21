"""Provider-specific thinking controls and read-only engine-default discovery.

Two separate concepts live here and must never be conflated:

* the *immutable user default* -- ``model['thinking_level']``, stored in the
  registry and only changed by an explicit panel action; and
* the *per-task probe state* -- what one real task actually managed to verify
  about the running engine, recorded on that task's record.

A task never writes back to ``thinking_level``.  A successful probe only refreshes
the *engine metadata cache* (``thinking_info``), which is discovered information,
not a user preset or a shared engine setting.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, ProxyHandler, build_opener

import model_registry as registry
import deepseek_billing

LABELS = {'default': '沿用默认', 'off': '关闭', 'low': '低', 'medium': '中', 'high': '高', 'xhigh': '超高', 'max': '最高'}
DEEPSEEK_DOC = 'https://api-docs.deepseek.com/guides/thinking_mode/'

# One metadata probe is up to two total attempts with a 30 second read timeout
# each: a slow local engine gets a real chance, while a dead one still fails in a
# bounded time.  The delay between attempts is bounded and interruptible.
PROBE_ATTEMPTS = 2
PROBE_READ_TIMEOUT = 30
PROBE_RETRY_DELAY = 1.5
# Stored engine metadata is only trusted for a bounded age when it is reused for
# an explicitly chosen non-default level.
CACHE_MAX_AGE_SECONDS = 3600
PROBE_DELAY_SLICE = 0.2

STATUS_VERIFIED = 'verified'
STATUS_DEGRADED = 'degraded'
STATUS_UNKNOWN = 'unknown'


class ThinkingError(RuntimeError):
    """Base class for a thinking-metadata problem that stops one task."""


class ThinkingMismatch(ThinkingError):
    """The engine currently serves a different model: fatal, never retried."""


class ThinkingCancelled(ThinkingError):
    """The task was cancelled while the metadata probe was running."""


class ThinkingUnavailable(ThinkingError):
    """Trusted metadata is required for an explicit level but is unavailable."""


def profile(model):
    if model.get('provider') == 'workbuddy':
        return {'adapter': 'workbuddy-native', 'supported': ['default'], 'selected': 'default',
                'label': 'WorkBuddy 原生默认', 'status': STATUS_UNKNOWN,
                'note': '原生模型设置由 WorkBuddy 管理；工作台不向其发送 API 思考参数。'}
    selected = model.get('thinking_level', 'default')
    if deepseek_billing.is_official(model):
        info = {'adapter': 'deepseek', 'supported': ['default', 'off', 'low', 'high', 'max'],
                'default_effort': 'high', 'default_enabled': True, 'status': 'documented',
                'source': DEEPSEEK_DOC, 'engine': 'DeepSeek 官方', 'checked_at': '2026-10-13', 'note': '官方默认开启思考，使用 high。'}
    else:
        stored = model.get('thinking_info') or {}
        if stored.get('api_base') == model.get('api_base') and stored.get('model') == model.get('model'):
            info = dict(stored)
            age = cache_age_seconds(info)
            if info.get('status') == STATUS_VERIFIED and age is not None and age > CACHE_MAX_AGE_SECONDS:
                # Historical metadata is still shown, but clearly labelled as
                # aged: it is not presented as a fresh verification.
                info['stale'] = True
                info['stale_seconds'] = int(age)
                info['stale_note'] = ('引擎元数据缓存已超过 %d 秒；派发时会重新核实，无法核实时不启用非默认等级。'
                                      % CACHE_MAX_AGE_SECONDS)
        else:
            info = {'adapter': 'unknown', 'supported': ['default'], 'default_effort': None, 'default_enabled': None,
                    'status': STATUS_UNKNOWN, 'note': '尚未确认引擎支持的等级；请先读取引擎默认。'}
    info['selected'] = selected
    if selected == 'default':
        if info.get('default_enabled') is False:
            label = '默认 · 关闭'
        elif info.get('default_effort'):
            label = '默认 · ' + LABELS.get(info['default_effort'], info['default_effort']) + ' / ' + info['default_effort']
        else:
            label = '引擎默认' if model.get('provider') == 'local' else '服务商默认'
        if info.get('status') in {STATUS_DEGRADED, STATUS_UNKNOWN} and model.get('provider') == 'local':
            label = '引擎默认（本轮未核实等级）'
        elif info.get('stale'):
            label += '（缓存已过期，本轮未重新核实）'
    else:
        label = LABELS.get(selected, selected) + (' / ' + selected if selected != 'off' else '')
    info['label'] = label
    return info


def enrich(data):
    for model in data['models']:
        model['thinking'] = profile(model)
    return data


# ------------------------------------------------------------------ time helpers

def parse_moment(value):
    """Parse an ISO timestamp into an aware datetime, or return None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def cache_age_seconds(info, moment=None):
    """Age of stored engine metadata in seconds, or None when it has no stamp."""
    checked = parse_moment((info or {}).get('checked_at')) if isinstance(info, dict) else None
    if checked is None:
        return None
    reference = moment or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0.0, (reference - checked).total_seconds())


def compatible_info(model, max_age=CACHE_MAX_AGE_SECONDS, moment=None):
    """Identity-matched, verified and still-fresh stored metadata, or None.

    Untrusted or aged information yields None so it can never be turned into an
    invented support claim for an explicitly chosen level.
    """
    stored = model.get('thinking_info')
    if not isinstance(stored, dict):
        return None
    if stored.get('api_base') != model.get('api_base') or stored.get('model') != model.get('model'):
        return None
    if stored.get('status') != STATUS_VERIFIED:
        return None
    age = cache_age_seconds(stored, moment)
    if age is None or age > max_age:
        return None
    return dict(stored)


# ------------------------------------------------------------------------ probe

def _read_json(opener, origin, path, headers, timeout):
    with opener.open(Request(origin + path, headers=headers), timeout=timeout) as response:
        return json.loads(response.read(4000000))


def _probe_once(model, timeout=PROBE_READ_TIMEOUT):
    """One read-only metadata attempt; raises ThinkingMismatch on identity drift."""
    if deepseek_billing.is_official(model):
        return profile({**model, 'thinking_level': 'default'})
    base = urlparse(model['api_base'])
    origin = f'{base.scheme}://{base.netloc}'
    headers = {'Authorization': 'Bearer ' + model['api_key']} if model.get('api_key') else {}
    opener = build_opener(ProxyHandler({})) if model.get('bypass_proxy') else build_opener()
    props = _read_json(opener, origin, '/props', headers, timeout)
    template = props.get('chat_template', '')
    engine = str(props.get('build_info', ''))
    if props.get('model_alias') and props['model_alias'] != model['model']:
        # An exact identity mismatch is a definitive answer, not a transient
        # fault: it is never retried and must stay fatal.
        raise ThinkingMismatch('引擎当前加载的模型与此卡片不一致，未更新默认等级')
    if not engine.startswith('Strata ') or not isinstance(template, str):
        raise ValueError('当前未适配此引擎的等级探测，保留默认，不假定它支持高低等级')
    default = re.search(r'reasoning_effort\s*\|\s*default\([\'\"](\w+)', template)
    offered = re.search(r"resolved_reasoning_effort not in \(([^)]+)\)", template)
    if not default or not offered:
        raise ValueError('无法从当前模型模板确认思考等级')
    levels = re.findall(r"['\"](\w+)['\"]", offered.group(1))
    if set(levels) != {'xhigh', 'medium', 'low'}:
        raise ValueError('此模型模板的等级定义已变化，请重新核对适配')
    # Strata's shared Chat defaults can override the template for other clients.
    shared = _read_json(opener, origin, '/settings', headers, timeout)
    defaults = shared.get('defaults', {})
    if not isinstance(defaults, dict):
        raise ValueError('引擎共享默认设置格式不正确')
    effort = defaults.get('reasoning_effort') or default.group(1)
    effort = 'xhigh' if effort == 'high' else effort
    enabled = effort != 'none'
    if enabled and effort not in levels:
        raise ValueError('引擎当前默认等级无法识别')
    return {'adapter': 'strata', 'supported': ['default', 'off', 'low', 'medium', 'xhigh'],
            'default_effort': effort if enabled else 'off', 'default_enabled': enabled, 'engine': engine,
            'status': STATUS_VERIFIED, 'api_base': model['api_base'], 'model': model['model'],
            'checked_at': registry.now(), 'source': origin + '/props + /settings',
            'template_sha256': hashlib.sha256(template.encode()).hexdigest(),
            'shared_effort': defaults.get('reasoning_effort'),
            'note': '已读取模型模板和共享设置；high 在此引擎映射为 xhigh。仅查询，不修改家中设置。'}


def _interruptible_delay(delay, cancel=None, sleep=None):
    """Sleep in small slices; return False as soon as cancellation is requested."""
    remaining = max(0.0, float(delay))
    sleeper = sleep or time.sleep
    while remaining > 0:
        if cancel is not None and cancel():
            return False
        step = min(PROBE_DELAY_SLICE, remaining)
        sleeper(step)
        remaining -= step
    return not (cancel is not None and cancel())


def probe(model, timeout=PROBE_READ_TIMEOUT, attempts=PROBE_ATTEMPTS, delay=PROBE_RETRY_DELAY,
          cancel=None, sleep=None):
    """Read engine metadata with up to ``attempts`` bounded, cancellable attempts.

    A model-identity mismatch is definitive and never retried.  Every other
    failure is retried up to the (small) attempt bound after an interruptible
    delay; the last error is re-raised so the caller can decide to degrade.
    """
    total = max(1, int(attempts))
    last = None
    for attempt in range(1, total + 1):
        if cancel is not None and cancel():
            raise ThinkingCancelled('思考等级元数据探测已取消')
        try:
            return _probe_once(model, timeout=timeout)
        except ThinkingMismatch:
            raise
        except ThinkingCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - classified by the caller
            last = error
            if attempt >= total:
                break
            if not _interruptible_delay(delay, cancel=cancel, sleep=sleep):
                raise ThinkingCancelled('思考等级元数据探测已取消')
    if last is None:
        raise ThinkingUnavailable('思考等级元数据不可用')
    raise last


def _try_store(model, info):
    """Refresh the engine metadata cache when the registry row still matches.

    Returns ``{'stored', 'kind', 'reason'}``.  ``kind`` distinguishes an identity
    problem (the row changed: never claim we stored anything) from a transient
    cache write failure.
    """
    model_id = model.get('id')
    if not model_id:
        return {'stored': False, 'kind': 'no_record', 'reason': '任务模型没有注册表记录'}
    try:
        with registry.registry_lock():
            data = registry.read_registry()
            row = next((m for m in data['models'] if m.get('id') == model_id), None)
            if row is None:
                return {'stored': False, 'kind': 'missing', 'reason': '模型记录已不存在'}
            if row.get('api_base') != model.get('api_base') or row.get('model') != model.get('model'):
                return {'stored': False, 'kind': 'identity', 'reason': '模型配置已变化'}
            row['thinking_info'] = info
            registry.write_json(registry.REGISTRY, data)
        return {'stored': True, 'kind': '', 'reason': ''}
    except (OSError, ValueError):
        return {'stored': False, 'kind': 'io', 'reason': '元数据缓存写入失败'}


def probe_and_store(model_id):
    """Explicit panel action: probe and persist engine metadata for one model."""
    model = registry.get_model(model_id, require_enabled=False)
    if model.get('provider') == 'workbuddy':
        raise ValueError('WorkBuddy 没有工作台 API 元数据探测接口，请使用原生默认')
    info = probe(model)
    result = _try_store(model, info)
    if not result['stored']:
        if result['kind'] in {'identity', 'missing'}:
            raise ValueError('模型配置已变化，请重新读取默认等级')
        raise ValueError('引擎元数据缓存写入失败，请稍后重试（未改动用户预设）')
    return profile({**model, 'thinking_info': info})


def _probe_at_start(model):
    """Only the local home model is re-probed at task start.

    Other cards keep using their stored metadata; the per-task probe never adds
    engine traffic for models the user did not ask to re-read.
    """
    return model.get('provider') == 'local' and model.get('id') == 'home'


def prepare_for_task(model, cancel=None, probe_fn=None, moment=None, sleep=None):
    """Resolve the thinking metadata that one real task may use.

    Returns ``(info, evidence)``.  ``info`` is the profile applied to the task's
    requests; ``evidence`` is a compact, credential-free per-task record.  Raises
    ``ThinkingMismatch`` for an exact model mismatch, ``ThinkingUnavailable`` when
    an explicitly chosen level cannot be supported by trusted metadata, and
    ``ThinkingCancelled`` when the caller cancelled.
    """
    if deepseek_billing.is_official(model):
        return profile(model), {'schema': 1, 'status': 'documented',
                                'selected': model.get('thinking_level', 'default'),
                                'source': DEEPSEEK_DOC, 'verified_for_task': False,
                                'note': '官方文档等级；未探测引擎元数据。'}
    selected = model.get('thinking_level', 'default')
    evidence = {'schema': 1, 'selected': selected, 'verified_for_task': False,
                'cache_max_age_seconds': CACHE_MAX_AGE_SECONDS}
    failure = None
    if _probe_at_start(model):
        try:
            info = (probe_fn or probe)(model, cancel=cancel, sleep=sleep)
        except ThinkingMismatch as error:
            raise ThinkingMismatch('引擎当前加载的模型与此卡片不一致，已停止且未发送任何推理请求：'
                                   + str(error)[:200])
        except ThinkingCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - degrade or refuse below
            failure = error
        else:
            store = _try_store(model, info)
            evidence.update({'status': STATUS_VERIFIED, 'verified_for_task': True,
                             'source': info.get('source'), 'checked_at': info.get('checked_at'),
                             'attempts': PROBE_ATTEMPTS, 'cached': store['stored'],
                             'note': '本次任务已读取引擎默认等级；只刷新引擎元数据缓存，不修改用户预设。'
                                     + (('（元数据缓存未更新：' + store['reason'] + '）') if not store['stored'] else '')})
            return info, evidence
    cached = compatible_info(model, moment=moment)
    if cached is not None:
        age = cache_age_seconds(cached, moment)
        evidence.update({'status': 'cached', 'verified_for_task': False,
                         'cache_age_seconds': round(age, 1) if age is not None else None,
                         'source': cached.get('source'),
                         'note': '复用同一模型身份、%d 秒内的已验证元数据；本轮未做新的引擎探测。'
                                 % CACHE_MAX_AGE_SECONDS})
        return profile({**model, 'thinking_info': cached}), evidence
    if selected == 'default':
        # Degrade to the engine default: no thinking control is injected (see
        # ``apply``) and the record states plainly that nothing was verified.
        degraded = {'adapter': 'unknown', 'supported': ['default'], 'default_effort': None,
                    'default_enabled': None, 'status': STATUS_DEGRADED if failure is not None else STATUS_UNKNOWN,
                    'api_base': model.get('api_base'), 'model': model.get('model'),
                    'probe_status': 'unavailable', 'probe_attempts': PROBE_ATTEMPTS if failure is not None else 0}
        evidence.update({'status': STATUS_DEGRADED if failure is not None else STATUS_UNKNOWN,
                         'category': 'metadata_unavailable',
                         'attempts': PROBE_ATTEMPTS if failure is not None else 0,
                         'error_type': type(failure).__name__ if failure is not None else None,
                         'detail': ('引擎元数据不可用，本次按引擎默认执行，不注入未经核实的思考等级参数'
                                    if failure is not None else '尚无该模型的引擎默认元数据，本次按引擎默认执行'),
                         'note': '降级执行：本轮未核实任何思考等级，未声称已核实。'})
        return profile({**model, 'thinking_info': degraded}), evidence
    raise ThinkingUnavailable(
        '无法确认该模型支持所选思考等级“%s”：引擎元数据不可用，或缓存已超过 %d 秒。'
        '请把等级改回“沿用默认”，或在面板重新读取引擎默认后再派发；本次未发送任何推理请求。'
        % (selected, CACHE_MAX_AGE_SECONDS))


def validate(model):
    value = model.get('thinking_level', 'default')
    if not isinstance(value, str) or value not in LABELS:
        raise ValueError('思考等级格式不正确')
    if value not in profile(model)['supported']:
        raise ValueError('此模型不支持选择该思考等级，请先读取引擎默认并选择已支持的选项')
    return value


def apply(body, model):
    value = validate(model)
    info = profile(model)
    # The stored selection owns these fields, rather than incidental DSH defaults.
    for field in ('reasoning_effort', 'thinking', 'enable_thinking', 'reasoning'):
        body.pop(field, None)
    if body.get('chat_template_kwargs') is not None and not isinstance(body['chat_template_kwargs'], dict):
        raise ValueError('聊天模板参数必须是对象')
    kwargs = dict(body.get('chat_template_kwargs') or {})
    for field in ('enable_thinking', 'reasoning_effort'):
        kwargs.pop(field, None)
    if kwargs:
        body['chat_template_kwargs'] = kwargs
    else:
        body.pop('chat_template_kwargs', None)
    if value != 'default':
        if info['adapter'] == 'strata':
            body['reasoning_effort'] = 'none' if value == 'off' else value
            body['chat_template_kwargs'] = {**kwargs, 'enable_thinking': value != 'off'}
            if value != 'off':
                body['chat_template_kwargs']['reasoning_effort'] = value
        elif info['adapter'] == 'deepseek':
            body['thinking'] = {'type': 'disabled' if value == 'off' else 'enabled'}
            if value != 'off':
                body['reasoning_effort'] = value
    return info
