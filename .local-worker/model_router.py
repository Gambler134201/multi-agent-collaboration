"""Explainable, deterministic routing; presets are declarations, not benchmarks."""
from datetime import datetime
from decimal import Decimal
import math

import deepseek_billing as billing
import failure_classification as failure
import model_scope
import preset_runtime

LEVELS = {'simple': 0, 'medium': 1, 'complex': 2}
# A failed connection check only blocks automatic routing while it is *fresh* and
# was produced by a real generation test.  An aged failure must never permanently
# exclude a capable local model: the dispatch path rechecks reachability with a
# bounded read-only probe before it really sends anything.
CONNECTIVITY_BLOCK_SECONDS = 900
CATEGORIES = {
    'code_small': ('简单代码', '小型代码', '单文件', '小范围', '简单修改'),
    'code_multi': ('多文件', '大型代码', '较大型', '重构', '架构'),
    'debug': ('排查', '疑难', '调试', '复杂逻辑'),
    'writing': ('文案', '文档', '写作'),
    'extract': ('提取', '整理', '重复', '信息'),
    'analysis': ('分析', '长上下文', '逻辑', '整理'),
    'tests': ('测试', '验收'),
    'other': (),
}


def is_paid(model):
    return model.get('provider') != 'local'


def _moment(value, moment=None):
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
        reference = moment or datetime.now().astimezone()
        parsed = parsed.replace(tzinfo=reference.tzinfo)
    return parsed


def connectivity_verdict(model, moment=None):
    """Read-only verdict on the stored connection check for one model.

    Pure and deterministic: it never performs network I/O and never writes a
    record, so the route preview stays free of side effects.  A read-only
    reachability check is never treated as a capability verdict, and an aged
    generation failure stops excluding the model and becomes a warning that the
    dispatch path will recheck.
    """
    check = model.get('last_check')
    if not isinstance(check, dict):
        return {'blocked': False, 'stale': False, 'kind': None, 'age_seconds': None,
                'recoverable': False, 'reason': ''}
    kind = str(check.get('kind') or 'generation')
    if check.get('ok') is not False:
        return {'blocked': False, 'stale': False, 'kind': kind, 'age_seconds': None,
                'recoverable': False, 'reason': ''}
    at = _moment(check.get('at'), moment)
    reference = moment or datetime.now().astimezone()
    age = None
    if at is not None:
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=at.tzinfo)
        age = (reference - at).total_seconds()
    if kind == 'reachability':
        return {'blocked': False, 'stale': True, 'kind': kind, 'age_seconds': age, 'recoverable': True,
                'reason': '上次只读可达性检查未通过；派发前会重新做一次只读可达性检查，这不是模型能力结论'}
    if age is None:
        # A legacy failure with no timestamp cannot be proven old, so the
        # historical conservative block stays (every new check writes ``at``).
        return {'blocked': True, 'stale': False, 'kind': kind, 'age_seconds': None, 'recoverable': True,
                'reason': '连接测试失败且没有时间戳；请在面板重新测试连接后重试'
                          '（这是可恢复的检查，不是能力结论）'}
    if age > CONNECTIVITY_BLOCK_SECONDS:
        return {'blocked': False, 'stale': True, 'kind': kind, 'age_seconds': age, 'recoverable': True,
                'reason': '上次连接测试早已失败（约 %d 秒前）；不再据此排除模型，派发前会重新做只读可达性检查'
                          % int(age)}
    return {'blocked': True, 'stale': False, 'kind': kind, 'age_seconds': age, 'recoverable': True,
            'reason': '刚刚的连接测试失败；可在面板重新测试连接后重试（这是可恢复的检查，不是能力结论）'}


def rates_for(model, moment=None, ceiling=False):
    if not is_paid(model):
        return {'cache_hit_input': '0', 'cache_miss_input': '0', 'output': '0', 'label': '自建模型，无 API 账单'}
    if billing.is_official(model):
        price = billing.pricing(model, moment)
        rates = price['rates']
        if not rates:
            return None
        if ceiling:
            canonical = billing.ALIASES.get(model.get('model'), model.get('model'))
            values = billing.RATES[canonical]['peak']
            rates = dict(zip(('cache_hit_input', 'cache_miss_input', 'output'), values))
        return {**rates, 'label': price['label'] if not ceiling else '按峰价保守预留'}
    rates = model.get('pricing_cny')
    return {**rates, 'label': '用户填写单价'} if rates else None


def cost(rates, input_tokens, output_tokens, cache_fraction=0):
    hit = Decimal(str(cache_fraction))
    return (Decimal(input_tokens) * ((1 - hit) * Decimal(rates['cache_miss_input']) + hit * Decimal(rates['cache_hit_input']))
            + Decimal(output_tokens) * Decimal(rates['output'])) / Decimal(1000000)


def _nonneg_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def normalize_usage(usage):
    """Validate a provider usage report without inventing missing numbers.

    Only a report carrying both prompt and completion tokens can settle cost.
    Billing-critical fields that are present but invalid, and cache/reasoning
    fields that contradict the token totals, make the report unusable: the
    request stays "费用待核对" instead of being charged on invented numbers.
    Missing (not contradictory) cache fields conservatively bill the whole
    prompt at the miss rate. ``completion_tokens`` already includes reasoning
    output for DeepSeek, so a larger separate reasoning figure is a
    contradiction rather than something to add on top.
    """
    if not isinstance(usage, dict):
        return {'valid': False, 'usage': {}, 'issues': ['缺少 usage 字段'], 'note': '未返回用量，费用待核对'}
    values, issues, invalid = {}, [], []
    for name in ('prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'):
        if name in usage and usage[name] is not None:
            if _nonneg_int(usage[name]):
                values[name] = usage[name]
            else:
                invalid.append(name + ' 字段无效')
    total = usage.get('total_tokens')
    if total is not None and not _nonneg_int(total):
        issues.append('total_tokens 字段无效，已忽略')
        total = None
    if invalid:
        return {'valid': False, 'usage': values, 'issues': issues + invalid, 'note': '用量字段无效，费用待核对'}
    prompt = values.get('prompt_tokens')
    completion = values.get('completion_tokens')
    if prompt is None or completion is None:
        issues.append('缺少输入用量' if prompt is None else '缺少输出用量')
        return {'valid': False, 'usage': values, 'issues': issues, 'note': '用量不完整，费用待核对'}
    details = usage.get('completion_tokens_details')
    if details is not None and not isinstance(details, dict):
        return {'valid': False, 'usage': values, 'issues': issues + ['completion_tokens_details 字段无效'],
                'note': '推理输出字段无效，费用待核对'}
    reasoning = details.get('reasoning_tokens') if isinstance(details, dict) else None
    if reasoning is not None:
        if not _nonneg_int(reasoning):
            return {'valid': False, 'usage': values, 'issues': issues + ['reasoning_tokens 字段无效'],
                    'note': '推理输出字段无效，费用待核对'}
        if reasoning > completion:
            return {'valid': False, 'usage': values, 'issues': issues + ['推理输出大于总输出，字段矛盾'],
                    'note': '用量字段矛盾，费用待核对'}
    if prompt == 0 and completion == 0:
        # A real relayed completion always consumed prompt tokens; an all-zero report is an anomaly.
        return {'valid': False, 'usage': values, 'issues': issues + ['用量全零，视为未返回有效用量'],
                'note': '用量全零，费用待核对'}
    hit = values.get('prompt_cache_hit_tokens')
    miss = values.get('prompt_cache_miss_tokens')
    cache_details = usage.get('prompt_tokens_details')
    if hit is None and isinstance(cache_details, dict) and 'cached_tokens' in cache_details:
        hit = cache_details['cached_tokens']
        if not _nonneg_int(hit):
            return {'valid': False, 'usage': values, 'issues': issues + ['缓存命中字段无效'], 'note': '缓存用量无效，费用待核对'}
    cache_complete = hit is not None or miss is not None
    if hit is not None and hit > prompt:
        return {'valid': False, 'usage': values, 'issues': issues + ['缓存命中大于输入用量，字段矛盾'],
                'note': '缓存字段矛盾，费用待核对'}
    if miss is not None and miss > prompt:
        return {'valid': False, 'usage': values, 'issues': issues + ['缓存未命中大于输入用量，字段矛盾'],
                'note': '缓存字段矛盾，费用待核对'}
    if hit is not None and miss is not None:
        if hit + miss != prompt:
            return {'valid': False, 'usage': values, 'issues': issues + ['缓存命中与未命中之和与输入用量不一致'],
                    'note': '缓存字段矛盾，费用待核对'}
    elif hit is None and miss is None:
        issues.append('缺少缓存命中字段，输入按未命中计价')
        hit, miss = 0, prompt
    elif hit is None:
        hit = prompt - miss
    else:
        miss = prompt - hit
    if total is not None and total != prompt + completion:
        issues.append('总用量与输入/输出明细不一致，按明细结算')
    normalized = {'prompt_tokens': prompt, 'completion_tokens': completion,
                  'total_tokens': total if total is not None else prompt + completion,
                  'prompt_cache_hit_tokens': hit, 'prompt_cache_miss_tokens': miss}
    return {'valid': True, 'usage': normalized, 'cache_complete': cache_complete,
            'issues': issues, 'note': '；'.join(issues)}


def usage_cost(rates, normalized):
    """Cost a validated usage report with the rates snapshotted at request time."""
    usage = normalized.get('usage') if isinstance(normalized, dict) and 'usage' in normalized else normalized
    hit = Decimal(usage.get('prompt_cache_hit_tokens', 0))
    miss = Decimal(usage.get('prompt_cache_miss_tokens', 0))
    output = Decimal(usage.get('completion_tokens', 0))
    return (hit * Decimal(str(rates['cache_hit_input'])) + miss * Decimal(str(rates['cache_miss_input']))
            + output * Decimal(str(rates['output']))) / Decimal(1000000)


def requirements(body):
    raw = body.get('routing', {})
    if not isinstance(raw, dict):
        raise ValueError('路由参数应为对象')
    category = raw.get('category', 'code_small' if body.get('kind') == 'edit' and len(body.get('files', [])) <= 2 else 'code_multi' if body.get('kind') == 'edit' else 'analysis')
    complexity = raw.get('complexity', 'medium' if category in {'code_multi', 'debug'} else 'simple')
    if category not in CATEGORIES or complexity not in LEVELS:
        raise ValueError('请选择有效的任务类别和复杂度')
    result = {'category': category, 'complexity': complexity}
    prompt_bytes = len(str(body.get('prompt', '')).encode('utf-8'))
    defaults = {'input_tokens': max(4096, prompt_bytes + 2048), 'output_tokens': 4096,
                'expected_calls': 3 if body.get('kind') == 'edit' else 1}
    upper = {'input_tokens': 1048576, 'output_tokens': 131072, 'expected_calls': 100000}
    declared = {}
    for field in ('input_tokens', 'output_tokens', 'expected_calls'):
        value = raw.get(field, defaults[field])
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper[field]:
            raise ValueError('路由输入、输出和调用次数估计必须是有效整数')
        result[field] = value
        declared[field] = field in raw
    # These legacy fields were always caller estimates, not engine counts.
    # Only an explicit min_* requirement is a strict capacity declaration.
    estimates = {}
    for field, legacy in (('estimated_input_tokens', 'input_tokens'),
                          ('estimated_output_tokens', 'output_tokens')):
        value = raw.get(field)
        if value is None:
            estimates[legacy] = result[legacy] if declared[legacy] else defaults[legacy]
        else:
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper[legacy]:
                raise ValueError('近似 token 估计必须是有效整数')
            estimates[legacy] = value
    minimum = {}
    for field, legacy in (('min_input_tokens', 'input_tokens'), ('min_output_tokens', 'output_tokens')):
        value = raw.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper[legacy]:
            raise ValueError('最低容量要求必须是有效整数')
        minimum[legacy] = value
    result['capacity'] = {
        'declared': {field: (result[field] if declared[field] else None) for field in ('input_tokens', 'output_tokens')},
        'estimates': estimates,
        'minimum': minimum,
        'declared_is_strict': False,
        'estimate_semantics': 'approximate',
        'notes': ['input_tokens/output_tokens 与 estimated_* 都是近似估计；只有明确 min_* 最低容量是门禁'],
    }
    result['estimated_input_tokens'] = estimates['input_tokens']
    result['estimated_output_tokens'] = estimates['output_tokens']
    # Echo an explicitly declared minimum at the top level only, so a rework that
    # reuses these requirements keeps the strict minimum without inventing one.
    for field, legacy in (('min_input_tokens', 'input_tokens'), ('min_output_tokens', 'output_tokens')):
        if legacy in minimum:
            result[field] = minimum[legacy]
    cache = raw.get('cache_fraction', 0)
    if isinstance(cache, bool) or not isinstance(cache, (int, float)) or not math.isfinite(cache) or not 0 <= cache <= 1:
        raise ValueError('缓存命中比例估计应在 0 到 1 之间')
    result['cache_fraction'] = cache
    for field in ('eligible_model_ids', 'failed_model_ids'):
        value = raw.get(field)
        if value is not None and (not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) for v in value)):
            raise ValueError('候选模型和失败模型列表格式不正确')
        result[field] = value
    result['assessment'] = str(raw.get('assessment', ''))[:1500]
    acceptance = raw.get('acceptance', '')
    if isinstance(acceptance, (list, tuple)):
        # Keep the canonical array shape: each element is one condition and must
        # never be flattened into a single str(list) requirement.
        result['acceptance'] = [str(item).strip()[:500] for item in list(acceptance)[:20] if str(item).strip()]
    else:
        result['acceptance'] = str(acceptance or '')[:1500]
    return result


def balance_limit(model):
    if not billing.is_official(model):
        return None
    state = model.get('billing', {}).get('balance', {})
    if state.get('status') != 'ready' or state.get('stale') or state.get('is_available') is not True:
        raise ValueError('DeepSeek 余额未确认或不可用，请刷新余额')
    values = [Decimal(str(r['total_balance'])) for r in state.get('balance_infos', []) if r.get('currency') == 'CNY']
    if not values:
        raise ValueError('缺少人民币余额，不能按人民币预算自动派发')
    return max(Decimal(0), max(values))


def _wilson_interval(successes, total, z=1.96):
    """95% Wilson score interval, honest for the small samples seen here."""
    if total <= 0:
        return 0.0, 1.0
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (z / denominator) * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _strict_bound(declared, minimum):
    """Largest explicit capacity bound (declaration or minimum), or None."""
    values = [v for v in (declared, minimum) if isinstance(v, int) and not isinstance(v, bool)]
    return max(values) if values else None


def _risk_confidence(verified_samples):
    if not verified_samples:
        return 'none'
    if verified_samples < 5:
        return 'low'
    if verified_samples < 20:
        return 'medium'
    return 'high'


def rework_risk(history, model_id, category, report=None):
    """Explainable rework risk for one model/category from real review evidence.

    Rework families (a task plus its corrective children) are counted once, so a
    single returned deliverable cannot inflate the rate.  Cancelled, system and
    unreviewed records are excluded from the verified rate, a handful of system
    failures never becomes a price multiplier, and a tiny sample is reported as
    low confidence instead of a mature calibration.  With no verified sample the
    point estimate stays neutral when only unclassified history exists; an
    uncalibrated range is labelled separately and never claims model quality.
    """
    report = report if isinstance(report, dict) else failure.classify_history(history)
    families = {}
    for finding in report['findings']:
        if model_id is not None and finding.get('model_id') != model_id:
            continue
        if category and finding.get('category') != category:
            continue
        families.setdefault(finding['family'], []).append(finding)

    verified = quality = system = cancelled = unverified = unverified_failed = 0
    samples = []
    for key, members in sorted(families.items()):
        outcome = failure.family_outcome(members)
        independent = any(m['source'] == 'independent_review'
                          and m['outcome'] in (failure.OUTCOME_QUALITY, failure.OUTCOME_SUCCESS)
                          for m in members)
        if independent and outcome in (failure.OUTCOME_QUALITY, failure.OUTCOME_SUCCESS):
            verified += 1
            if outcome == failure.OUTCOME_QUALITY:
                quality += 1
            samples.append({'family': key, 'outcome': outcome, 'task_id': members[0]['task_id'],
                            'source': 'independent_review', 'reason': members[0]['reason']})
        elif outcome == failure.OUTCOME_CANCELLED:
            cancelled += 1
        elif outcome == failure.OUTCOME_SYSTEM:
            system += 1
        else:
            unverified += 1
            if any(m['outcome'] != failure.OUTCOME_SUCCESS for m in members):
                unverified_failed += 1

    exclusions = {'system_families': system, 'cancelled_families': cancelled, 'unreviewed_families': unverified}
    notes = ['按返工家族去重；取消、纯系统失败与未验收样本不计入质量返工率']
    if verified:
        rate = (quality + 1) / (verified + 2)
        low, high = _wilson_interval(quality, verified)
        retry, retry_low, retry_high = 1 + rate, 1 + low, 1 + high
        basis, method = 'verified_independent_review', 'wilson_95'
        if verified < 5:
            notes.append('独立验收样本仅 %d 个，不足以声称校准成熟' % verified)
    elif unverified:
        retry, retry_low, retry_high = 1.0, 1.0, 2.0
        basis, method = 'unverified_legacy_default', 'uncalibrated_range'
        notes.append('没有独立验收样本，不用未知失败放大价格；1–2倍仅为未校准范围，不是统计置信区间')
    else:
        retry = retry_low = retry_high = 1.0
        basis, method = 'no_samples', 'none'
        notes.append('同类历史无可用样本，按无返工风险的诚实默认处理')

    return {'schema': failure.SCHEMA,
            'model_id': model_id,
            'category': category,
            'families': len(families),
            'verified_samples': verified,
            'quality_rework_families': quality,
            'quality_rework_rate': (quality / verified) if verified else None,
            'confidence': _risk_confidence(verified),
            'basis': basis,
            'retry_factor': round(retry, 4),
            'retry_factor_low': round(retry_low, 4),
            'retry_factor_high': round(retry_high, 4),
            'interval': {'low': round(retry_low, 4), 'high': round(retry_high, 4), 'method': method},
            'excluded': exclusions,
            'samples': samples[:5],
            'notes': notes}


def cost_observations(history, model_id, category):
    """Actual recorded spend next to the prediction (unknown cost stays separate)."""
    settled = Decimal(0)
    unknown_calls = calls = settled_samples = 0
    for record in history or ():
        if not isinstance(record, dict) or record.get('model_id') != model_id:
            continue
        requirements = (record.get('route') or {}).get('requirements') or {}
        if category and requirements.get('category') != category:
            continue
        amount = record.get('cost_settled_cny')
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            settled += Decimal(str(amount))
            settled_samples += 1
        unknown = record.get('cost_unknown_calls')
        if isinstance(unknown, int) and not isinstance(unknown, bool):
            unknown_calls += unknown
        recorded_calls = record.get('model_calls')
        if isinstance(recorded_calls, int) and not isinstance(recorded_calls, bool):
            calls += recorded_calls
    return {'settled_cost_cny': float(settled), 'unknown_cost_calls': unknown_calls,
            'observed_calls': calls, 'settled_samples': settled_samples,
            'source': 'task history cost_settled_cny / cost_unknown_calls（实际账本，未知单独列出）'}


def pricing_info(model):
    """Where a price comes from, and whether there is a trustworthy one at all."""
    if not is_paid(model):
        return {'known': True, 'source': 'self_hosted', 'note': '自建模型，无 API 账单'}
    if billing.is_official(model):
        return {'known': rates_for(model) is not None, 'source': 'official_schedule',
                'note': '官方峰谷价目表，按请求时快照结算'}
    return {'known': bool(model.get('pricing_cny')), 'source': 'user_pricing_cny',
            'note': '用户填写人民币单价'}


def _global_agent(settings):
    """Describe the fallback without turning an unused broken value into a crash."""
    import execution_engines
    try:
        return execution_engines.selected(settings)
    except ValueError:
        return 'invalid'


def choose(body, models, settings, history=(), daily_remaining=None, moment=None, daily_uncertain=False):
    import execution_engines
    import execution_endpoints
    endpoint_controls = execution_endpoints.snapshot()
    req = requirements(body)
    mode = body.get('route_mode', 'manual')
    if mode not in {'auto', 'manual'}:
        raise ValueError('路由方式不正确')
    # Global execution-model scope.  It is an authoritative filter for every
    # path that reaches this function (automatic routing, explicit manual model,
    # the manual default model and the preview endpoint).  An illegal stored
    # value is refused instead of being silently widened.
    raw_scope = settings.get('model_scope', model_scope.DEFAULT_SCOPE)
    try:
        scope = model_scope.normalize(raw_scope)
    except ValueError as error:
        raise ValueError('执行模型范围设置无效，已拒绝路由：' + str(error)) from None
    # Do not resolve the global agent here: automatic routing must be able to
    # choose a model whose saved preference is the other agent.  The final
    # candidate resolves its own agent below, and every agent-dependent gate
    # (availability, paid execution) uses that resolved value.
    requested = execution_engines.requested_engine(body)
    if mode == 'auto' and (not settings.get('enabled') or not settings.get('readable', True)):
        raise ValueError('协作总开关已关闭，请开启后使用自动路由')
    if mode == 'auto' and requested is not None and requested not in execution_engines.AGENTS:
        raise ValueError('自动协作请使用 DSH 或 Pi 执行器')
    if settings.get('enforce_model_call_limit', True) and req['expected_calls'] > settings['max_model_calls']:
        raise ValueError('预计请求次数超过任务上限，请拆分任务或调整限制')
    capacity = req['capacity']
    strict_input = _strict_bound(None, capacity['minimum'].get('input_tokens'))
    strict_output = _strict_bound(None, capacity['minimum'].get('output_tokens'))
    est_input = capacity['estimates']['input_tokens']
    est_output = capacity['estimates']['output_tokens']
    request_target = body.get('request_output_tokens')
    if request_target is not None and (isinstance(request_target, bool) or not isinstance(request_target, int)
                                       or not 1 <= request_target <= 131072):
        raise ValueError('单请求输出额度需要是 1 到 131072 的整数')
    classification = failure.classify_history(history)
    candidates, excluded = [], []
    for model in models:
        if mode == 'manual' and model['id'] != body.get('model_id'):
            continue
        try:
            if not model_scope.matches(scope, is_paid(model)):
                # Explicitly refused, never replaced by a model of the other
                # class; the reason is surfaced verbatim when no candidate is
                # left inside the scope.
                raise ValueError(model_scope.reason(scope, is_paid(model)))
            if not model.get('enabled', True):
                raise ValueError('模型已停用')
            agent, agent_source = execution_engines.resolution(body, settings, model=model)
            execution_endpoints.gate(agent, model, settings, controls=endpoint_controls, automatic=mode == 'auto')
            model = execution_endpoints.apply_preset(model, agent, endpoint_controls)
            if agent_source == 'model' and agent in execution_engines.AGENTS:
                # A saved model preference is authoritative for this candidate:
                # an unavailable adapter refuses the route instead of quietly
                # falling back to the global engine.  Explicit/global engines are
                # checked by the dispatch entry point, which also owns task type
                # and project validation.
                availability = execution_engines.availability(agent)
                if not availability.get('ready'):
                    detail = str(availability.get('message') or '').strip()
                    raise ValueError(execution_engines.LABELS[agent]
                                     + ' 执行器未就绪；不会切换到其他执行器'
                                     + (('：' + detail) if detail else ''))
            elif mode == 'auto' and agent not in execution_engines.AGENTS:
                raise ValueError('自动协作请使用 DSH 或 Pi 执行器')
            connectivity = None
            if mode == 'auto':
                connectivity = connectivity_verdict(model)
                if connectivity['blocked']:
                    raise ValueError(connectivity['reason'])
                if req['eligible_model_ids'] is not None and model['id'] not in req['eligible_model_ids']:
                    raise ValueError('主脑依据能力说明排除')
                if model['id'] in (req['failed_model_ids'] or []):
                    raise ValueError('本项任务已经在此模型失败，需要重新评估')
                limit = model.get('max_task_complexity', 'simple' if not is_paid(model) else 'complex')
                if LEVELS[req['complexity']] > LEVELS[limit]:
                    raise ValueError('超过用户声明的任务复杂度上限')
            # Strict gates use explicit minimum requirements only. Both legacy
            # token fields and estimated_* are estimates and only warn.
            model_input = model.get('max_input_tokens', 32768)
            model_output = model.get('max_output_tokens', 4096)
            if strict_input is not None and strict_input > model_input:
                raise ValueError('声明的输入容量超过模型上下文窗口')
            if strict_output is not None and strict_output > model_output:
                raise ValueError('声明的单次输出超过模型最大输出能力')
            if (strict_input or 0) + (strict_output or 0) > model_input:
                raise ValueError('声明或最低容量超过模型上下文窗口')
            warnings = []
            if connectivity is not None and connectivity.get('recoverable') and connectivity.get('reason'):
                warnings.append(connectivity['reason'])
            if est_input + est_output > model_input:
                warnings.append('近似 token 估计 %d + %d 超过模型窗口 %d：仅作提示，请缩小范围或换更大窗口的模型'
                                % (est_input, est_output, model_input))
            if est_output > model_output:
                warnings.append('近似单次输出估计 %d 超过模型最大输出 %d：具体单次请求以模型上限为准'
                                % (est_output, model_output))
            rates = rates_for(model, moment)
            cap = None
            native = model.get('provider') == 'workbuddy'
            if native:
                import workbuddy_worker
                workbuddy_worker.validate_task(body, settings)
                if mode == 'auto' and req['eligible_model_ids'] is None:
                    raise ValueError('WorkBuddy 原生积分费用未知；自动路由需主脑明确列入 eligible_model_ids，或手动选择')
                warnings.append('WorkBuddy 积分和实际底层请求数未返回；成本未知，不按免费或最低价排序')
            if is_paid(model) and not native:
                if not settings.get('paid_allowed'):
                    raise ValueError('付费执行未开启')
                if agent not in execution_engines.AGENTS:
                    raise ValueError('付费任务请使用 DSH 或 Pi，以执行逐次请求预算检查')
                if not rates:
                    raise ValueError('缺少可核对的单价，请在模型中填写人民币单价')
                if not model.get('api_key_set', False):
                    raise ValueError('未配置 API Key')
                balance = balance_limit(model)
                if balance is not None and balance <= 0:
                    raise ValueError('余额不足，请充值后重新派发')
                if settings.get('enforce_paid_budget', True):
                    if daily_uncertain:
                        raise ValueError('今日存在未结算或待核对费用，请核对后再启用金额上限')
                    cap = min(Decimal(str(settings['task_budget_cny'])), Decimal(str(daily_remaining if daily_remaining is not None else settings['daily_budget_cny'])))
                    if balance is not None:
                        cap = min(cap, balance)
                    if cap <= 0:
                        raise ValueError('付费任务预算、今日剩余额度或余额不足')
            risk = rework_risk(history, model['id'], req['category'], report=classification)
            retry_factor = risk['retry_factor']
            base_cost = cost(rates, est_input, est_output, req['cache_fraction']) if rates else Decimal(0)
            estimate = base_cost * Decimal(str(req['expected_calls'] * retry_factor))
            prediction = {'kind': 'prediction', 'estimated_cost_cny': float(estimate), 'base_cost_cny': float(base_cost),
                          'expected_calls': req['expected_calls'], 'retry_factor': retry_factor,
                          'retry_factor_interval': risk['interval'],
                          'interval_low_cny': float(base_cost * Decimal(str(req['expected_calls'] * risk['retry_factor_low']))),
                          'interval_high_cny': float(base_cost * Decimal(str(req['expected_calls'] * risk['retry_factor_high']))),
                          'interval_basis': risk['interval']['method'], 'risk_basis': risk['basis'],
                          'input_source': {'declared': capacity['declared'], 'estimates': capacity['estimates'],
                                           'minimum': capacity['minimum'], 'cache_fraction': req['cache_fraction']},
                          'source': '请求时单价快照 + 预计输入/输出/次数；这是预测，不是实际账单'}
            if native:
                prediction = {'kind': 'unknown', 'estimated_cost_cny': None, 'base_cost_cny': None,
                              'source': 'WorkBuddy 原生积分；未取得可核对金额，不能等同于零费用'}
            # Estimates rank models and inform the user; usage-based caps are
            # checked at the gateway. A worst-case forecast is not spending.
            matches = [tag for tag in model.get('preferred_tasks', []) if any(term in tag for term in CATEGORIES[req['category']])]
            if mode == 'auto' and model.get('preferred_tasks') and not matches and req['eligible_model_ids'] is None:
                raise ValueError('任务类别未匹配能力标签；需要主脑依据说明提供候选模型')
            reason = ('手动指定；' if mode == 'manual' else '满足复杂度、上下文与输出要求；')
            reason += ('执行 Agent：' + execution_engines.LABELS.get(agent, agent)
                       + '（' + {'explicit': '本次显式指定', 'model': '模型默认'}.get(agent_source, '全局默认') + '）；')
            if scope != model_scope.ALL:
                reason += '执行模型范围：' + model_scope.LABELS[scope] + '；'
            reason += ('匹配标签：' + '、'.join(matches) + '；') if matches else '主脑候选或未设置标签；'
            reason += ('WorkBuddy 登录账户原生积分，费用未知' if native else
                       f"{rates['label'] if rates else '无单价'}，预计总成本 ¥{estimate:.4f}")
            if warnings:
                reason += '；' + warnings[0]
            if risk['verified_samples']:
                reason += (f"，独立验收样本 {risk['verified_samples']} 个、质量返工家族 {risk['quality_rework_families']} 个"
                           f"（置信 {risk['confidence']}）")
            elif risk['basis'] == 'unverified_legacy_default':
                reason += "，无独立验收样本；未知失败不用于放大价格"
            candidates.append({'model': model, 'estimated_cost_cny': None if native else float(estimate), 'budget_cny': float(cap) if cap is not None else 0,
                               'reason': reason, 'retry_factor': retry_factor, 'matched_tags': matches,
                               'warnings': warnings, 'risk': risk, 'prediction': prediction,
                               'pricing': pricing_info(model), 'engine': agent,
                               'agent_source': agent_source})
        except ValueError as error:
            excluded.append({'model_id': model['id'], 'model_name': model['name'], 'reason': str(error)})
    if not candidates:
        message = '；'.join(f"{row['model_name']}：{row['reason']}" for row in excluded) or '没有可用模型'
        raise ValueError('需要主脑重新拆分任务或人工调整：' + message)
    candidates.sort(key=lambda row: (row['estimated_cost_cny'] is None, row['estimated_cost_cny'] or 0, is_paid(row['model']), {'economy': 0, 'balanced': 1, 'quality': 2}.get(row['model'].get('cost_preference'), 1), -len(row['matched_tags'])))
    selected = candidates[0]
    model = selected['model']
    observed = cost_observations(history, model['id'], req['category'])
    model_ceiling = model.get('max_output_tokens', 4096)
    goal = request_target if request_target is not None else est_output
    return {'mode': mode, 'model_id': model['id'], 'reason': selected['reason'], 'requirements': req,
            'engine': selected['engine'], 'agent_source': selected['agent_source'],
            'agent_label': execution_engines.LABELS.get(selected['engine'], selected['engine']),
            'model_default_agent': model.get('default_agent', 'follow_global'),
            'global_agent': _global_agent(settings),
            'agent_preset': preset_runtime.selected_id(model),
            'routing_preset': model.get('routing_preset'),
            'estimated_cost_cny': selected['estimated_cost_cny'], 'budget_cny': selected['budget_cny'],
            'budget_enforced': settings.get('enforce_paid_budget', True), 'excluded': excluded,
            'rates': rates_for(model, moment), 'pricing_snapshot': datetime.now().astimezone().isoformat(),
            'model_scope': {**model_scope.describe(scope),
                            'candidate': model_scope.candidate_kind(is_paid(model)),
                            'candidate_paid': bool(is_paid(model)),
                            'selected_note': '本次已选中范围内的候选；未替换模型，也未回退到另一类'},
            'warnings': selected['warnings'],
            'failure_risk': selected['risk'],
            'single_request_output': {'task_target': request_target, 'model_ceiling': model_ceiling,
                                      'effective_target': min(goal, model_ceiling),
                                      'note': '模型最大输出是能力上限；任务单请求输出目标只约束本次请求，二者不同'},
            'cost': {'prediction': selected['prediction'], 'observed': observed, 'pricing': selected['pricing']}}
