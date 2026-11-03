"""Host-neutral, explicit-only panel readiness and bounded startup policy."""
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

STARTUP_BUDGET = 32.0  # below Codex bridge (45s) and DSH adapter (50s)
LAUNCH_BUDGET = 28.0
PROBE_BUDGET = 3.0  # Windows can take ~2s to report a refused loopback connect


class StartupError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'[{code}] {message}')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def same_workspace(found, root):
    return (isinstance(found, str) and Path(found).is_absolute()
            and os.path.normcase(os.path.realpath(found)) == os.path.normcase(str(Path(root).resolve())))


def _read_response(response, maximum, deadline):
    chunks, size = [], 0
    while True:
        if response.isclosed():
            return b''.join(chunks)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('readiness deadline')
        # Recompute the socket deadline for each available chunk, so a slowly
        # streaming unknown service cannot reset the total budget indefinitely.
        response.fp.raw._sock.settimeout(remaining)
        chunk = response.read1(min(8192, maximum + 1 - size))
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk); size += len(chunk)
        if size > maximum:
            raise ValueError('oversized readiness response')


def workspace(base_url, timeout=PROBE_BUDGET):
    """Cheap readiness request; bounded legacy fallback only on HTTP 404."""
    deadline = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for path in ('/api/ready', '/api/state'):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StartupError('probe_timeout', '就绪检查超时；保留已有服务，不重复启动。')
        try:
            with opener.open(base_url + path, timeout=remaining) as response:
                maximum = 16384 if path == '/api/ready' else 2 * 1024 * 1024
                raw = _read_response(response, maximum, deadline)
                data = json.loads(raw.decode('utf-8'))
        except HTTPError as error:
            if error.code == 404 and path == '/api/ready':
                error.close()
                continue
            error.close()
            raise StartupError('identity_unknown', '端口响应无法核实；不会停止或替换已有服务。') from None
        except (TimeoutError, URLError, OSError) as error:
            reason = getattr(error, 'reason', error)
            if isinstance(reason, ConnectionRefusedError) or getattr(reason, 'winerror', None) == 10061:
                return None
            raise StartupError('probe_unconfirmed', '端口连接未确认；保留已有服务，请稍后重新打开。') from None
        except (ValueError, UnicodeError):
            raise StartupError('identity_unknown', '端口没有可信的工作台安装目录声明。') from None
        if (not isinstance(data, dict) or not isinstance(data.get('workspace'), str) or not Path(data['workspace']).is_absolute()
                or (path == '/api/ready' and (data.get('service') != 'model-desk' or data.get('ready') is not True))):
            raise StartupError('identity_unknown', '端口没有可信的工作台就绪声明。')
        return data['workspace']


def launch(root, pwsh, launcher, base_url, timeout, environment=None):
    if not Path(launcher).is_file():
        raise StartupError('launcher_missing', '找不到模型工作台启动脚本，请检查安装文件。')
    command = [pwsh, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(launcher),
               '-NoBrowser', '-StartupTimeoutSeconds', str(max(1, math.floor(timeout - 1))),
               '-Port', str(urlparse(base_url).port)]
    with tempfile.TemporaryFile() as sink:
        try:
            result = subprocess.run(command, cwd=str(root), stdin=subprocess.DEVNULL,
                                    stdout=sink, stderr=sink, timeout=timeout,
                                    env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise StartupError('launcher_timeout', '启动器超时，后台结果尚未确认；请查看 .local-worker/panel.stderr.log。') from None
        except OSError:
            raise StartupError('spawn_failed', '无法创建原生 PowerShell 启动进程，请检查安装路径与系统权限。') from None
        if result.returncode:
            sink.seek(0)
            # Never copy arbitrary launcher output or credentials into UI.
            output = sink.read(16384).decode('utf-8', 'replace')
            match = re.search(r'MD_START_(?:BUSY|FOREIGN|TIMEOUT|SPAWN|INSTALL)', output)
            code = match.group() if match else 'launcher_exit'
            raise StartupError(code, f'启动脚本退出（{result.returncode}）；请查看 .local-worker/panel.stderr.log 或手动运行启动脚本。')


def ensure(root, lock, probe, start, timeout=STARTUP_BUDGET):
    """Lock, probe, launch and final confirmation share one monotonic budget."""
    deadline = time.monotonic() + max(0, min(float(timeout), STARTUP_BUDGET))
    if not lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise StartupError('startup_busy', '另一个入口仍在启动工作台；未重复创建进程，请稍后重新打开。')
    try:
        def check():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            found = probe(timeout=min(PROBE_BUDGET, remaining))
            if found is not None and not same_workspace(found, root):
                raise StartupError('foreign_installation', '端口属于另一份工作台；保留原服务，不重启、不替换。')
            return found is not None
        if check():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 2:
            raise StartupError('startup_timeout', '启动预算已耗尽；未重复创建进程，请稍后重新打开。')
        failure = None
        try:
            start(timeout=min(LAUNCH_BUDGET, remaining - 2))
        except StartupError as error:
            if error.code in ('spawn_failed', 'launcher_missing'):
                raise
            failure = error
        while time.monotonic() < deadline:
            if check():
                return True
            if failure and failure.code not in ('launcher_timeout', 'MD_START_TIMEOUT', 'MD_START_BUSY'):
                raise failure
            time.sleep(min(.15, max(0, deadline - time.monotonic())))
        raise failure or StartupError('startup_timeout', '启动预算内未确认后台就绪；保留现有进程，请查看 .local-worker/panel.stderr.log。')
    finally:
        lock.release()
