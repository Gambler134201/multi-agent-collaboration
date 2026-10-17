"""Read account quotas through Codex's documented app-server, never inference or tokens."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

SOURCE = 'https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt'
POLL_SECONDS = 60
REFRESH_COOLDOWN = 10


class UsageError(Exception):
    pass


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def normalize(payload):
    """Whitelist public quota fields. Account IDs, tokens and credit-reset actions never escape."""
    mapped = payload.get('rateLimitsByLimitId')
    if isinstance(mapped, dict) and mapped:
        rows = list(mapped.items())
    else:
        legacy = payload.get('rateLimits')
        rows = [(legacy.get('limitId') or 'codex', legacy)] if isinstance(legacy, dict) else []
    buckets = []
    for bucket_id, row in rows:
        if not isinstance(row, dict):
            continue
        windows = []
        for slot in ('primary', 'secondary'):
            window = row.get(slot)
            if not isinstance(window, dict):
                continue
            used = number(window.get('usedPercent'))
            if used is not None:
                used = min(100, max(0, used))
            minutes = number(window.get('windowDurationMins'))
            reset = number(window.get('resetsAt'))
            windows.append({'slot': slot, 'used_percent': used, 'remaining_percent': None if used is None else 100-used,
                            'window_minutes': minutes if minutes is not None and minutes > 0 else None,
                            'resets_at': reset if reset is not None and 0 < reset < 253402300800 else None})
        name = row.get('limitName')
        plan = row.get('planType')
        buckets.append({'id': str(bucket_id)[:100], 'name': (name if isinstance(name, str) and name else str(bucket_id))[:100],
                        'plan_type': plan[:40] if isinstance(plan, str) else None, 'windows': windows})
    buckets.sort(key=lambda b: (b['id'] != 'codex', b['id']))
    return {'buckets': buckets, 'source': SOURCE, 'account_scope': '当前 Codex 登录账号共享额度'}


def find_codex():
    found = shutil.which('codex.exe') or shutil.which('codex')
    if found and (os.name != 'nt' or Path(found).suffix.lower() == '.exe'):
        return found
    if os.name == 'nt':
        local_appdata = os.environ.get('LOCALAPPDATA')
        if local_appdata:
            candidates = list((Path(local_appdata) / 'OpenAI/Codex/bin').glob('*/codex.exe'))
            if candidates:
                return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise UsageError('未找到本机 Codex 程序，无法读取账号额度。')


def query_limits(timeout=25):
    """Only initialize / initialized / account/rateLimits/read; no thread or turn is created."""
    process = subprocess.Popen([find_codex(), 'app-server', '--stdio'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    messages = queue.Queue(maxsize=128)

    def read_stdout():
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                    if isinstance(message, dict) and message.get('id') in (1, 2):
                        messages.put_nowait(message)
                except (ValueError, queue.Full):
                    continue
        finally:
            try:
                messages.put_nowait(None)
            except queue.Full:
                pass

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic()+timeout

    def send(message):
        process.stdin.write(json.dumps(message)+'\n')
        process.stdin.flush()

    def receive(request_id):
        while True:
            left = deadline-time.monotonic()
            if left <= 0:
                raise UsageError('Codex 额度查询超时，请稍后刷新。')
            try:
                message = messages.get(timeout=left)
            except queue.Empty:
                raise UsageError('Codex 额度查询超时，请稍后刷新。') from None
            if message is None:
                raise UsageError('Codex 查询进程已退出，请检查本机登录状态。')
            if message.get('id') != request_id:
                continue
            if 'error' in message:
                # Provider messages can contain private identifiers; expose only a known instruction.
                raise UsageError('Codex 未返回额度，请确认本机已使用 ChatGPT 账号登录。')
            result = message.get('result')
            if not isinstance(result, dict):
                raise UsageError('Codex 额度响应格式无法识别。')
            return result

    try:
        send({'method': 'initialize', 'id': 1, 'params': {'clientInfo': {'name': 'model_desk_usage', 'title': 'Model Desk quota', 'version': '0.1.0'}}})
        receive(1)
        send({'method': 'initialized', 'params': {}})
        send({'method': 'account/rateLimits/read', 'id': 2})
        return receive(2)
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        reader.join(timeout=1)
        process.stdout.close()


class UsageCache:
    def __init__(self, query=query_limits, clock=time.monotonic):
        self.query = query
        self.clock = clock
        self.lock = threading.Lock()
        self.last_attempt = None
        self.good = None
        self.refreshing = False
        self.error = ''

    def _refresh(self):
        try:
            result = normalize(self.query())
            result['updated_at'] = datetime.now(timezone.utc).isoformat()
            with self.lock:
                self.good = result
                self.error = ''
        except Exception as exc:
            # Raw subprocess/auth/provider exception strings never go to the UI or log.
            with self.lock:
                self.error = str(exc) if isinstance(exc, UsageError) else 'Codex 额度读取失败，请稍后刷新。'
        finally:
            with self.lock:
                self.refreshing = False

    def snapshot(self, force=False):
        with self.lock:
            age = float('inf') if self.last_attempt is None else self.clock()-self.last_attempt
            if not self.refreshing and age >= (REFRESH_COOLDOWN if force else POLL_SECONDS):
                self.last_attempt = self.clock()
                self.refreshing = True
                threading.Thread(target=self._refresh, daemon=True, name='codex-usage-read').start()
            result = deepcopy(self.good) if self.good is not None else {'buckets': [], 'updated_at': None, 'source': SOURCE, 'account_scope': '当前 Codex 登录账号共享额度'}
            result.update(status='ready' if self.good is not None else 'error' if self.error else 'loading',
                          refreshing=self.refreshing, stale=bool(self.good is not None and (self.error or age >= POLL_SECONDS)), error=self.error)
            return result


CACHE = UsageCache()


def usage_state(force=False):
    return CACHE.snapshot(force=force)
