"""Explicit native cold-start probe in a temporary, model-free installation.

Runs the real PowerShell launchers with only System32 in PATH and no CODEX_*
variables. A fixture HTTP service replaces the workbench and reads no accounts,
models, tasks or user configuration. Never uses the real installation/port.
"""
from pathlib import Path
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler
import venv
import ctypes
from ctypes import wintypes

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import panel_service
import powershell_runtime

SERVER = r'''
import http.server,json,os,sys,threading
from pathlib import Path
root=Path(__file__).resolve().parent.parent
port=int(sys.argv[1])
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path=='/api/state':
   data=json.dumps({'workspace':str(root)}).encode()
   self.send_response(200);self.end_headers();self.wfile.write(data)
  elif self.path=='/shutdown':
   self.send_response(200);self.end_headers()
   threading.Thread(target=server.shutdown,daemon=True).start()
  else:self.send_response(404);self.end_headers()
 def log_message(self,*args):pass
server=http.server.HTTPServer(('127.0.0.1',port),Handler)
(root/'.local-worker/panel-runtime.json').write_text(json.dumps({'pid':os.getpid(),'url':f'http://127.0.0.1:{port}'}))
server.serve_forever();server.server_close()
'''
SPAWNER = r'''
import subprocess,sys
from pathlib import Path
port=sys.argv[sys.argv.index('--port')+1]
subprocess.Popen([sys.executable,str(Path(__file__).with_name('fixture_server.py')),port],
 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
 close_fds=True,creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP)
'''


def main():
    if os.name != 'nt':
        raise SystemExit('Windows-only explicit native probe')
    source = panel_service.ROOT
    old_root = panel_service.ROOT
    old_environment = dict(os.environ)
    requests = build_opener(ProxyHandler({}))
    url = None
    temporary = tempfile.TemporaryDirectory(prefix='modeldesk-coldstart-')
    root = Path(temporary.name).resolve()
    assert root.parent == Path(tempfile.gettempdir()).resolve() and root.name.startswith('modeldesk-coldstart-')
    try:
        worker = root / '.local-worker'; worker.mkdir()
        venv.EnvBuilder(with_pip=False).create(worker / 'venv')
        for name in ('打开模型工作台.ps1', '启动模型面板.ps1'):
            shutil.copy2(source / name, root / name)
        (worker / 'fixture_server.py').write_text(SERVER, encoding='utf-8')
        (worker / 'start_panel.py').write_text(SPAWNER, encoding='utf-8')
        os.environ.clear()
        os.environ.update({k: v for k, v in old_environment.items()
                           if not k.upper().startswith('CODEX') and k.upper() != 'MODEL_DESK_PWSH'})
        os.environ['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
        executable = powershell_runtime.find_pwsh()
        assert str(Path(executable).parent).lower() not in os.environ['PATH'].lower()
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0));port = reservation.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        panel_service.ROOT = root
        first = panel_service.ensure_service(url)
        second = panel_service.ensure_service(url)
        assert first['ready'] and first['started']
        assert second['ready'] and not second['started']
        assert not (root / '.model-desk').exists() and not (root / '.git').exists()
        proof = {'native_cold_start': 'passed', 'second_open_reused': True,
                 'codex_path_absent': True, 'codex_environment_absent': True,
                 'model_called': False, 'real_panel_touched': False}
    finally:
        handle = None
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        record = root / '.local-worker/panel-runtime.json'
        if record.exists():
            pid = json.loads(record.read_text())['pid']
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if url:
            try:requests.open(url + '/shutdown', timeout=2).close()
            except OSError:pass
            for _ in range(20):
                try:requests.open(url + '/api/state', timeout=.2).close()
                except OSError:break
                time.sleep(.1)
        if handle:
            try:
                if kernel.WaitForSingleObject(handle, 5000) != 0:
                    raise RuntimeError('Fixture process exit not confirmed; no process terminated')
            finally:kernel.CloseHandle(handle)
        panel_service.ROOT = old_root
        os.environ.clear();os.environ.update(old_environment)
        temporary.cleanup()
    print(json.dumps(proof))


if __name__ == '__main__':
    main()
