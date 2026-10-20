"""Explicit local bootstrap; import/discovery never starts a service or model."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

import brain_transport

ROOT = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()


def startup_environment():
    # Inherit ordinary runtime/model configuration without logging any values.
    # The shared service must never inherit this caller's private chat bridge.
    blocked = {'CODEX_THREAD_ID', 'CODEX_APP_TOOLS_PIPE_PATH', 'MODEL_DESK_COMPLETION_HOST'}
    return {key: value for key, value in os.environ.items()
            if key.upper() not in blocked and not key.upper().startswith('MODEL_DESK_DSH_WAKE_')}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def workspace(base_url):
    request = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with request.open(base_url + '/api/state', timeout=2) as response:
            data = json.loads(response.read(2 * 1024 * 1024).decode('utf-8'))
    except HTTPError as error:
        raise ValueError('端口返回异常 HTTP 状态；不会替换已有服务') from error
    except (URLError, TimeoutError, OSError):
        return None
    except (ValueError, UnicodeError) as error:
        raise ValueError('端口响应无法核实；不会替换已有服务') from error
    if not isinstance(data, dict) or not isinstance(data.get('workspace'), str):
        raise ValueError('端口没有可信的工作台安装目录声明')
    return data['workspace']


def ensure_service(base_url):
    base_url = brain_transport.normalize_base_url(base_url)
    port = urlparse(base_url).port
    with _LOCK:
        found = workspace(base_url)
        if found is not None:
            if os.path.normcase(os.path.realpath(found)) != os.path.normcase(str(ROOT.resolve())):
                raise ValueError('端口属于另一份工作台；保留原服务，不重启、不替换')
            return {'ready': True, 'started': False, 'url': base_url}
        pwsh = shutil.which('pwsh.exe') or shutil.which('pwsh')
        if not pwsh:
            raise ValueError('需要原生 PowerShell 7，不会改用其他 shell')
        launcher = ROOT / '打开模型工作台.ps1'
        if not launcher.is_file():
            raise ValueError('工作台启动脚本不存在')
        try:
            result = subprocess.run([pwsh, '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(launcher), '-NoBrowser', '-Port', str(port)],
                cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=45,
                env=startup_environment(),
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError('启动未确认；没有停止服务或自动重试，请查看本机启动日志') from error
        if result.returncode != 0 or workspace(base_url) != str(ROOT):
            raise ValueError('工作台启动结果未确认；保留已有进程，不自动重试')
        return {'ready': True, 'started': True, 'url': base_url}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ensure', action='store_true', required=True)
    parser.add_argument('--panel-url', required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(ensure_service(args.panel_url), ensure_ascii=True))
        return 0
    except ValueError as error:
        print(json.dumps({'ready': False, 'error': str(error)}, ensure_ascii=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
