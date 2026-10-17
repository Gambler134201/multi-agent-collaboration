"""Per-job loopback relay: bound calls and settle usage-based API cost.

Accounting model (ledger schema 2): a request reserves a conservative worst-case
amount *before* it is sent, then settles with the usage the provider reports and
the unit price that applied when the request was sent. ``reserved_cny`` /
``budget_reserved_cny`` stay as raw audit fields; they are not settled spending.
Reservations are audit estimates, never money-cap usage. Enabled caps stop
subsequent requests when settled cost reaches the cap, or while a *current*
billable request remains unresolved. Pre-v2 reservations are kept in history for
minimal compatibility (``legacy_reserved_cny`` / ``cost_legacy_unsettled_cny``)
but never enter current statistics, ``remaining``/``unresolved`` judgment,
routing, or the request gate. A single request may cross the cap. Ledger
receipts make settlement recoverable across a failed task-file write without
adding the daily cost twice.

Before a request is sent, ``context_manager`` offloads large ``role=tool``
results to stable per-task files and keeps a bounded preview. That changes only
the outgoing message content: bytes saved there are audit values, never token
savings. Metering and cost settlement still use the processed request and the
provider's reported usage.
"""
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import secrets
import threading
import time
from datetime import datetime
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

import context_manager
import deepseek_billing as billing
import model_registry as registry
import model_router as router
import recovery_retry
import thinking_config
import token_meter


LEDGER_SCHEMA = 2
REQUEST_RECORD_CAP = 200
EVIDENCE_PATH = registry.HERE / 'tasks' / 'token-meter-capability-evidence.json'
# Socket waiting allowance for one upstream call.  This is a transport bound,
# *not* a task deadline: ``task_timing`` alone decides an optional total runtime
# and timeout=0 stays unlimited.  A slow local engine may legitimately spend a
# long time on one generation, so the allowance is generous.
UPSTREAM_SOCKET_TIMEOUT = 300
# Protocol safety limit on the serialized request body. This is a bytes/transport
# bound (the handler also caps the raw body), deliberately separate from any
# token-window decision.
PROTOCOL_BYTE_CEILING = 6 * 1024 * 1024
OUTCOME_NOTES = {
    'ok': '接口结束但未返回有效用量，费用待核对',
    'http_error': '接口失败且未返回用量，保守预留保留待核对',
    'stream_interrupted': '流式响应中断，未取得最终用量，费用待核对',
    'unconfirmed_terminal': '接口结束但没有有效终止证据，未确认是否完成，费用待核对',
    'client_disconnected': '客户端断开，未取得最终用量，费用待核对',
    'cancelled': '任务被取消，未取得最终用量，费用待核对',
}


def ledger_path():
    return registry.HERE / 'budget-ledger.json'


def day_key(moment=None):
    moment = moment or datetime.now(billing.CHINA)
    return moment.astimezone(billing.CHINA).date().isoformat()


def _decimal(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('预算金额格式不正确') from None
    if not amount.is_finite() or amount < 0:
        raise ValueError('预算金额格式不正确')
    return amount


def _valid_token_bound(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _int_field(row, name):
    value = row.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError()
    return value


def read_ledger():
    """Read and validate the ledger; new rows keep the legacy reserved fields."""
    path = ledger_path()
    if not path.exists():
        return {'schema': LEDGER_SCHEMA, 'days': {}}
    try:
        value = registry.read_json(path)
        if not isinstance(value, dict) or not isinstance(value['days'], dict):
            raise ValueError()
        if not isinstance(value.get('settlements', {}), dict):
            raise ValueError()
        for row in value['days'].values():
            if not isinstance(row, dict):
                raise ValueError()
            for field in ('reserved_cny', 'settled_cny', 'unsettled_cny', 'legacy_reserved_cny'):
                if field in row:
                    _decimal(row[field])
            for field in ('calls', 'settled_calls', 'pending_calls', 'unknown_calls'):
                if field in row:
                    _int_field(row, field)
        return value
    except (ValueError, KeyError, TypeError, OSError):
        raise ValueError('预算账本无法读取，付费派发已停止，请检查文件') from None


def _day_row(row):
    """Return a validated v2 row; old rows keep their reserve as raw legacy audit.

    Pre-v2 ``reserved_cny`` is the accumulated worst-case reservation of the
    early build: there is no usage settlement for it. It is preserved as
    ``legacy_reserved_cny`` for minimal compatibility and history, but it stays
    out of ``unsettled_cny``, ``remaining_cny``, ``unresolved`` and the request
    gate, so an old over-estimate can never zero the remaining budget.
    """
    if not isinstance(row, dict):
        row = {}
    if 'settled_cny' not in row and 'unsettled_cny' not in row:
        reserved = _decimal(row.get('reserved_cny', 0))
        row['legacy_reserved_cny'] = str(reserved)
        row['unsettled_cny'] = '0'
        row['settled_cny'] = '0'
        row['settled_calls'] = 0
        row['pending_calls'] = 0
        row['unknown_calls'] = 0
        row['schema'] = LEDGER_SCHEMA
    else:
        row.setdefault('settled_cny', '0')
        row.setdefault('unsettled_cny', '0')
        row.setdefault('legacy_reserved_cny', '0')
    return row


def _task_migrate(state):
    """Add v2 counters to a pre-v2 task; the old reserve is kept as raw legacy."""
    if 'cost_settled_cny' in state or 'cost_unsettled_cny' in state:
        return
    legacy = _decimal(state.get('budget_reserved_cny', 0))
    state['cost_legacy_unsettled_cny'] = float(legacy)
    state['cost_unsettled_cny'] = 0.0
    state['cost_settled_cny'] = 0.0
    state['cost_settled_calls'] = 0
    state['cost_unknown_calls'] = 0
    state['cost_schema'] = 2


def task_cost_state(state):
    """Current cost counters for one task; raw legacy fields are not included."""
    if 'cost_settled_cny' in state or 'cost_unsettled_cny' in state:
        settled = _decimal(state.get('cost_settled_cny', 0))
        unsettled = _decimal(state.get('cost_unsettled_cny', 0))
    else:
        settled = None
        unsettled = Decimal(0)
    used = settled or Decimal(0)
    return {'settled_cny': settled, 'unsettled_cny': unsettled,
            'used_cny': used, 'settled_calls': int(state.get('cost_settled_calls', 0) or 0),
            'unknown_calls': int(state.get('cost_unknown_calls', 0) or 0)}


def daily_state(settings):
    try:
        with registry.file_lock(registry.HERE / 'budget.lock'):
            ledger = read_ledger()
        day = day_key()
        row = ledger['days'].get(day, {})
        reserved = _decimal(row.get('reserved_cny', 0))
        if 'settled_cny' in row or 'unsettled_cny' in row:
            settled = _decimal(row.get('settled_cny', 0))
            unsettled = _decimal(row.get('unsettled_cny', 0))
            legacy = _decimal(row.get('legacy_reserved_cny', 0))
            settled_calls = _int_field(row, 'settled_calls')
            unknown_calls = _int_field(row, 'unknown_calls')
        else:
            # Pre-v2 rows: reservations only, no settlement record. The old
            # reserve stays as a raw compatibility/audit value and is
            # deliberately excluded from remaining/unresolved and the gate.
            settled = None
            unsettled = Decimal(0)
            legacy = reserved
            settled_calls = 0
            unknown_calls = 0
        used = settled or Decimal(0)
        remaining = max(Decimal(0), Decimal(str(settings['daily_budget_cny'])) - used)
        unresolved = unsettled > 0 or unknown_calls > 0 or _int_field(row, 'pending_calls') > 0
        if unresolved and settings.get('enforce_paid_budget', True):
            remaining = Decimal(0)
        return {'day': day, 'reserved_cny': float(reserved),
                'settled_cny': float(settled) if (settled is not None and settled_calls > 0) else None,
                'settled_calls': settled_calls, 'unsettled_cny': float(unsettled),
                'legacy_reserved_cny': float(legacy), 'unknown_calls': unknown_calls,
                'calls': _int_field(row, 'calls'), 'budget_used_cny': float(used),
                'remaining_cny': float(remaining), 'has_actual': settled_calls > 0,
                'unresolved': unresolved, 'error': ''}
    except ValueError as error:
        return {'day': day_key(), 'reserved_cny': None, 'settled_cny': None, 'settled_calls': 0,
                'unsettled_cny': 0.0, 'legacy_reserved_cny': 0.0, 'unknown_calls': 0, 'calls': 0,
                'budget_used_cny': 0.0, 'remaining_cny': 0.0, 'has_actual': False, 'error': str(error)}


def _period(model, moment):
    if not router.is_paid(model):
        return 'local'
    if billing.is_official(model):
        return billing.period(moment)
    return 'third_party'


def _trim_requests(requests):
    if len(requests) <= REQUEST_RECORD_CAP:
        return requests
    removable = [key for key, row in requests.items() if not isinstance(row, dict) or row.get('state') in {'settled', 'free'}]
    for key in removable[:max(0, len(requests) - REQUEST_RECORD_CAP)]:
        requests.pop(key, None)
    return requests


def reserve(run, model, settings, input_bound, output_bound, request_id=None, moment=None):
    """Reserve the conservative worst case and open a settleable request record.

    No refunds on uncertain or failed requests: the reservation is released only
    when a valid usage report arrives and settles the request.
    """
    moment = (moment or datetime.now(billing.CHINA)).astimezone(billing.CHINA)
    request_day = day_key(moment)
    request_id = str(request_id) if request_id else secrets.token_hex(8)
    with registry.file_lock(registry.HERE / 'budget.lock'):
        state = registry.read_task(run)
        if (run / 'cancelled.flag').exists():
            raise ValueError('任务已取消')
        calls = state.get('model_calls', 0)
        if settings.get('enforce_model_call_limit', True) and calls >= settings['max_model_calls']:
            raise ValueError('已达到本任务模型请求次数上限；需要主脑验收或重新拆分')
        _task_migrate(state)
        if request_id in (state.get('cost_requests') or {}):
            raise ValueError('重复模型请求标识，已阻止重复发送')
        paid = router.is_paid(model)
        rates = router.rates_for(model, moment)
        ceiling = rates
        amount = Decimal(0)
        if paid:
            if not settings['paid_allowed']:
                raise ValueError('付费执行未授权')
            ceiling = router.rates_for(model, moment, ceiling=True)
            if not rates or not ceiling:
                raise ValueError('缺少可核对单价，已阻止付费请求')
            amount = router.cost(ceiling, input_bound, output_bound)
            ledger = read_ledger()
            row = _day_row(ledger['days'].get(request_day))
            day_settled = _decimal(row.get('settled_cny', 0))
            day_unsettled = _decimal(row.get('unsettled_cny', 0))
            task_cost = task_cost_state(state)
            task_used = task_cost['used_cny']
            cap = min(Decimal(str(settings['task_budget_cny'])),
                      Decimal(str(state.get('route', {}).get('budget_cny', 0))))
            if settings.get('enforce_paid_budget', True):
                if (task_cost['unsettled_cny'] > 0 or task_cost['unknown_calls'] > 0
                        or day_unsettled > 0 or _int_field(row, 'unknown_calls') > 0
                        or _int_field(row, 'pending_calls') > 0):
                    raise ValueError('预算检查存在未结算或待核对费用；请核对后再启用金额上限')
                if task_used >= cap or day_settled >= Decimal(str(settings['daily_budget_cny'])):
                    raise ValueError('按用量计算费用已达到任务或今日预算，已停止后续请求')
            row['reserved_cny'] = str(_decimal(row.get('reserved_cny', 0)) + amount)
            row['calls'] = _int_field(row, 'calls') + 1
            row['unsettled_cny'] = str(day_unsettled + amount)
            row['pending_calls'] = _int_field(row, 'pending_calls') + 1
            ledger['days'][request_day] = row
            ledger['days'] = dict(sorted(ledger['days'].items())[-31:])
            ledger['schema'] = LEDGER_SCHEMA
            registry.write_json(ledger_path(), ledger)
        record = {'schema': LEDGER_SCHEMA, 'request_id': request_id, 'day': request_day,
                  'at': moment.isoformat(), 'period': _period(model, moment), 'billable': paid,
                  'model': model.get('model'), 'provider': model.get('provider'),
                  'rates': rates, 'ceiling_rates': ceiling, 'input_bound': int(input_bound),
                  'output_bound': int(output_bound), 'reserved_cny': str(amount), 'state': 'pending',
                  'usage': None, 'cost_cny': None,
                  'note': '保守预留待结算' if paid else '自建模型，无 API 账单'}
        requests = state.get('cost_requests') if isinstance(state.get('cost_requests'), dict) else {}
        requests[request_id] = record
        updates = {'model_calls': calls + 1,
                   'budget_reserved_cny': float(_decimal(state.get('budget_reserved_cny', 0)) + amount),
                   'cost_settled_cny': float(_decimal(state.get('cost_settled_cny', 0))),
                   'cost_settled_calls': int(state.get('cost_settled_calls', 0) or 0),
                   'cost_unsettled_cny': float(_decimal(state.get('cost_unsettled_cny', 0)) + amount),
                   'cost_unknown_calls': int(state.get('cost_unknown_calls', 0) or 0),
                   'cost_requests': _trim_requests(requests), 'cost_schema': LEDGER_SCHEMA}
        if 'cost_legacy_unsettled_cny' in state:
            updates['cost_legacy_unsettled_cny'] = float(_decimal(state.get('cost_legacy_unsettled_cny', 0)))
        registry.patch_task(run, **updates)
    return amount


def settle(run, request_id, usage, outcome='ok', note=''):
    """Settle one request exactly once by request id.

    A repeated usage event or repeated settle call is a no-op. Missing/invalid
    usage keeps the conservative reservation in the unsettled bucket; it is
    never booked as actual cost and never silently erased.
    """
    request_id = str(request_id)
    with registry.file_lock(registry.HERE / 'budget.lock'):
        state = registry.read_task(run)
        _task_migrate(state)
        requests = state.get('cost_requests') if isinstance(state.get('cost_requests'), dict) else {}
        record = requests.get(request_id)
        if not isinstance(record, dict):
            return {'request_id': request_id, 'state': 'untracked', 'cost_cny': None, 'duplicate': False}
        if record.get('state') != 'pending':
            return {'request_id': request_id, 'state': record.get('state'),
                    'cost_cny': (float(_decimal(record['cost_cny'])) if record.get('cost_cny') else None),
                    'duplicate': True}
        normalized = router.normalize_usage(usage)
        rates = record.get('rates') or {}
        if (record.get('billable', True) and normalized['valid']
                and not normalized.get('cache_complete', False)
                and rates.get('cache_hit_input') != rates.get('cache_miss_input')):
            normalized = {**normalized, 'valid': False,
                          'note': '缺少缓存用量明细，无法确认输入费用，费用待核对'}
        reserved = _decimal(record.get('reserved_cny', 0))
        day = record.get('day') or day_key()
        ledger = read_ledger()
        row = _day_row(ledger['days'].get(day))
        day_settled = _decimal(row.get('settled_cny', 0))
        if not record.get('billable', True):
            new_state = 'free'
            cost = Decimal(0)
        elif normalized['valid'] and record.get('rates') is not None:
            new_state = 'settled'
            cost = router.usage_cost(record['rates'], normalized)
        else:
            new_state = 'unknown'
            cost = None
        receipts = ledger.setdefault('settlements', {})
        receipt_key = run.name + ':' + request_id
        receipt = receipts.get(receipt_key)
        if receipt:
            # The ledger committed but the task-file update may have failed.
            # Replay its exact outcome; never update daily counters twice.
            new_state = receipt['state']
            cost = _decimal(receipt['cost_cny']) if receipt['cost_cny'] is not None else None
            normalized = receipt['normalized']
            note = receipt['note']
        if new_state == 'unknown':
            # Unconfirmed cost: keep the conservative reservation counted, never book it as spent.
            day_unsettled = _decimal(row.get('unsettled_cny', 0))
            task_unsettled = _decimal(state.get('cost_unsettled_cny', 0))
        else:
            day_unsettled = max(Decimal(0), _decimal(row.get('unsettled_cny', 0)) - reserved)
            task_unsettled = max(Decimal(0), _decimal(state.get('cost_unsettled_cny', 0)) - reserved)
        if record.get('billable', True) and not receipt:
            row['unsettled_cny'] = str(day_unsettled)
            row['pending_calls'] = max(0, _int_field(row, 'pending_calls') - 1)
            if new_state == 'settled':
                row['settled_cny'] = str(day_settled + cost)
                row['settled_calls'] = _int_field(row, 'settled_calls') + 1
            elif new_state == 'unknown':
                row['unknown_calls'] = _int_field(row, 'unknown_calls') + 1
            ledger['days'][day] = row
            ledger['days'] = dict(sorted(ledger['days'].items())[-31:])
            receipts[receipt_key] = {'state': new_state, 'day': day,
                'cost_cny': str(cost) if cost is not None else None,
                'normalized': normalized, 'note': note}
            ledger['settlements'] = {key: value for key, value in receipts.items() if value['day'] in ledger['days']}
            ledger['schema'] = LEDGER_SCHEMA
            registry.write_json(ledger_path(), ledger)
        updates = {'cost_unsettled_cny': float(task_unsettled), 'usage_live': None,
                   'cost_requests': requests}
        if 'cost_legacy_unsettled_cny' in state:
            updates['cost_legacy_unsettled_cny'] = float(_decimal(state.get('cost_legacy_unsettled_cny', 0)))
        if new_state == 'settled':
            updates['cost_settled_cny'] = float(_decimal(state.get('cost_settled_cny', 0)) + cost)
            updates['cost_settled_calls'] = int(state.get('cost_settled_calls', 0) or 0) + 1
        elif new_state == 'unknown':
            updates['cost_unknown_calls'] = int(state.get('cost_unknown_calls', 0) or 0) + 1
        if normalized['valid']:
            total = dict(state.get('usage', {})) if isinstance(state.get('usage'), dict) else {}
            for name, value in normalized['usage'].items():
                total[name] = int(total.get(name, 0) or 0) + value
            updates['usage'] = total
        record['state'] = new_state
        record['settled_at'] = registry.now()
        record['cost_cny'] = str(cost) if cost is not None else None
        record['usage'] = normalized['usage'] if normalized['valid'] else None
        record['issues'] = normalized['issues']
        record['note'] = note or normalized['note'] or OUTCOME_NOTES.get(outcome) or '费用待核对'
        requests[request_id] = record
        updates['cost_requests'] = _trim_requests(requests)
        registry.patch_task(run, **updates)
        return {'request_id': request_id, 'state': new_state, 'duplicate': False, 'issues': normalized['issues'],
                'cost_cny': float(cost) if cost is not None else None, 'reserved_cny': float(reserved),
                'usage': record['usage'], 'day': day}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Relay:
    def __init__(self, run, model, settings, port=0):
        self.run, self.model, self.settings = run, model, settings
        self.token = secrets.token_urlsafe(32)
        self._local = threading.local()
        relay = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reject(self, message, status=402):
                data = json.dumps({'error': {'message': message, 'type': 'budget_guard', 'code': 'budget_guard'}}).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.client_address[0] != '127.0.0.1' or not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + relay.token):
                    return self.reject('本地模型网关认证失败', 403)
                if self.path not in {'/v1/chat/completions', '/chat/completions'}:
                    return self.reject('此网关仅允许对话补全', 404)
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 < length <= 8 * 1024 * 1024:
                        raise ValueError('请求大小超出网关范围')
                    body = json.loads(self.rfile.read(length))
                    if body.get('model') != relay.model['model']:
                        raise ValueError('请求模型与已选择模型不一致')
                    messages = body.get('messages')
                    if not isinstance(messages, list):
                        raise ValueError('模型请求缺少消息列表')
                    for message in messages:
                        content = message.get('content')
                        if isinstance(content, list) and any(not isinstance(c, dict) or c.get('type') != 'text' for c in content):
                            raise ValueError('当前预算网关仅支持文本输入')
                    # Offload large tool results before metering and sending. The
                    # provider's usage then describes the request that was really
                    # sent, while the full original evidence stays readable on
                    # disk. Context control must never become a model-call
                    # failure, so a failure keeps the original content.
                    try:
                        context_metrics = context_manager.prepare_request(body, relay.run)
                    except Exception:  # noqa: BLE001 - see above
                        context_metrics = {}
                    # Output goal provenance and the model capability bound are
                    # captured before the body is normalized, so a task-level
                    # target is never mislabelled as an explicit one. One task
                    # read serves both the metering decision and the record.
                    task_state = relay.task_state()
                    resolved_output = relay.output_target(body, task_state)
                    if resolved_output is None:
                        raise ValueError('模型输出上限格式不正确')
                    body.pop('max_completion_tokens', None)
                    thinking = thinking_config.apply(body, relay.model)
                    ceiling = relay.model.get('max_output_tokens', 4096)
                    output = resolved_output['target'] or min(4096, ceiling or 4096)
                    body['max_tokens'] = output
                    if body.get('stream'):
                        body['stream_options'] = {**body.get('stream_options', {}), 'include_usage': True}
                    raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
                    # Reservation-only worst case: this byte bound deliberately
                    # overestimates text tokens. It never rejects a request; the
                    # window decision below uses the metered count instead.
                    input_bound = len(raw) + 1024 + len(messages) * 64
                    if len(raw) > PROTOCOL_BYTE_CEILING:
                        raise ValueError('请求体字节数超出本地网关协议安全上限（大工具结果已尝试外置后仍过大），请缩小上下文')
                    meter = relay.measure(body, raw, task_state=task_state,
                                          output_target=resolved_output, llm_ceiling=ceiling)
                    if meter.get('guard') == 'reject':
                        raise ValueError(token_meter.guard_message(meter))
                    request_id = secrets.token_hex(8)
                    relay._local.request_id = request_id
                    relay._local.latest_usage = None
                    relay._local.final_usage = None
                    relay._local.meter_record = meter
                    relay._local.calibration_done = False
                    relay._local.calibration_pending = None
                    # At most one automatic transport retry per logical request.
                    # The flag lives on this handler's thread, so it can never be
                    # shared with another in-flight request.
                    relay._local.transport_retry_used = False
                    reserve(relay.run, relay.model, relay.settings, input_bound, output,
                            request_id=request_id)
                    registry.patch_task(relay.run, request_thinking={key: body[key] for key in ('reasoning_effort', 'thinking', 'enable_thinking', 'chat_template_kwargs') if key in body},
                                        thinking_runtime=thinking, thinking_selected_level=thinking['selected'],
                                        request_tool_names=[tool.get('function', {}).get('name') for tool in body.get('tools', [])],
                                        request_system_prefix_bytes=sum(len(str(message.get('content', '')).encode('utf-8')) for message in messages if message.get('role') in {'system', 'developer'}),
                                        request_body_bytes=len(raw),
                                        **context_manager.context_fields(context_metrics),
                                        **token_meter.meter_fields(meter))
                except (ValueError, TypeError, KeyError) as error:
                    # A request-level guard rejection, not a provider fault: it is
                    # recorded with scope=request so a later successful request can
                    # supersede it instead of poisoning the whole task outcome.
                    registry.patch_task(relay.run, budget_error=str(error),
                                        budget_error_scope='request')
                    return self.reject(str(error))
                except (OSError, RuntimeError):
                    return self.reject('工作台任务记录暂时无法读取或保存，请稍后重试', 503)
                headers = {'Content-Type': 'application/json'}
                if relay.model.get('api_key'):
                    headers['Authorization'] = 'Bearer ' + relay.model['api_key']
                upstream_url = relay.model['api_base'].rstrip('/') + '/chat/completions'
                opener = build_opener(ProxyHandler({}), NoRedirect()) if relay.model.get('bypass_proxy') else build_opener(NoRedirect())
                started = False
                outcome = 'ok'
                try:
                    # One logical request: the same body and the same model, with
                    # at most one extra *provably safe* transport attempt while
                    # nothing has reached the caller yet.  There is no whole-job
                    # re-dispatch and no provider/model substitution here.
                    while True:
                        request = Request(upstream_url, data=raw, headers=headers)
                        # Evidence for this attempt only.  A socket loop ending is
                        # *not* proof that a generation completed: only a valid
                        # terminal completion (SSE ``[DONE]``/finish_reason, or a
                        # non-stream JSON completion) may resolve an earlier fault.
                        terminal_proof = False
                        error_payload = False
                        try:
                            with opener.open(request, timeout=UPSTREAM_SOCKET_TIMEOUT) as response:
                                self.send_response(response.status)
                                self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                                self.send_header('Connection', 'close')
                                self.end_headers()
                                started = True
                                if 'text/event-stream' in response.headers.get('Content-Type', ''):
                                    for line in response:
                                        if (relay.run / 'cancelled.flag').exists():
                                            outcome = 'cancelled'
                                            break
                                        if line.startswith(b'data:'):
                                            payload = line[5:].strip()
                                            if payload == b'[DONE]':
                                                terminal_proof = True
                                                relay._local.final_usage = relay._local.latest_usage
                                            elif payload:
                                                try:
                                                    event = json.loads(payload)
                                                    if event.get('error'):
                                                        error_payload = True
                                                    if event.get('usage'):
                                                        relay.record_usage(event['usage'], final=event.get('choices') == [])
                                                    for choice in event.get('choices') or []:
                                                        if choice.get('finish_reason'):
                                                            terminal_proof = True
                                                except (ValueError, AttributeError):
                                                    pass
                                        self.wfile.write(line)
                                        self.wfile.flush()
                                else:
                                    data = response.read()
                                    event = None
                                    try:
                                        event = json.loads(data)
                                    except (ValueError, AttributeError):
                                        event = None
                                    if isinstance(event, dict) and not event.get('error'):
                                        if event.get('usage'):
                                            relay.record_usage(event['usage'], final=True)
                                        if isinstance(event.get('choices'), list):
                                            terminal_proof = True
                                    elif isinstance(event, dict):
                                        error_payload = True
                                    self.wfile.write(data)
                            if outcome == 'cancelled':
                                pass
                            elif terminal_proof and not error_payload:
                                # A valid terminal completion proves that an
                                # earlier transient fault really was recovered.
                                relay.record_recovery(request_id)
                            else:
                                # Keep the unresolved fault and the cost
                                # uncertainty: never clear evidence just because
                                # a socket loop ended, and never replay a stream.
                                outcome = 'stream_interrupted' if started else 'unconfirmed_terminal'
                                relay.record_fault(
                                    recovery_retry.CATEGORY_PROTOCOL if error_payload
                                    else recovery_retry.CATEGORY_UNKNOWN,
                                    http=getattr(response, 'status', None), channel='gateway',
                                    note='接口返回了非成功/无有效终止证据的内容，未确认生成是否完成，费用待核对')
                            break
                        except BrokenPipeError:
                            # The local caller went away; never a provider fault.
                            outcome = 'client_disconnected'
                            break
                        except ConnectionResetError as error:
                            # A reset is a provider transport fault when nothing was
                            # delivered; once content has been sent it is recorded
                            # and reported, never replayed.
                            decision = relay.transport_fault(error, started=started, request_id=request_id)
                            if decision['retry']:
                                continue
                            outcome = 'stream_interrupted' if started else 'http_error'
                            if not started:
                                self.reject(decision['message'], 502)
                            break
                        except Exception as error:
                            decision = relay.transport_fault(error, started=started, request_id=request_id)
                            if decision['retry']:
                                continue
                            # Never persist a raw provider error that could carry
                            # credentials, a prompt or a response body: the record
                            # keeps only the safe category and HTTP status.
                            outcome = 'stream_interrupted' if started else 'http_error'
                            if not started:
                                self.reject(decision['message'], 502)
                            break
                finally:
                    self.close_connection = True
                    relay.finish_request(request_id, outcome)

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def output_target(self, body, task_state=None):
        """Resolve this request's generation goal, or None when the body is malformed.

        The model's ``max_output_tokens`` is a capability ceiling, not this
        request's goal: an explicit ``max_tokens`` or a task-level
        ``request_output_tokens`` states the goal, and a missing goal becomes a
        recorded 4096 estimate rather than a silent hard cap. The returned dict
        keeps the goal's provenance separate from the capability bound.
        """
        state = task_state if isinstance(task_state, dict) else self.task_state()
        task_target = state.get('request_output_tokens')
        if 'max_tokens' in body and not _valid_token_bound(body.get('max_tokens')):
            return None
        try:
            return token_meter.resolve_output_target(body, self.model, task_target)
        except ValueError:
            return None

    def measure(self, body, raw, task_state=None, output_target=None, llm_ceiling=None):
        """Meter one request before it is sent.

        Metering is advisory bookkeeping: if it cannot run at all, the request
        still goes out with an explicit warning, because token metering must
        never be reported as (or become) a model-call failure. Only an exact
        count with verified window semantics yields ``guard == 'reject'``.
        """
        try:
            evidence = token_meter.read_evidence(EVIDENCE_PATH)
            metrics = None
            if self.model.get('provider') == 'local':
                metrics = token_meter.read_engine_metrics_cached(self.model)
            record = token_meter.measure_for_relay(self.model, body,
                                                   task_state=task_state if isinstance(task_state, dict) else self.task_state(),
                                                   settings=self.settings, evidence=evidence,
                                                   metrics=metrics, output_target=output_target,
                                                   llm_ceiling=llm_ceiling)
            try:
                registry.patch_task(self.run, meter_record=token_meter.record_summary(record),
                                    meter_guard_note=('; '.join(record.get('warnings') or []))[:1000])
            except (OSError, ValueError, RuntimeError):
                pass
            return record
        except Exception as error:  # noqa: BLE001 - metering must not block the request
            fallback = {'schema': token_meter.SCHEMA, 'guard': 'warn',
                        'warnings': ['token 计量不可用（' + type(error).__name__ + '），本请求按未计量放行'],
                        'sources': {'used': token_meter.SOURCE_NONE,
                                    'precision': token_meter.UNAVAILABLE,
                                    'method': None, 'covers_tools': False, 'covers_template': False},
                        'input': {'tokens': None, 'precision': token_meter.UNAVAILABLE,
                                  'source': token_meter.SOURCE_NONE, 'method': None},
                        'output': {'target': body.get('max_tokens'), 'origin': 'explicit_max_tokens'},
                        'window': {'kind': token_meter.WINDOW_UNVERIFIED, 'fit': {'state': 'unknown'},
                                   'semantics_verified': False, 'enforceable': False},
                        'identity': {}, 'model_id': self.model.get('id'), 'model': self.model.get('model'),
                        'provider': self.model.get('provider')}
            try:
                registry.patch_task(self.run, meter_error=type(error).__name__,
                                    meter_guard_note=fallback['warnings'][0])
            except Exception:
                pass
            return fallback

    def task_state(self):
        try:
            return registry.read_task(self.run)
        except Exception:
            return {}

    def record_usage(self, usage, final=False):
        """Buffer the latest usage event of the in-flight request (replace, not add).

        The provider's real ``prompt_tokens`` also becomes a post-hoc
        calibration observation that links the metering source to the returned
        usage. It is observability only and never feeds the pre-send estimate.
        """
        if isinstance(usage, dict):
            self._local.latest_usage = usage
            if final:
                self._local.final_usage = usage
            self._calibrate(usage, final)
            try:
                registry.patch_task(self.run, usage_live={'request_id': getattr(self._local, 'request_id', None),
                                                          'usage': usage})
            except (OSError, ValueError, RuntimeError):
                pass

    def _calibrate(self, usage, final):
        if getattr(self._local, 'calibration_done', False):
            return
        if not final:
            self._local.calibration_pending = usage
            return
        record = getattr(self._local, 'meter_record', None)
        if not isinstance(record, dict):
            return
        self._local.calibration_done = True
        try:
            observation = token_meter.record_calibration(self.model, record, usage)
        except Exception:
            observation = None
        if observation is None:
            return
        try:
            registry.patch_task(self.run, usage_calibration={**observation,
                                                             'summary': token_meter.calibration_snapshot()})
        except (OSError, ValueError, RuntimeError):
            pass

    def _flush_pending_calibration(self):
        usage = getattr(self._local, 'calibration_pending', None)
        if usage is not None:
            self._calibrate(usage, True)

    def finish_request(self, request_id, outcome):
        try:
            self._flush_pending_calibration()
            return settle(self.run, request_id, getattr(self._local, 'final_usage', None), outcome=outcome)
        except Exception as error:  # settlement bookkeeping must never break the relay loop
            try:
                registry.patch_task(self.run, settlement_error=str(error)[:500])
            except Exception:
                pass
            return None

    # ------------------------------------------------- transient fault recovery

    def cancelled(self):
        try:
            return (self.run / 'cancelled.flag').exists()
        except OSError:
            return False

    def record_fault(self, category, http=None, channel='gateway', note=None):
        """Append one compact, safe fault event and keep the legacy field label.

        Only a fixed category label plus at most the HTTP status is persisted;
        provider bodies, prompts, credentials and raw exception text never are.
        """
        try:
            state = self.task_state()
            event = recovery_retry.compact_event(
                channel, category, at=registry.now(),
                detail=note or recovery_retry.category_label(category), http=http)
            events = recovery_retry.append_event(state.get(recovery_retry.FAULT_EVENTS_KEY), event)
            label = recovery_retry.event_summary(event)
            fields = {'fault_events': events, 'fault_status': 'unresolved',
                      'fault_category': category,
                      'fault_evidence': recovery_retry.recovery_evidence({**state, 'fault_events': events})}
            # Legacy mirrors keep older readers working; fault_events stays the
            # authoritative record for outcome attribution.  The ``_scope`` marks
            # this as a real fault (vs. a recoverable request-level rejection).
            if channel == 'budget':
                fields['budget_error'] = label
                fields['budget_error_scope'] = 'fault'
            else:
                fields['gateway_error'] = label
                fields['gateway_error_scope'] = 'fault'
            registry.patch_task(self.run, **fields)
            return event
        except Exception:  # bookkeeping must never break the relay loop
            return None

    def record_recovery(self, request_id):
        """Mark unresolved transient faults resolved by this delivered request."""
        try:
            state = self.task_state()
            events = state.get(recovery_retry.FAULT_EVENTS_KEY)
            if not isinstance(events, list) or not events:
                return 0
            updated, changed = recovery_retry.resolve_transient(events, registry.now(), request_id)
            if not changed:
                return 0
            registry.patch_task(self.run, fault_events=updated, fault_status='recovered',
                                fault_recovered_at=registry.now(),
                                fault_evidence=recovery_retry.recovery_evidence({**state, 'fault_events': updated}))
            return changed
        except Exception:
            return 0

    @staticmethod
    def _retry_after(error):
        headers = getattr(error, 'headers', None)
        raw = None
        if headers is not None:
            try:
                raw = headers.get('Retry-After')
            except Exception:
                raw = None
        return recovery_retry.retry_after_seconds(raw)

    def _wait_before_retry(self, delay):
        """Interruptible bounded delay; returns False when the task was cancelled."""
        try:
            remaining = float(delay)
        except (TypeError, ValueError):
            remaining = 0.0
        if not math.isfinite(remaining) or remaining < 0:
            remaining = 0.0
        while remaining > 0:
            if self.cancelled():
                return False
            slice_ = min(0.2, remaining)
            time.sleep(slice_)
            remaining -= slice_
        return not self.cancelled()

    def transport_fault(self, error, *, started, request_id):
        """Classify one upstream failure, record it, and decide the safe retry.

        A retry is allowed only for a provably unaccepted transport failure with
        no byte delivered yet, explicit non-acceptance evidence and only once per
        logical request.  A reset, a timeout or an HTTP 5xx is ambiguous (a proxy
        may answer *after* the upstream accepted and charged), so it is recorded
        and reported instead of replayed.
        """
        info = recovery_retry.classify_exception(error)
        category, status = info['category'], info['http_status']
        retry_after, retry_after_reason = self._retry_after(error)
        retry_used = bool(getattr(self._local, 'transport_retry_used', False))
        cancelled = self.cancelled()
        # Acceptance evidence is explicit and truthful: a refused connection /
        # DNS failure never left this host; an HTTP 429 is an explicit refusal.
        # Everything else (reset, timeout, 502/504) stays ambiguous.
        acceptance = recovery_retry.INHERENT_ACCEPTANCE.get(category)
        if acceptance is None and category not in recovery_retry.RETRYABLE_TRANSPORT:
            acceptance = recovery_retry.ACCEPTANCE_AMBIGUOUS
        decision = recovery_retry.transport_retry_allowed(
            category=category, http_status=status, response_started=started,
            retry_used=retry_used, retry_after=retry_after,
            retry_after_reason=retry_after_reason, cancelled=cancelled,
            acceptance=acceptance)
        if decision['retry']:
            if decision['reason'] == 'retry_after':
                note = recovery_retry.category_label(category) + '；服务端显式拒绝并按有界 Retry-After 重试一次'
            else:
                note = recovery_retry.category_label(category) + '；已证明未被接受，将安全重试一次'
        else:
            note = (recovery_retry.category_label(category) + '；'
                    + recovery_retry.retry_reason_label(decision['reason']))
        self.record_fault(category, status, channel='gateway', note=note)
        message = recovery_retry.category_label(category)
        if isinstance(status, int):
            message += f'（HTTP {status}）'
        message += '；请检查本地引擎/网络后重试（未自动重复发送）'
        if decision['retry']:
            self._local.transport_retry_used = True
            try:
                state = self.task_state()
                # Logical request / transport attempts / charges stay separate:
                # one reservation and one receipt describe one logical request,
                # while transport_retries records the extra socket attempt.
                registry.patch_task(self.run,
                                    transport_attempts=int(state.get('transport_attempts', 1) or 1) + 1,
                                    transport_retries=int(state.get('transport_retries', 0) or 0) + 1,
                                    transport_retry_reason=decision['reason'],
                                    transport_retry_acceptance=acceptance,
                                    transport_retry_delay=float(decision['delay']))
            except Exception:
                pass
            if not self._wait_before_retry(decision['delay']):
                decision = {'retry': False, 'delay': 0.0, 'reason': 'cancelled'}
                message = recovery_retry.category_label(recovery_retry.CATEGORY_CANCELLED) + '；已停止重试'
        return {'retry': bool(decision['retry']), 'category': category,
                'http_status': status, 'message': message, 'reason': decision['reason'],
                'acceptance': acceptance}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
