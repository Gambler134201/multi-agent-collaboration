"""Reviewed 5.8.2/5.8.3 native-host lease transport for task-owned sessions.

Only an already running, same-profile WorkBuddy sidecar is used. No PID/auth
files, ping/userinfo endpoints, existing-session lists or bootstrap keys are
read. The native host remains responsible for credential protection.
"""
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

import workbuddy_executor as wb


class HostUnavailable(wb.ProtocolError):
    pass


def profile_prefix(profile):
    root = Path(profile)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('WorkBuddy profile must be an explicit existing directory')
    digest = hashlib.sha1(str(root).encode('utf-8')).hexdigest()[:12]
    return 'workbuddy-' + digest + '-sidecar-'


def select_channel(profile, names):
    pattern = re.compile(re.escape(profile_prefix(profile) + 'control-') + r'[a-f0-9]{8}')
    matches = [n for n in names if pattern.fullmatch(n)]
    if len(matches) != 1:
        raise HostUnavailable('Expected one native host channel; no host started or guessed')
    return '\\\\.\\pipe\\' + matches[0]


class WindowsPipe:
    """Bounded stream IO, with server-image verification before any RPC."""
    def __init__(self, path, executable, expected_pid=None):
        if os.name != 'nt':
            raise HostUnavailable('Native WorkBuddy pipe transport requires Windows')
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateFileW': ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            'GetNamedPipeServerProcessId': ([wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)], wintypes.BOOL),
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'PeekNamedPipe': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
            'ReadFile': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
            'WriteFile': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            'GetExitCodeProcess': ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.api, name)
            fn.argtypes, fn.restype = args, result
        self.handle = self.api.CreateFileW(path, 0xC0000000, 0, None, 3, 0, None)
        self.closed = threading.Event()
        self.buffer = b''
        if self.handle in (None, ctypes.c_void_p(-1).value):
            self.handle = None
            raise HostUnavailable('Native host pipe could not be opened; no retry')
        try:
            pid = wintypes.ULONG()
            if not self.api.GetNamedPipeServerProcessId(self.handle, ctypes.byref(pid)):
                raise HostUnavailable('Cannot verify native pipe server')
            self.server_pid = pid.value
            if expected_pid is not None and self.server_pid != expected_pid:
                raise HostUnavailable('Native data pipe belongs to a different server')
            process = self.verified_process(self.server_pid, executable)
            self.api.CloseHandle(process)
        except BaseException:
            self.close()
            raise

    def verified_process(self, pid, executable):
        handle = self.api.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            raise HostUnavailable('Cannot inspect native process identity')
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if (not self.api.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) or
                os.path.normcase(buf.value) != os.path.normcase(str(executable))):
            self.api.CloseHandle(handle)
            raise HostUnavailable('Native process does not match the verified WorkBuddy installation')
        return handle

    def write(self, text):
        if self.closed.is_set():
            raise OSError('Native pipe closed')
        data = text.encode('utf-8')
        if len(data) > 4 * 1024 * 1024:
            raise ValueError('Native pipe request exceeded the limit')
        buf = ctypes.create_string_buffer(data)
        written = wintypes.DWORD()
        if not self.api.WriteFile(self.handle, buf, len(data), ctypes.byref(written), None) or written.value != len(data):
            raise OSError('Native pipe write outcome unknown')
        return len(text)

    def flush(self):
        pass

    def readline(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not self.closed.is_set():
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                return (line + b'\n').decode('utf-8', errors='replace')
            if len(self.buffer) > 4 * 1024 * 1024:
                raise wb.ProtocolError('Native pipe frame exceeded the limit')
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('Native pipe response timeout')
            available = wintypes.DWORD()
            if not self.api.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(available), None):
                return ''
            if available.value:
                buf = ctypes.create_string_buffer(min(available.value, 65536))
                read = wintypes.DWORD()
                if not self.api.ReadFile(self.handle, buf, len(buf), ctypes.byref(read), None):
                    return ''
                self.buffer += buf.raw[:read.value]
            else:
                self.closed.wait(0.02)
        return ''

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    def close(self):
        if not self.closed.is_set():
            self.closed.set()
            if self.handle:
                self.api.CloseHandle(self.handle)
                self.handle = None


class LeaseControl:
    def __init__(self, pipe, session_id):
        if not re.fullmatch(r'modeldesk-wb-[a-f0-9]{32}', session_id):
            raise ValueError('Invalid task-owned native session identifier')
        self.pipe, self.session_id = pipe, session_id
        self.counter = 0
        self.create_attempted = False
        self.kill_attempted = False
        self.runtime_id = None

    def request(self, method, params):
        if method not in ('session.create', 'session.kill') or params.get('sessionId') != self.session_id:
            raise ValueError('Native bridge only controls its own new session')
        if method == 'session.create':
            if self.create_attempted:
                raise wb.OutcomeUnknown('Native create already attempted; not resending: ' + self.session_id)
            self.create_attempted = True
        else:
            if self.kill_attempted or not self.runtime_id or params.get('expectedRuntimeId') != self.runtime_id:
                raise ValueError('Native cleanup requires the verified lease runtime')
            self.kill_attempted = True
        self.counter += 1
        packet = {'jsonrpc': '2.0', 'id': self.counter, 'method': method, 'params': params}
        deadline = time.monotonic() + 30
        try:
            self.pipe.write(json.dumps(packet) + '\n')
            while time.monotonic() < deadline:
                line = self.pipe.readline(max(0.001, deadline - time.monotonic()))
                if not line:
                    raise EOFError()
                value = json.loads(line)
                if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
                    raise ValueError('Invalid native control frame')
                # The host may broadcast lifecycle metadata. Never save or
                # return unrequested events or query another native session.
                if type(value.get('id')) is not int or value.get('id') != self.counter or value.get('method'):
                    continue
                if 'error' in value:
                    code = value['error'].get('code') if isinstance(value['error'], dict) else None
                    code = code if type(code) is int else None
                    raise HostUnavailable('Native host rejected the request (code %s)' % code)
                if 'result' not in value:
                    raise ValueError('Native result missing')
                return value['result']
        except HostUnavailable:
            raise
        except Exception:
            raise wb.OutcomeUnknown('Native host result unknown; not resending: ' + self.session_id) from None
        raise wb.OutcomeUnknown('Native host result unknown; not resending: ' + self.session_id)


class OwnedHostProcess:
    def __init__(self, control, data, child_handle, result):
        self.control, self.data = control, data
        self.pid, self.child_handle = result['pid'], child_handle
        self.returncode = None
        self.stdout, self.stderr = data, io.StringIO('')
        self.stdin = self.Input(data)

    class Input:
        def __init__(self, pipe):
            self.pipe, self.closed = pipe, False
        def write(self, text):
            if self.closed:
                raise OSError('Native lease input closed')
            return self.pipe.write(text)
        def flush(self):
            self.pipe.flush()
        def close(self):
            self.closed = True

    def wait(self, timeout=None):
        if self.child_handle is None:
            return self.returncode
        ms = 0xFFFFFFFF if timeout is None else max(0, min(int(timeout * 1000), 0xFFFFFFFE))
        if self.data.api.WaitForSingleObject(self.child_handle, ms) != 0:
            raise subprocess.TimeoutExpired('task-owned WorkBuddy lease', timeout)
        code = wintypes.DWORD()
        self.data.api.GetExitCodeProcess(self.child_handle, ctypes.byref(code))
        self.control.pipe.close()
        self.data.api.CloseHandle(self.child_handle)
        self.child_handle = None
        self.returncode = code.value
        self.data.close()
        return self.returncode

    def terminate(self):
        self.control.request('session.kill', {'sessionId': self.control.session_id,
                                             'expectedRuntimeId': self.control.runtime_id})

    def release(self):
        self.data.close()
        self.control.pipe.close()
        if self.child_handle is not None:
            self.data.api.CloseHandle(self.child_handle)
            self.child_handle = None


class NativeHostClient(wb.NativeChatClient):
    def close(self):
        try:
            super().close()
        finally:
            self.process.release()


def create_client(info, project, profile, model_id, *, names=None, pipe_factory=WindowsPipe,
                  tool_job=None, on_lease=None):
    """Create one native-host-owned lease; built-ins remain disabled, no prompt."""
    project = Path(project)
    if not project.is_absolute() or not project.is_dir():
        raise ValueError('Native lease requires an explicit existing project')
    if info.get('version') not in wb.SUPPORTED_VERSIONS or info.get('product_name') != 'WorkBuddy':
        raise HostUnavailable('Unverified WorkBuddy native-host version')
    channel = select_channel(profile, os.listdir('\\\\.\\pipe\\') if names is None else names)
    session_id = 'modeldesk-wb-' + uuid.uuid4().hex

    def spawn(args, **kwargs):
        if args != wb.command(info, model_id) or Path(kwargs['cwd']).resolve() != project.resolve():
            raise ValueError('Native lease command or project mismatch')
        control = LeaseControl(pipe_factory(channel, info['executable']), session_id)
        if on_lease:
            on_lease({'state': 'creating', 'session_id': session_id, 'channel': channel})
        env = dict(kwargs['env'])
        # This process is physically spawned by the verified native WorkBuddy
        # host. The brain owner/caller remains Model Desk; no keys are supplied.
        env['CODEBUDDY_HOST'] = 'workbuddy-desktop'
        try:
            result = control.request('session.create', {'sessionId': session_id,
                'command': info['executable'], 'args': args[1:], 'cwd': str(project.resolve()),
                'port': 0, 'env': env, 'cols': 120, 'rows': 40})
            expected_data = ('\\\\.\\pipe\\' + profile_prefix(profile) + 'data-' + session_id + '-' +
                             hashlib.sha1(session_id.encode()).hexdigest()[:8])
            if (not isinstance(result, dict) or result.get('sessionId') != session_id or
                    result.get('socketPath') != expected_data or type(result.get('pid')) is not int or
                    result['pid'] <= 1 or not isinstance(result.get('runtimeId'), str) or
                    not re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', result['runtimeId'])):
                raise wb.OutcomeUnknown('Native lease identity not confirmed; no attachment: ' + session_id)
            control.runtime_id = result['runtimeId']
            if on_lease:
                on_lease({'state': 'created', 'session_id': session_id, 'channel': channel,
                          'runtime_id': control.runtime_id, 'pid': result['pid']})
            data = pipe_factory(expected_data, info['executable'], expected_pid=control.pipe.server_pid)
            try:
                child = data.verified_process(result['pid'], info['executable'])
            except BaseException:
                data.close()
                raise
            return OwnedHostProcess(control, data, child, result)
        except BaseException:
            control.pipe.close()
            raise

    return NativeHostClient(info, project, profile, model_id=model_id, popen=spawn, tool_job=tool_job)
