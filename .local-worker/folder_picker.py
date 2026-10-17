"""Explicit-click Windows folder picker; transient jobs never change project settings."""
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time

import project_scope

HERE = Path(__file__).resolve().parent


def find_pwsh():
    bundled = Path(os.environ.get('USERPROFILE', '')) / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe'
    found = str(bundled) if bundled.is_file() else shutil.which('pwsh.exe')
    if not found:
        raise ValueError('未找到 PowerShell 7，无法打开系统目录选择器')
    return found


def native_pick(initial=None):
    if os.name != 'nt':
        raise ValueError('系统目录选择器需要工作台运行在本机 Windows 桌面会话中')
    command = [find_pwsh(), '-NoProfile', '-STA', '-WindowStyle', 'Hidden',
               '-File', str(HERE / 'pick_project_folder.ps1')]
    if initial:
        command += ['-InitialDirectory', project_scope.normalize_project(initial)]
    # Hide the helper console, while the explicitly requested folder dialog remains visible.
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        raw, _ = process.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise ValueError('目录选择已超时，请重新点击选择目录') from None
    if process.returncode or len(raw) > 65536:
        raise ValueError('无法打开目录选择器，请确认工作台在当前 Windows 桌面会话运行')
    try:
        result = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise ValueError('目录选择器返回无效结果') from None
    if not isinstance(result, dict):
        raise ValueError('目录选择器返回无效结果')
    if result.get('status') == 'cancelled':
        return {'status': 'cancelled'}
    if result.get('status') != 'selected':
        raise ValueError('目录选择器返回无效状态')
    return {'status': 'selected', 'project': project_scope.normalize_project(result.get('project'))}


class PickerJobs:
    def __init__(self, picker=native_pick):
        self.picker = picker
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, initial=None):
        if initial:
            initial = project_scope.normalize_project(initial)
        with self.lock:
            now = time.monotonic()
            self.jobs = {key: row for key, row in self.jobs.items() if now - row['_created'] < 600}
            if any(row['status'] == 'pending' for row in self.jobs.values()):
                raise ValueError('目录选择窗口已经打开，请先完成或取消现有选择')
            job_id = secrets.token_hex(16)
            self.jobs[job_id] = {'id': job_id, 'status': 'pending', '_created': now}
        threading.Thread(target=self._run, args=(job_id, initial), daemon=True,
                         name='project-folder-picker').start()
        return {'id': job_id, 'status': 'pending'}

    def _run(self, job_id, initial):
        try:
            result = self.picker(initial)
            if result.get('status') == 'selected':
                result = {'status': 'selected', 'project': project_scope.normalize_project(result.get('project'))}
            elif result.get('status') == 'cancelled':
                result = {'status': 'cancelled'}
            else:
                raise ValueError('目录选择器返回无效状态')
        except Exception as exc:
            # Native helper never returns stdout/stderr or command lines to the UI.
            result = {'status': 'failed', 'error': str(exc) if isinstance(exc, ValueError) else '目录选择失败，请重试'}
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id].update(result)

    def get(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id or ''):
            raise ValueError('目录选择请求不存在或已过期')
        with self.lock:
            row = self.jobs.get(job_id)
            if row is None or time.monotonic() - row['_created'] >= 600:
                raise ValueError('目录选择请求不存在或已过期')
            return {key: value for key, value in row.items() if not key.startswith('_')}


JOBS = PickerJobs()
