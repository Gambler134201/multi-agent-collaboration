"""Lightweight, extensible token metering with explicit provenance and trust.

Why this module exists
----------------------
The gateway used to hard-reject a request when
``len(json_utf8) + 1024 + message_count * 64 + max_tokens`` exceeded the model's
``max_input_tokens``. That is a byte bound, not a token count: for CJK/code text
the true token count is several times smaller, so legitimate requests were
rejected before they were ever sent. This module replaces that arithmetic with a
measurement record that always states *what* was measured, *how*, *how much it
can be trusted*, and *which model/template identity* it applies to.

Trust model
-----------
* ``exact``       - the real tokenizer/deployment counted the real payload.
* ``approximate`` - a proxy estimate (bytes, content classes) with no verified
                    tokenizer/template match. Never used to hard-reject.
* ``unavailable`` - metering failed or is unsupported. Warning only.

Only an ``exact`` count whose *window semantics* are also verified may reject a
request locally. Anything less is recorded as a warning and the request is sent,
so the provider/engine - which owns the real tokenizer and the real window - is
the one that decides. Approximate values must never be presented as exact, and
the byte arithmetic above is deliberately never labelled exact.

Window semantics are per-vendor, not one formula. A vendor that publishes a
single total context window (prompt + generated must fit) and a vendor that
publishes a separate prompt window plus a separate generation cap are different
shapes; they are described by separate ``WINDOW_SPECS`` entries and never
collapsed into one rule.
"""
from dataclasses import dataclass
import hashlib
import json
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request, ProxyHandler, build_opener

SCHEMA = 1

EXACT = 'exact'
APPROXIMATE = 'approximate'
UNAVAILABLE = 'unavailable'
CONFIDENCES = (EXACT, APPROXIMATE, UNAVAILABLE)

# Token-source identities. A source is only usable for enforcement when it is
# matched to the deployment identity *and* its counts were verified against that
# deployment; see ``verified_source``.
SOURCE_ENGINE_REPORT = 'engine_report'
SOURCE_ENGINE_TOKENIZE = 'engine_tokenize'
SOURCE_OFFLINE_TOKENIZER = 'offline_tokenizer'
SOURCE_BYTE_PROXY = 'json_utf8_byte_proxy'
SOURCE_NONE = 'unavailable'

IMPLEMENTED_METHODS = (SOURCE_ENGINE_TOKENIZE, SOURCE_OFFLINE_TOKENIZER)

# Window-semantics kinds. Distinct shapes stay distinct.
WINDOW_COMBINED = 'combined_context_window'   # prompt + max_tokens <= window
WINDOW_SPLIT = 'split_input_output_windows'   # prompt <= input_window and cap <= output_window
WINDOW_UNVERIFIED = 'unverified_window'

# Byte classes used by the labelled approximate proxy. UTF-8 byte counts are
# divided by a class-specific conservative divisor; ASCII keeps ~4 bytes/token,
# CJK ~3 bytes/token, and everything else ~3. These are estimates, labelled as
# such, and are never allowed to reject a request.
_CJK_RANGES = (
    (0x1100, 0x11FF), (0x2E80, 0x2EFF), (0x3000, 0x303F), (0x3040, 0x30FF),
    (0x3130, 0x318F), (0x31C0, 0x31EF), (0x3200, 0x4DBF), (0x4E00, 0x9FFF),
    (0xA960, 0xA97F), (0xAC00, 0xD7FF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F),
    (0xFF00, 0xFFEF), (0x20000, 0x3FFFF),
)
_ASCII_DIVISOR = 4.0
_CJK_DIVISOR = 3.0
_OTHER_DIVISOR = 3.0
_PER_MESSAGE_TOKENS = 5
_PRIMING_TOKENS = 4

# Bounded auxiliary metering (never generation, never a paid call).
PROBE_TIMEOUT = 6.0
PROBE_MAX_BYTES = 65536
PROBE_TTL = 900.0
METRICS_TTL = 60.0
TIKTOKEN_LOAD_TIMEOUT = 20.0

_lock = threading.RLock()
_candidates_cache = None
_probe_cache = {}
_metrics_cache = {}
_calibration = {}


@dataclass(frozen=True)
class MeterIdentity:
    """The deployment identity a measurement is valid for.

    ``template_sha256`` is part of identity because the same model served by a
    different chat template tokenizes differently. A stored verification that
    does not match this identity must not be reused.
    """
    provider: str = ''
    api_base: str = ''
    model: str = ''
    template_sha256: str = ''

    def as_dict(self):
        return {'provider': self.provider, 'api_base': self.api_base,
                'model': self.model, 'template_sha256': self.template_sha256}

    def matches(self, other):
        if not isinstance(other, dict):
            return False
        return all((other.get(name) or '') == getattr(self, name) for name in
                   ('provider', 'api_base', 'model', 'template_sha256'))


@dataclass(frozen=True)
class WindowSpec:
    """How one vendor's window is shaped; kept per vendor, never merged."""
    kind: str = WINDOW_UNVERIFIED
    input_window: int | None = None
    output_cap: int | None = None
    source: str = ''
    verified: bool = False


# Per-vendor window shapes. These constants describe the *shape* of each
# vendor's rule; they are not a claim that a given deployment was verified.
# ``verified`` stays False until evidence exists for the concrete deployment.
WINDOW_SPECS = {
    # Single total context window: prompt + generation must fit inside it.
    'local': {'kind': WINDOW_COMBINED, 'source': 'llama.cpp-compatible /props n_ctx and /health max_context; prompt + max_tokens share it',
              'note': '家用引擎按总量窗口计，输入与输出共享同一上限。'},
    # Prompt window and generation cap are published separately.
    'deepseek': {'kind': WINDOW_SPLIT, 'source': 'DeepSeek 官方文档：上下文与最大输出分别限制',
                 'note': '官方模型输入窗口与最大输出分列，不与总量窗口规则混用。'},
    'glm': {'kind': WINDOW_SPLIT, 'source': '第三方 OpenAI 兼容：输入与输出分列限制',
            'note': '第三方兼容接口按输入窗口与输出上限分列。'},
    'custom': {'kind': WINDOW_SPLIT, 'source': '第三方自定义：输入与输出分列限制',
               'note': '自定义接口按输入窗口与输出上限分列。'},
}


def window_spec(model):
    """Return the window shape for a model row, or an explicit unverified spec."""
    provider = str((model or {}).get('provider') or 'custom')
    row = WINDOW_SPECS.get(provider)
    declared = (model or {}).get('token_window')
    spec = WindowSpec(kind=WINDOW_UNVERIFIED, source='未确认厂商窗口规则')
    if row:
        spec = WindowSpec(kind=row['kind'], source=row['source'], verified=False)
    if isinstance(declared, dict) and str(declared.get('kind') or '') in {
            WINDOW_COMBINED, WINDOW_SPLIT}:
        spec = WindowSpec(kind=str(declared['kind']),
                          input_window=_positive_int(declared.get('input_window')),
                          output_cap=_positive_int(declared.get('output_cap')),
                          source=str(declared.get('source') or '模型记录声明的窗口规则'),
                          verified=bool(declared.get('verified')))
    # The registry's max_input_tokens is the configured input/context bound and
    # max_output_tokens the configured generation bound; both stay advisory
    # unless the semantics for this deployment were verified.
    if spec.input_window is None:
        spec = WindowSpec(kind=spec.kind, input_window=_positive_int((model or {}).get('max_input_tokens')),
                          output_cap=spec.output_cap, source=spec.source, verified=spec.verified)
    if spec.output_cap is None:
        spec = WindowSpec(kind=spec.kind, input_window=spec.input_window,
                          output_cap=_positive_int((model or {}).get('max_output_tokens')),
                          source=spec.source, verified=spec.verified)
    return spec


def output_ceiling(model):
    """Generation capability bound published for the model (not this request)."""
    return _positive_int((model or {}).get('max_output_tokens'))


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def resolve_output_target(body, model, task_target=None):
    """Separate the request's generation target from the model capability cap.

    Precedence: an explicit ``max_tokens`` in the body, then the task-level
    ``request_output_tokens``, then a documented fallback estimate. The fallback
    is an estimate of one request's output, never a claim that the model cannot
    produce more.
    """
    body = body if isinstance(body, dict) else {}
    ceiling = output_ceiling(model)
    target = None
    origin = None
    if 'max_tokens' in body:
        value = body.get('max_tokens')
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError('模型输出上限格式不正确')
        target, origin = value, 'explicit_max_tokens'
    elif task_target is not None:
        if isinstance(task_target, bool) or not isinstance(task_target, int) or task_target < 1:
            raise ValueError('任务级输出目标格式不正确')
        target, origin = task_target, 'task_request_output_tokens'
    requested = target
    task_limit_applied = False
    if task_target is not None:
        if isinstance(task_target, bool) or not isinstance(task_target, int) or task_target < 1:
            raise ValueError('任务级输出目标格式不正确')
        if target is not None and task_target < target:
            target = task_target
            origin = 'explicit_max_tokens_task_limit'
            task_limit_applied = True
    capped = None
    if target is not None and ceiling is not None:
        # An explicit or task-level target is compatible input; it is limited to
        # the model capability rather than rejected for exceeding it.
        capped = min(target, ceiling)
    elif target is not None:
        capped = target
    elif ceiling is not None:
        capped, origin = ceiling, 'configured_model_default'
    else:
        capped, origin = 4096, 'fallback_estimate_4096'
    return {'target': capped, 'requested': requested, 'origin': origin,
            'task_limit_applied': task_limit_applied, 'task_limit': task_target,
            'capped_to_model_limit': bool(target is not None and ceiling is not None and target > ceiling),
            'capability_ceiling': ceiling}


def resolve_request_output_target(body):
    """Late-bound output resolution used by the relay before metering.

    Precedence walks the *body as it exists at this point*: an explicit
    ``max_tokens`` left by the caller or the thinking layer, then a task-level
    ``request_output_tokens``, then a documented 4096 estimate of this request's
    output. The estimate is never treated as the model's reasoning ceiling.
    """
    body = body if isinstance(body, dict) else {}
    if 'max_tokens' in body:
        value = body.get('max_tokens')
        if not _positive_int(value):
            raise ValueError('模型输出上限格式不正确')
        return value, 'explicit_max_tokens'
    if _positive_int(body.get('request_output_tokens')):
        return int(body['request_output_tokens']), 'task_request_output_tokens'
    return 4096, 'fallback_estimate_4096'


# --------------------------------------------------------------------------
# Candidate sources: declared precision, and what would be needed to trust them
# --------------------------------------------------------------------------

def implemented_candidates():
    """What this process can actually run locally, with honest precision claims."""
    return [
        {'source': SOURCE_ENGINE_REPORT, 'precision': EXACT,
         'implemented': True,
         'note': '请求发出后由服务商 usage 返回的真实 prompt_tokens；只能用于事后校准与结算，不能用于发出前判定。'},
        {'source': SOURCE_ENGINE_TOKENIZE, 'precision': EXACT,
         'implemented': True,
         'requires': '引擎提供只读完整聊天计数接口（含模板与 tools），且响应可按模型版本/模板匹配',
         'note': '引擎端完整聊天计数，是本地判定的首选来源；接口不存在或不覆盖模板/tools 时不得声明精确。'},
        {'source': SOURCE_OFFLINE_TOKENIZER, 'precision': EXACT,
         'implemented': True,
         'matched_by': 'cache/token-meter-sources.json 的模型版本 + 模板 sha256 匹配记录',
         'note': '离线 tokenizer 只有在模型版本与模板匹配验证通过后才可作为精确来源。'},
        {'source': SOURCE_BYTE_PROXY, 'precision': APPROXIMATE, 'implemented': True,
         'note': '按 UTF-8 字节分内容类的保守近似；不声明精确，也不用于硬拦截。'},
    ]


def verified_source(model, props=None, evidence=None, source=SOURCE_ENGINE_TOKENIZE):
    """Return the verified source record for this identity, or None.

    A record only counts when its ``identity`` matches the live model/template
    identity, its ``verified`` flag is set, and its evidence shows the same
    source. Nothing here upgrades a fallback tokenizer (for example tiktoken) or
    a count that ignores tools into an exact source.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    identity = cache_identity(model, props)
    rows = evidence.get('verified_sources')
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or row.get('source') != source or not row.get('verified'):
            continue
        if identity.matches(row.get('identity')):
            return row
    return None


def cache_identity(model, props=None):
    """Identity used to key any cached metering capability for a deployment."""
    model = model or {}
    template = ''
    if isinstance(props, dict):
        template = str(props.get('chat_template') or '')
    elif isinstance(model.get('thinking_info'), dict) and \
            model['thinking_info'].get('api_base') == model.get('api_base') and \
            model['thinking_info'].get('model') == model.get('model'):
        template = 'sha256:' + str(model['thinking_info'].get('template_sha256') or '')
    if template.startswith('sha256:'):
        digest = template[len('sha256:'):]
    else:
        digest = hashlib.sha256(template.encode('utf-8')).hexdigest() if template else ''
    return MeterIdentity(provider=str(model.get('provider') or ''),
                         api_base=str(model.get('api_base') or ''),
                         model=str(model.get('model') or ''),
                         template_sha256=digest)


def identity_matches(model, identity):
    return cache_identity(model).matches(identity.as_dict() if isinstance(identity, MeterIdentity) else identity)


# --------------------------------------------------------------------------
# Optional offline tokenizer (never trusted without matcher verification)
# --------------------------------------------------------------------------

def _encoding_names(model):
    explicit = (model or {}).get('tokenizer_encoding')
    if isinstance(explicit, str) and explicit.strip():
        return [explicit.strip()]
    return ['o200k_base']


def _load_candidates(model):
    global _candidates_cache
    with _lock:
        if _candidates_cache is None:
            loaded = {}
            try:
                import tiktoken
            except Exception:
                tiktoken = None
            if tiktoken is not None:
                for name in _encoding_names(model):
                    try:
                        loaded[name] = tiktoken.get_encoding(name)
                    except Exception:
                        continue
            _candidates_cache = loaded
        return _candidates_cache


def warm_offline_tokenizer(model):
    """Load the optional offline tokenizer once, with a bounded wait.

    A failure here is a metering limitation, not a model failure: the caller
    keeps the labelled approximate proxy and must not surface this as an API
    error. The wait is bounded so a slow/absent tokenizer package cannot stall
    a request path.
    """
    box = {}

    def load():
        try:
            box['result'] = _load_candidates(model)
        except BaseException as error:  # noqa: BLE001 - reported as a limitation
            box['error'] = error

    thread = threading.Thread(target=load, daemon=True)
    thread.start()
    thread.join(TIKTOKEN_LOAD_TIMEOUT)
    if thread.is_alive():
        return {'available': False, 'reason': '离线 tokenizer 加载超时，本阶段按近似计并只告警'}
    if box.get('error') is not None:
        return {'available': False, 'reason': '离线 tokenizer 不可用：' + type(box['error']).__name__}
    loaded = box.get('result') or {}
    return {'available': bool(loaded), 'encodings': sorted(loaded),
            'reason': '' if loaded else '离线 tokenizer 不可用，按近似计并只告警'}


def count_with_offline_tokenizer(model, text):
    """Return a candidate count with the identity of the encoding that produced it.

    The result is only ``exact`` for this deployment when ``verified_source``
    says the encoding was verified against the same model version and template;
    otherwise the caller must label it approximate.
    """
    if not isinstance(text, str):
        return None
    loaded = _load_candidates(model)
    if not loaded:
        return None
    name, encoding = next(iter(loaded.items()))
    try:
        tokens = encoding.encode(text)
    except Exception:
        return None
    return {'count': len(tokens), 'encoding': name, 'precision': APPROXIMATE,
            'identity': {'model': str((model or {}).get('model') or ''), 'encoding': name}}


# --------------------------------------------------------------------------
# Labelled approximate proxy (the old byte arithmetic, kept only as an estimate)
# --------------------------------------------------------------------------

def byte_class_counts(text):
    """UTF-8 byte counts split by class, for the labelled proxy estimate."""
    if not isinstance(text, str):
        text = str(text or '')
    data = text.encode('utf-8', 'surrogatepass')
    ascii_bytes = cjk_bytes = other_bytes = 0
    for character in text:
        point = ord(character)
        size = len(character.encode('utf-8', 'surrogatepass'))
        if point < 0x80:
            ascii_bytes += size
        elif any(low <= point <= high for low, high in _CJK_RANGES):
            cjk_bytes += size
        else:
            other_bytes += size
    return {'ascii_bytes': ascii_bytes, 'cjk_bytes': cjk_bytes,
            'other_bytes': other_bytes, 'utf8_bytes': len(data)}


def estimate_tokens(text):
    """Conservative content-class estimate; deterministic, offline, labelled."""
    counts = byte_class_counts(text)
    total = (counts['ascii_bytes'] / _ASCII_DIVISOR + counts['cjk_bytes'] / _CJK_DIVISOR
             + counts['other_bytes'] / _OTHER_DIVISOR)
    return {'tokens': int(total + 0.999), 'bytes': counts['utf8_bytes'], 'classes': counts}


def render_template(messages, tools):
    """Deterministic stand-in for the chat template so template text is counted."""
    parts = []
    for message in messages or []:
        content = message.get('content', '')
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        parts.append(str(message.get('role', '')) + ': ' + content)
        for call in message.get('tool_calls') or []:
            parts.append(json.dumps(call, ensure_ascii=False, sort_keys=True))
        if message.get('tool_call_id'):
            parts.append(str(message['tool_call_id']))
    for tool in tools or []:
        parts.append(json.dumps(tool, ensure_ascii=False, sort_keys=True))
    return '\n'.join(parts)


def estimate_request(text, template, message_count=0):
    """Approximate prompt-side tokens: body text + rendered template + overhead."""
    body = estimate_tokens(text)
    tpl = estimate_tokens(template)
    tokens = body['tokens'] + tpl['tokens'] + _PRIMING_TOKENS + _PER_MESSAGE_TOKENS * max(0, int(message_count))
    classes = {name: int(body['classes'][name]) + int(tpl['classes'][name])
               for name in ('ascii_bytes', 'cjk_bytes', 'other_bytes')}
    classes['utf8_bytes'] = int(body['bytes']) + int(tpl['bytes'])
    classes['message_overhead_tokens'] = _PRIMING_TOKENS + _PER_MESSAGE_TOKENS * max(0, int(message_count))
    return {'tokens': int(tokens), 'bytes': body['bytes'] + tpl['bytes'],
            'body_tokens': body['tokens'], 'template_tokens': tpl['tokens'],
            'estimate_classes': classes}


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def _window_fit(spec, input_tokens, output_cap):
    """Evaluate the window shape; only an exact+verified measurement may reject."""
    if spec is None or spec.kind == WINDOW_UNVERIFIED:
        return {'state': 'unknown', 'detail': '窗口规则未验证，不作本地判定',
                'semantics_verified': False}
    if not input_tokens or input_tokens.get('precision') != EXACT:
        return {'state': 'unknown', 'detail': '缺少精确输入计数，不作本地判定',
                'semantics_verified': bool(spec.verified)}
    count = int(input_tokens.get('tokens') or 0)
    cap = output_cap if isinstance(output_cap, int) and output_cap > 0 else 0
    if spec.kind == WINDOW_COMBINED:
        if spec.input_window is None:
            return {'state': 'unknown', 'detail': '总量窗口上限未知', 'semantics_verified': False}
        total = count + cap
        return {'state': 'over' if total > spec.input_window else 'ok',
                'total': total, 'window': spec.input_window,
                'detail': f'输入 {count} + 输出上限 {cap} 与总量窗口 {spec.input_window} 比较',
                'semantics_verified': bool(spec.verified)}
    if spec.kind == WINDOW_SPLIT:
        problems = []
        if spec.input_window is not None and count > spec.input_window:
            problems.append(f'输入 {count} 超过输入窗口 {spec.input_window}')
        if spec.output_cap is not None and cap > spec.output_cap:
            problems.append(f'输出上限 {cap} 超过模型输出能力 {spec.output_cap}')
        return {'state': 'over' if problems else 'ok', 'window': spec.input_window,
                'detail': '；'.join(problems) or f'输入 {count} 与输出上限 {cap} 分别在各自窗口内',
                'semantics_verified': bool(spec.verified)}
    return {'state': 'unknown', 'detail': '未知窗口类型', 'semantics_verified': False}


def measure(model, body, output_target=None, task_target=None, props=None,
            evidence=None, declared_window=None, allow_offline_tokenizer=False,
            engine_tokenize=None, llm_ceiling=None):
    """Measure one request and decide whether a locally-provable rejection exists.

    Returns a complete record: value, unit, source, trust, model/template
    identity, input/output window semantics and warnings. The record never
    claims a count that ignores tools or templates is exact.

    ``engine_tokenize`` is a callable that asks the engine for a *complete chat*
    count (template and tools included). It is only consulted when an evidence
    record verifies that source for this exact model/template identity, so a
    fragment-only endpoint cannot be promoted to exact by wiring alone.

    ``output_target`` may carry an already-resolved goal (with its provenance
    intact, because the relay normalizes the body before metering);
    ``llm_ceiling`` is the context bound the request will actually be sent with.
    """
    body = body if isinstance(body, dict) else {}
    messages = [m for m in body.get('messages') or [] if isinstance(m, dict)]
    tools = [t for t in body.get('tools') or [] if isinstance(t, dict)]
    text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    template = render_template(messages, tools)

    if isinstance(output_target, dict):
        resolved = dict(output_target)
    else:
        resolved = resolve_output_target(body, model, task_target)
    if isinstance(llm_ceiling, int) and not isinstance(llm_ceiling, bool) and llm_ceiling > 0:
        resolved = {**resolved, 'capability_ceiling': llm_ceiling}
        if isinstance(resolved.get('target'), int) and not isinstance(resolved['target'], bool):
            resolved['capped_to_model_limit'] = bool(resolved['target'] > llm_ceiling)
            resolved['target'] = min(resolved['target'], llm_ceiling)
    warnings = []
    input_measure = None
    identity = cache_identity(model, props)

    verified_engine = verified_source(model, props, evidence, SOURCE_ENGINE_TOKENIZE)
    verified_offline = verified_source(model, props, evidence, SOURCE_OFFLINE_TOKENIZER)

    if verified_engine is not None and callable(engine_tokenize):
        raw = None
        try:
            raw = engine_tokenize(body)
        except Exception:
            raw = None
        count = raw.get('tokens') if isinstance(raw, dict) else raw
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            input_measure = {
                'tokens': count, 'precision': EXACT, 'source': SOURCE_ENGINE_TOKENIZE,
                'method': str((raw or {}).get('method') or 'engine_chat_count') if isinstance(raw, dict)
                else 'engine_chat_count',
                'covers_tools': (raw.get('covers_tools') is True) if isinstance(raw, dict) else False,
                'covers_template': (raw.get('covers_template') is True) if isinstance(raw, dict) else False,
                'note': '引擎端完整聊天计数，已按模型版本与模板验证'}
            if not (input_measure['covers_tools'] and input_measure['covers_template']):
                # A partial engine count is not an exact full-request count.
                input_measure = None
        if input_measure is None:
            warnings.append('引擎端完整聊天计数不可用或结果不完整，退回标注为 approximate 的估计。')
    if input_measure is None and verified_offline is not None:
        candidate = count_with_offline_tokenizer(model, text) if allow_offline_tokenizer else None
        if candidate is not None and str(candidate.get('encoding') or '') == str(verified_offline.get('encoding') or ''):
            # Encoding the transport JSON is not the deployment's rendered
            # chat prompt. Identity metadata alone cannot make it exact.
            input_measure = {'tokens': candidate['count'], 'precision': APPROXIMATE,
                             'source': SOURCE_OFFLINE_TOKENIZER, 'method': candidate['encoding'],
                             'covers_tools': False, 'covers_template': False,
                             'note': '离线 tokenizer 对请求JSON计数，尚未使用部署的完整聊天模板；仅作近似'}
    if input_measure is None:
        estimate = estimate_request(text, template, len(messages))
        input_measure = {'tokens': estimate['tokens'], 'precision': APPROXIMATE,
                         'source': SOURCE_BYTE_PROXY, 'method': 'content_class_byte_proxy',
                         'covers_tools': False,
                         'covers_template': False,
                         'note': '按 UTF-8 字节分内容类的保守估计，未验证 tokenizer；只告警不拦截',
                         'body_tokens': estimate['body_tokens'],
                         'template_tokens': estimate['template_tokens'],
                         'estimate_classes': estimate['estimate_classes']}
        warnings.append('token 计量为近似值（来源 '
                        + SOURCE_BYTE_PROXY + '，信任度 approximate）；本阶段不据此判定超窗，'
                        '由引擎或服务商决定。')

    spec = declared_window if isinstance(declared_window, WindowSpec) else window_spec(model)
    fit = _window_fit(spec, input_measure, resolved.get('target'))
    # The only locally provable rejection: an exact count plus window semantics
    # that were verified for this deployment.
    enforceable = (spec.verified and bool(fit.get('semantics_verified'))
                   and input_measure['precision'] == EXACT)
    if fit['state'] == 'unknown':
        warnings.append('窗口语义不可用或不完整（' + str(fit.get('detail')) + '）；只记录不拦截。')
        guard = 'proceed'
    elif fit['state'] == 'over' and enforceable:
        guard = 'reject'
    elif fit['state'] == 'over':
        warnings.append('估计超窗但缺少精确且经证实的窗口语义，不拦截：' + str(fit.get('detail')))
        guard = 'warn'
    else:
        guard = 'proceed'

    return {
        'schema': SCHEMA,
        'model_id': str(model.get('id') or ''),
        'model': str(model.get('model') or ''),
        'provider': str(model.get('provider') or ''),
        'identity': identity.as_dict(),
        'input': input_measure,
        'output': {'target': resolved.get('target'), 'requested': resolved.get('requested'),
                   'origin': resolved.get('origin'),
                   'capped_to_model_limit': resolved.get('capped_to_model_limit'),
                   'unit': 'tokens', 'kind': 'request_output_target',
                   'capability_ceiling': resolved.get('capability_ceiling'),
                   'note': ('未显式指定输出上限，按 4096 估计本次请求输出；不是模型推理总输出硬上限。'
                            if resolved.get('origin') == 'fallback_estimate_4096' else
                            '本次请求输出目标已记录来源，并与模型最大输出能力分开保存。')},
        'window': {'kind': spec.kind, 'input_window': spec.input_window,
                   'output_cap': spec.output_cap, 'source': spec.source,
                   'semantics_verified': bool(spec.verified), 'fit': fit,
                   'enforceable': enforceable},
        'guard': guard,
        'warnings': warnings,
        'sources': {'used': input_measure['source'], 'precision': input_measure['precision'],
                    'method': input_measure['method'], 'covers_tools': input_measure['covers_tools'],
                    'covers_template': input_measure['covers_template']},
    }


def guard_message(record):
    """Human-facing rejection text carrying the measurement provenance."""
    fit = record.get('window', {}).get('fit', {})
    return ('请求超过已证实的上下文窗口：' + str(fit.get('detail'))
            + '。计量来源 ' + str(record.get('sources', {}).get('used'))
            + '（' + str(record.get('sources', {}).get('precision')) + '）。')


# --------------------------------------------------------------------------
# Post-hoc calibration (observability only; never feeds the pre-send estimate)
# --------------------------------------------------------------------------

def record_calibration(model, record, usage):
    """Correlate the estimate with the provider's real prompt_tokens.

    This is explicitly post-hoc observability: the returned observation must not
    be presented as a pre-send exact count, and it does not change the estimate.
    """
    if not isinstance(usage, dict):
        return None
    reported = usage.get('prompt_tokens')
    if isinstance(reported, bool) or not isinstance(reported, int) or reported < 1:
        return None
    if not isinstance(record, dict):
        return None
    source = record.get('sources', {}).get('used')
    if source == SOURCE_ENGINE_REPORT:
        return None
    identity = record.get('identity') or {}
    key = '|'.join(str(identity.get(name) or '') for name in ('provider', 'model', 'template_sha256'))
    estimate = int((record.get('input') or {}).get('tokens') or 0)
    classes = ((record.get('input') or {}).get('estimate_classes') or {})
    observation = {'estimate_tokens': estimate, 'actual_prompt_tokens': reported,
                   'source': source, 'precision': record.get('sources', {}).get('precision'),
                   'model': identity.get('model'), 'at': time.time(),
                   'estimate_classes': {name: classes.get(name) for name in
                                        ('ascii_bytes', 'cjk_bytes', 'other_bytes', 'utf8_bytes')},
                   'note': '事后校准观测：真实 prompt_tokens 来自服务商 usage，不是发出前精确计数。'}
    with _lock:
        row = _calibration.setdefault(key, {'observed': 0, 'estimate_total': 0,
                                            'actual_total': 0, 'class_bytes': {},
                                            'class_tokens': {}})
        row['observed'] += 1
        row['estimate_total'] += estimate
        row['actual_total'] += reported
        for name in ('ascii_bytes', 'cjk_bytes', 'other_bytes'):
            value = classes.get(name)
            if isinstance(value, int):
                row['class_bytes'][name] = row['class_bytes'].get(name, 0) + value
        row['class_tokens']['actual'] = row['class_tokens'].get('actual', 0) + reported
        row['last'] = observation
        row['ratio'] = round(row['estimate_total'] / row['actual_total'], 4) if row['actual_total'] else None
        row['bytes_per_actual_token'] = {
            name: round(value / row['actual_total'], 3)
            for name, value in row['class_bytes'].items() if row['actual_total']}
        row['note'] = ('按内容类累计字节与真实 prompt_tokens 的相关性观测；'
                       '累计值不做因果推断，也不回灌发出前估计或判定。')
    return observation


def calibration_snapshot():
    with _lock:
        return json.loads(json.dumps(_calibration, default=str))


def reset_calibration():
    with _lock:
        _calibration.clear()


def apply_calibration(record, usage):
    """Attach a calibration observation to a meter record; never raises."""
    try:
        return record_calibration(record, record, usage)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Read-only capability probe (bounded, cached, never a generation request)
# --------------------------------------------------------------------------

def _http_read(origin, path, headers, bypass_proxy, timeout=PROBE_TIMEOUT):
    opener = build_opener(ProxyHandler({})) if bypass_proxy else build_opener()
    request = Request(origin + path, headers=headers, method='GET')
    with opener.open(request, timeout=timeout) as response:
        data = response.read(PROBE_MAX_BYTES)
    return json.loads(data)


# Named separately so the request path and tests can substitute a single
# bounded GET without touching the POST helper.
_http_get = _http_read


def _http_post(origin, path, headers, bypass_proxy, payload, timeout=PROBE_TIMEOUT):
    opener = build_opener(ProxyHandler({})) if bypass_proxy else build_opener()
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = Request(origin + path, data=body, method='POST',
                      headers={**headers, 'Content-Type': 'application/json'})
    with opener.open(request, timeout=timeout) as response:
        data = response.read(PROBE_MAX_BYTES)
    return json.loads(data)


def probe_metering_capability(model, samples=("{'role': 'user', 'content': 'Hello 你好'}",),
                              force=False, clock=time.monotonic):
    """Read-only probe of what a deployment exposes for token metering.

    Never sends a generation request, never changes engine settings, and never
    raises network errors as model failures: every branch returns a report with
    ``supported`` / ``unverified`` statements. Successful results are cached per
    deployment identity so the request path does not re-read large props.
    """
    model = model or {}
    identity = cache_identity(model)
    key = '|'.join((identity.provider, identity.api_base, identity.model, identity.template_sha256))
    now = clock()
    with _lock:
        cached = _probe_cache.get(key)
    if cached and not force and now - cached['at'] < PROBE_TTL:
        return {**cached['report'], 'cached': True}

    report = {'schema': SCHEMA, 'identity': identity.as_dict(), 'read_only': True,
              'generation_requests': 0, 'tokenize_endpoint': 'unverified',
              'apply_template_endpoint': 'unverified', 'engine_reported_prompt_tokens': 'unverified',
              'props': None, 'notes': [], 'errors': [], 'checked_at_epoch': now}
    base = urlparse(str(model.get('api_base') or ''))
    if base.scheme not in {'http', 'https'} or not base.netloc:
        report['errors'].append('接口地址无效，未探测')
        return report
    origin = f'{base.scheme}://{base.netloc}'
    headers = {'Authorization': 'Bearer ' + model['api_key']} if model.get('api_key') else {}
    bypass = bool(model.get('bypass_proxy'))

    try:
        props = _http_get(origin, '/props', headers, bypass)
        if isinstance(props, dict):
            template = props.get('chat_template')
            report['props'] = {
                'build_info': props.get('build_info'),
                'model_alias': props.get('model_alias'),
                'n_ctx': (props.get('default_generation_settings') or {}).get('n_ctx'),
                'template_sha256': hashlib.sha256(template.encode('utf-8')).hexdigest()
                if isinstance(template, str) else None,
                'template_matches_model': (props.get('model_alias') in (None, '', model.get('model'))),
            }
            if report['props']['template_sha256'] and identity.template_sha256 and \
                    report['props']['template_sha256'] != identity.template_sha256:
                report['notes'].append('引擎当前模板与记录身份不一致，任何缓存计量结论都不再适用。')
    except Exception as error:
        report['errors'].append('读取 /props 失败（' + type(error).__name__ + '），能力按未验证处理')

    for path in ('/tokenize', '/v1/tokenize'):
        try:
            result = _http_post(origin, path, headers, bypass,
                                {'content': 'Hello 你好', 'add_special': True})
            if isinstance(result, dict) and isinstance(result.get('tokens'), list):
                report['tokenize_endpoint'] = path
                report['tokenize_covers_template'] = False
                report['tokenize_covers_tools'] = False
                report['notes'].append('存在 ' + path + '，但它只计片段文本：不含聊天模板与 tools，'
                                       '不满足完整聊天精确计数的要求，不得作为精确来源。')
            else:
                report['notes'].append(path + ' 返回格式不含 tokens，未采纳。')
        except Exception:
            continue
    if report['tokenize_endpoint'] == 'unverified':
        report['notes'].append('未发现可用的 /tokenize 完整聊天计数接口。')

    for path in ('/apply-template', '/v1/apply-template'):
        try:
            result = _http_post(origin, path, headers, bypass,
                                {'messages': [{'role': 'user', 'content': 'Hi'}]})
            if isinstance(result, dict) and 'prompt' in result:
                report['apply_template_endpoint'] = path
                report['notes'].append('存在 ' + path + '，但本阶段未确认它与线上模板/tools 参数一致，'
                                       '仅记录为未验证。')
        except Exception:
            continue
    if report['apply_template_endpoint'] == 'unverified':
        report['notes'].append('未发现可用的 /apply-template 接口。')

    try:
        metrics = _http_get(origin, '/metrics', headers, bypass)
        if isinstance(metrics, dict):
            engine = metrics.get('engine') or {}
            live = metrics.get('live') or {}
            history = metrics.get('requests') if isinstance(metrics.get('requests'), list) else []
            if isinstance(engine.get('max_context'), int):
                report['engine_context_window'] = engine['max_context']
            if live.get('prompt_tokens') is not None or any(
                    isinstance(row, dict) and isinstance(row.get('prompt_tokens'), int) for row in history[:5]):
                report['engine_reported_prompt_tokens'] = 'observed_in_metrics'
                report['notes'].append('/metrics 暴露了引擎实测 prompt_tokens 历史，可用于事后校准，'
                                       '但请求发出前拿不到该值，不能当作发出前精确计数。')
            report['metrics_requests_kept'] = len(history)
    except Exception as error:
        report['errors'].append('读取 /metrics 失败（' + type(error).__name__ + '）')

    report['exact_pre_send_available'] = bool(report['tokenize_endpoint'] != 'unverified'
                                              or report['apply_template_endpoint'] != 'unverified')
    report['conclusion'] = (
        '存在可用的引擎端计数字段，仍需按模型/模板匹配验证后才能声明精确。'
        if report['exact_pre_send_available'] else
        '未发现请求发出前可用的完整聊天 token 计数接口；本部署只能给出标注为 approximate 的估计，'
        '并通过 /metrics 与 usage 做事后校准。')
    with _lock:
        _probe_cache[key] = {'at': now, 'report': report}
    return report


def reset_probe_cache():
    with _lock:
        _probe_cache.clear()


def cached_probe(model):
    """Return the cached capability report without any network access."""
    identity = cache_identity(model)
    key = '|'.join((identity.provider, identity.api_base, identity.model, identity.template_sha256))
    with _lock:
        cached = _probe_cache.get(key)
    return cached['report'] if cached else None


# --------------------------------------------------------------------------
# Relay-facing helpers
# --------------------------------------------------------------------------

def read_evidence(path):
    """Bounded read of the capability evidence file; missing/corrupt is empty."""
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def measure_for_relay(model, body, task_state=None, settings=None, evidence=None,
                      metrics=None, props=None, declared_window=None, engine_tokenize=None,
                      output_target=None, llm_ceiling=None):
    """Build the meter record for one gateway request.

    Failures of metering itself are recorded as warnings on the task and are
    never allowed to surface as a model-call failure.
    """
    record = measure(model, body, output_target=output_target,
                     task_target=_task_output_target(task_state),
                     props=props, evidence=evidence, declared_window=declared_window,
                     engine_tokenize=engine_tokenize, llm_ceiling=llm_ceiling)
    record['metrics'] = _metrics_snapshot(metrics)
    record['calibration'] = None
    return record


def _task_output_target(task_state):
    if not isinstance(task_state, dict):
        return None
    value = task_state.get('request_output_tokens')
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _metrics_snapshot(metrics):
    """Small, bounded engine metrics extract used for cross-checking usage."""
    if not isinstance(metrics, dict):
        return None
    engine = metrics.get('engine') if isinstance(metrics.get('engine'), dict) else {}
    live = metrics.get('live') if isinstance(metrics.get('live'), dict) else {}
    return {'max_context': engine.get('max_context'), 'state': live.get('state'),
            'prompt_tokens': live.get('prompt_tokens'), 'generated': live.get('generated')}


def read_engine_metrics(model, timeout=PROBE_TIMEOUT):
    """Bounded, read-only engine metrics read; failures are metering limitations."""
    model = model or {}
    base = urlparse(str(model.get('api_base') or ''))
    if base.scheme not in {'http', 'https'} or not base.netloc:
        return None
    origin = f'{base.scheme}://{base.netloc}'
    headers = {'Authorization': 'Bearer ' + model['api_key']} if model.get('api_key') else {}
    try:
        return _http_get(origin, '/metrics', headers, bool(model.get('bypass_proxy')), timeout)
    except Exception:
        return None


def read_engine_metrics_cached(model, ttl=METRICS_TTL, clock=time.monotonic):
    """Read engine metrics at most once per TTL per deployment identity.

    The request path must not re-read a large props/metrics payload for every
    request, and a slow or unreachable engine must degrade to "no metrics"
    rather than surfacing as a model-call failure.
    """
    identity = cache_identity(model)
    key = '|'.join((identity.provider, identity.api_base, identity.model))
    now = clock()
    with _lock:
        cached = _metrics_cache.get(key)
    if cached and now - cached['at'] < ttl:
        return cached['value']
    value = read_engine_metrics(model)
    with _lock:
        _metrics_cache[key] = {'at': now, 'value': value}
    return value


def reset_metrics_cache():
    with _lock:
        _metrics_cache.clear()


def record_summary(record):
    """Compact version of a meter record for the per-request task ledger.

    The full record stays available in-process; only what an operator needs to
    link request volume with a count source/trust level and a usage observation
    is persisted, so per-request task writes stay bounded.
    """
    if not isinstance(record, dict):
        return {}
    input_measure = dict(record.get('input') or {})
    classes = input_measure.pop('estimate_classes', None)
    if isinstance(classes, dict):
        input_measure['estimate_class_bytes'] = {
            name: classes.get(name) for name in ('ascii_bytes', 'cjk_bytes', 'other_bytes')}
    window = record.get('window') or {}
    return {
        'schema': record.get('schema', SCHEMA),
        'model_id': record.get('model_id'),
        'model': record.get('model'),
        'provider': record.get('provider'),
        'identity': record.get('identity'),
        'input': input_measure,
        'output': record.get('output'),
        'sources': record.get('sources'),
        'window': {'kind': window.get('kind'), 'input_window': window.get('input_window'),
                   'output_cap': window.get('output_cap'), 'source': window.get('source'),
                   'semantics_verified': window.get('semantics_verified'),
                   'enforceable': window.get('enforceable'),
                   'fit': {key: value for key, value in (window.get('fit') or {}).items()}},
        'guard': record.get('guard'),
        'warnings': list(record.get('warnings') or [])[:8],
        'metrics': record.get('metrics'),
    }


def meter_fields(record):
    """Compact task-record fields so request volume and count provenance link up."""
    if not isinstance(record, dict):
        return {}
    input_measure = record.get('input') or {}
    output = record.get('output') or {}
    window = record.get('window') or {}
    fit = window.get('fit') or {}
    return {
        'meter_schema': SCHEMA,
        'meter_source': record.get('sources', {}).get('used'),
        'meter_precision': record.get('sources', {}).get('precision'),
        'meter_input_tokens': input_measure.get('tokens'),
        'meter_input_origin': input_measure.get('method'),
        'meter_output_target': output.get('target'),
        'meter_output_origin': output.get('origin'),
        'meter_window_kind': window.get('kind'),
        'meter_window_verified': window.get('semantics_verified'),
        'meter_fit_state': fit.get('state'),
        'meter_guard': record.get('guard'),
        'meter_identity': record.get('identity'),
        'meter_warnings': record.get('warnings') or [],
        'meter_record': record_summary(record),
    }
