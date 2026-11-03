"""Explicit local bootstrap; import/discovery never starts a service or model."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import threading
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

import brain_transport
import panel_startup
from powershell_runtime import find_pwsh

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


def workspace(base_url, timeout=panel_startup.PROBE_BUDGET):
    return panel_startup.workspace(base_url, timeout)


def ensure_service(base_url):
    base_url = brain_transport.normalize_base_url(base_url)
    started = panel_startup.ensure(ROOT, _LOCK,
        lambda timeout: workspace(base_url, timeout),
        lambda timeout: panel_startup.launch(ROOT, find_pwsh(), ROOT / '打开模型工作台.ps1',
                                            base_url, timeout, startup_environment()))
    return {'ready': True, 'started': started, 'url': base_url}


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
