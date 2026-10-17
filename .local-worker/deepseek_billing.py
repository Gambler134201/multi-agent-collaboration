"""Official DeepSeek price schedule and read-only account balance cache."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

import model_registry as registry

CHINA = timezone(timedelta(hours=8))
PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
BALANCE_DOC_URL = "https://api-docs.deepseek.com/zh-cn/api/get-user-balance/"
TOP_UP_URL = "https://platform.deepseek.com/top_up"
RULE_VERIFIED = "2026-10-13"
HOLIDAY_SOURCE = "https://www.beijing.gov.cn/zhengce/zhengcefagui/202511/t20251104_4258873.html"
HOLIDAYS = {2026: (("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"),
                   ("05-01", "05-05"), ("06-19", "06-21"), ("09-25", "09-27"), ("10-01", "10-07"))}
RATES = {"deepseek-flash": {"peak": ("0.04", "2", "8"), "off_peak": ("0.02", "1", "4")},
         "deepseek-v4-pro": {"peak": ("0.30", "9", "27"), "off_peak": ("0.15", "4.5", "13.5")}}
ALIASES = {"deepseek-v4-flash": "deepseek-flash", "deepseek-v4-flash-vision-exp": "deepseek-flash"}
CACHE = {}
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="balance-reader")


def is_deepseek(row):
    return row.get("provider") == "deepseek" or urlparse(row.get("api_base", "")).hostname == "api.deepseek.com"


def is_official(row):
    url = urlparse(row.get("api_base", ""))
    return url.scheme == "https" and url.hostname == "api.deepseek.com" and url.port in {None, 443}


def holiday(day):
    if day.year not in HOLIDAYS:
        return None
    for first, last in HOLIDAYS[day.year]:
        if date.fromisoformat(f"{day.year}-{first}") <= day <= date.fromisoformat(f"{day.year}-{last}"):
            return True
    return False


def period(moment):
    moment = moment.astimezone(CHINA)
    if moment.weekday() >= 5:
        return "off_peak"
    is_holiday = holiday(moment.date())
    if is_holiday is None:
        return "unknown"
    if is_holiday:
        return "off_peak"
    minutes = moment.hour * 60 + moment.minute
    return "peak" if 540 <= minutes < 720 or 840 <= minutes < 1080 else "off_peak"


def pricing(row, moment=None):
    moment = (moment or datetime.now(CHINA)).astimezone(CHINA)
    official = is_official(row)
    current = period(moment) if official else "third_party"
    canonical = ALIASES.get(row.get("model"), row.get("model"))
    rates = RATES.get(canonical, {}).get(current)
    change = None
    if current in {"peak", "off_peak"}:
        for offset in range(32):
            day = moment.date() + timedelta(days=offset)
            for hour in (0, 9, 12, 14, 18):
                candidate = datetime(day.year, day.month, day.day, hour, tzinfo=CHINA)
                following = period(candidate)
                if candidate > moment and following != current:
                    change = {"at": candidate.isoformat(), "period": following,
                              "in_seconds": int((candidate - moment).total_seconds())}
                    break
            if change:
                break
    return {"period": current, "label": {"peak": "峰价时段", "off_peak": "谷价时段",
            "unknown": "节假日日历待更新", "third_party": "第三方接口计价"}[current],
            "beijing_time": moment.isoformat(), "next_change": change,
            "holiday": holiday(moment.date()), "currency": "CNY", "per_tokens": 1000000,
            "rates": {"cache_hit_input": rates[0], "cache_miss_input": rates[1], "output": rates[2]} if rates else None,
            "schedule": "北京时间周一至周五 09:00–12:00、14:00–18:00 为峰价；周末、节假日及其余时段为谷价。",
            "note": "谷价为峰价的 50%" if current == "off_peak" else "谷价时段可节省 50% 模型单价",
            "source": PRICING_URL, "verified_at": RULE_VERIFIED, "holiday_source": HOLIDAY_SOURCE,
            "top_up_url": TOP_UP_URL if official else None}


def normalize_balances(data):
    if not isinstance(data.get("is_available"), bool) or not isinstance(data.get("balance_infos"), list):
        raise ValueError("余额接口返回格式不正确")
    rows = []
    for row in data["balance_infos"]:
        if row.get("currency") not in {"CNY", "USD"}:
            raise ValueError("余额货币格式不正确")
        values = {}
        for field in ("total_balance", "granted_balance", "topped_up_balance"):
            try:
                value = Decimal(str(row[field]))
            except (KeyError, InvalidOperation):
                raise ValueError("余额金额格式不正确") from None
            if not value.is_finite():
                raise ValueError("余额金额格式不正确")
            values[field] = format(value, "f")
        rows.append({"currency": row["currency"], **values})
    return {"is_available": data["is_available"], "balance_infos": rows}


def read_balance(row):
    key = row.get("api_key", "")
    request = Request("https://api.deepseek.com/user/balance", headers={"Authorization": "Bearer " + key})
    opener = build_opener(ProxyHandler({})) if row.get("bypass_proxy") else build_opener()
    with opener.open(request, timeout=15) as response:
        return normalize_balances(json.load(response))


def cache_identity(public_row):
    with registry.registry_lock():
        data = registry.read_registry()
    row = next((r for r in data["models"] if r["id"] == public_row["id"]), None)
    if row is None:
        raise ValueError("模型不存在")
    identity = json.dumps({"base": row["api_base"], "secret": row.get("secret", ""),
        "proxy": row.get("bypass_proxy"), "env": os.environ.get(row.get("api_key_env", ""), "")})
    return row["id"], hashlib.sha256(identity.encode()).hexdigest()


def balance_error(exc):
    if isinstance(exc, HTTPError):
        return {401: "API Key 认证失败，请在模型配置中检查密钥", 402: "账户余额不足，请前往官方充值",
                429: "查询过于频繁，请稍后刷新"}.get(exc.code, f"余额接口暂时不可用（HTTP {exc.code}）")
    if isinstance(exc, ValueError):
        return str(exc)
    return "余额查询暂时失败，请检查网络连接后重试"


def fetch_balance(model_id, identity):
    try:
        row = registry.get_model(model_id, require_enabled=False)
        result = {"status": "ready", **read_balance(row), "updated_at": registry.now(), "error": ""}
    except Exception as exc:
        result = {"status": "error", "error": balance_error(exc), "checked_at": registry.now()}
    with LOCK:
        previous = CACHE.get(model_id)
        if previous is None or previous.get("identity") != identity:
            return
        if result["status"] == "error":
            for field in ("balance_infos", "is_available", "updated_at"):
                if field in previous:
                    result[field] = previous[field]
            result["stale"] = "balance_infos" in result
        CACHE[model_id] = {**result, "identity": identity, "retry_at": time.monotonic() + 60, "refreshing": False}


def balance_state(public_row, force=False):
    if not is_official(public_row):
        return {"status": "unsupported", "error": "第三方接口的余额和价格请在对应服务商平台查看"}
    if not public_row.get("api_key_set"):
        return {"status": "missing_key", "error": "填写 API Key 后可查询账户余额"}
    model_id, identity = cache_identity(public_row)
    schedule = False
    with LOCK:
        previous = CACHE.get(model_id)
        if previous is None or previous.get("identity") != identity:
            previous = {"status": "loading", "identity": identity, "retry_at": 0, "refreshing": False}
        if not previous.get("refreshing") and (force or time.monotonic() >= previous.get("retry_at", 0)):
            previous = {**previous, "refreshing": True}
            schedule = True
        CACHE[model_id] = previous
        result = {k: v for k, v in previous.items() if k not in {"identity", "retry_at"}}
    if schedule:
        POOL.submit(fetch_balance, model_id, identity)
    return result


def enrich_models(data):
    for row in data["models"]:
        if is_deepseek(row):
            row["billing"] = {"pricing": pricing(row), "balance": balance_state(row)}
    return data
