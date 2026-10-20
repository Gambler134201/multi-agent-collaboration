"""Explicit, local-only preparation and installation of the optional DSH bundle.

Preparation does not contact DSH or the controller. Check/installation explicitly
probe the existing brain broker without granting task mutation authority. Only
install/uninstall invoke Desktop's package-manager CLI or edit one User environment
value. No profile patch, account file, credential or session history is inspected.
Unknown CLI effects are recorded for inspection, never retried or blindly undone.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time

import brain_context
import brain_transport
import dsh_brain_plugin

PACKAGE = 'model-desk-dsh-brain'
ENV_NAME = 'MODEL_DESK_DSH_BRAIN_CONFIG'
FILES = ('index.cjs', 'account.cjs', 'wakeup.cjs', 'client.mjs', 'package.json', 'cordis.patch.yml', 'README.md', 'LICENSE')
RUNTIME_FILES = ('brain_mcp_server.py','brain_context.py','brain_transport.py','brain_gateway.py',
                 'workflow_settings.py','host_balance.py','completion_wait.py','desktop_notify.py',
                 'completion_host.py','dsh_notify.py','panel_service.py')
CLIENT_ORIGIN_MARKER = b'"__MODEL_DESK_PANEL_ORIGIN__"'
CLI_RELATIVE = 'dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
SUPPORTED_DSH = '0.2.1-rc.2'
BUNDLE = Path(__file__).resolve().parent.parent / 'integrations' / 'dsh-brain'


class SetupError(ValueError):
    pass


class PackageCommandFailed(SetupError):
    def __init__(self, exit_code, output):
        # Preserve useful fixed error categories, never raw registry output.
        known = ('ERR_PNPM_NO_OFFLINE_META', 'ERR_PNPM_NO_OFFLINE_TARBALL',
                 'ERR_PNPM_UNEXPECTED_STORE', 'ERR_PNPM_LOCKFILE_CONFIG_MISMATCH',
                 'ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY', 'ERR_PNPM_FETCH_401',
                 'ERR_PNPM_FETCH_403', 'ERR_PNPM_FETCH_404', 'ERR_PNPM_META_FETCH_FAIL')
        self.diagnostic = {'exit_code': int(exit_code), 'error_codes': [code for code in known if code in output]}
        super().__init__('官方 CLI 未成功返回；效果需核查，未自动重试')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        raise SetupError('安装元数据不可读或无效') from None
    if not isinstance(value, dict):
        raise SetupError('安装元数据必须是对象')
    return value


def explicit_path(value, *, directory=False, new=False):
    path = Path(value)
    if not path.is_absolute():
        raise SetupError('需要明确的绝对路径')
    resolved = path.resolve()
    if any(part.lower() == '.model-desk' for part in (*path.parts, *resolved.parts)):
        raise SetupError('安装文件不能位于 .model-desk 或其链接目标')
    if new:
        if path.exists() or path.is_symlink() or not path.parent.is_dir():
            raise SetupError('输出目录必须尚不存在，父目录必须已存在')
    elif not (resolved.is_dir() if directory else resolved.is_file()):
        raise SetupError('所需本机文件或目录不存在')
    return resolved


def profile_manifest(home):
    profile = Path(home) / 'profiles' / 'desktop'
    # Do not let a profile symlink redirect an explicitly selected Home.
    if profile.resolve() != profile or not profile.is_dir():
        raise SetupError('需要由 DSH 初始化、未重定向的 desktop profile')
    manifest = read_json(profile / 'package.json')
    bundles = manifest.get('dsh', {}).get('profile', {}).get('bundles')
    if not isinstance(bundles, list) or not all(isinstance(item, str) for item in bundles):
        raise SetupError('不是有效的 DSH desktop profile')
    if '@deepseek-ai/dsh-web-app' not in bundles:
        raise SetupError('目标不是已确认的 desktop Web profile')
    for field in ('dependencies', 'devDependencies', 'optionalDependencies'):
        if not isinstance(manifest.get(field, {}), dict):
            raise SetupError('profile 依赖字段无效')
    return profile, manifest


def without_ours(manifest):
    clean = json.loads(json.dumps(manifest))
    for field in ('dependencies', 'devDependencies', 'optionalDependencies'):
        if field in clean:
            clean[field].pop(PACKAGE, None)
            if not clean[field]:
                clean.pop(field)
    clean['dsh']['profile']['bundles'] = [item for item in clean['dsh']['profile']['bundles'] if item != PACKAGE]
    return digest(json_bytes(clean))


def has_ours(manifest):
    return (PACKAGE in manifest['dsh']['profile']['bundles'] or any(
        PACKAGE in manifest.get(field, {}) for field in ('dependencies', 'devDependencies', 'optionalDependencies')))


def ours_matches(manifest, folder, home):
    if manifest['dsh']['profile']['bundles'].count(PACKAGE) != 1:
        return False
    if any(PACKAGE in manifest.get(field, {}) for field in ('devDependencies', 'optionalDependencies')):
        return False
    spec = manifest.get('dependencies', {}).get(PACKAGE, '')
    if not isinstance(spec, str) or not spec.startswith('file:'):
        return False
    value = Path(spec[5:])
    if not value.is_absolute():
        value = Path(home) / 'profiles' / 'desktop' / value
    return value.resolve() == folder / 'plugin.tgz'


def probe_dsh(executable):
    """Inspect the installed static runtime, not user configuration."""
    import preset_runtime
    archive = Path(executable).parent / 'resources' / 'app.asar'
    try:
        manifest = json.loads(preset_runtime._asar_read(archive, 'dsh/node_modules/@deepseek-ai/dsh/package.json'))
        preset_runtime._asar_read(archive, CLI_RELATIVE)
    except (ValueError, OSError, KeyError):
        raise SetupError('无法确认该 DSH 安装的官方 desktop CLI') from None
    if manifest.get('version') != SUPPORTED_DSH:
        raise SetupError('该 DSH 版本尚未通过本安装器验证')
    return {'version': manifest['version'], 'cli': str(archive / CLI_RELATIVE)}


def controller_check(plan):
    context = brain_context.build_context(plan['workbench'], 'dsh-setup', 'desktop-installer',
                                          session_binding='unknown', launcher='dsh-desktop-check')
    transport = brain_transport.BrainTransport(plan['panel_url'], timeout=10)
    try:
        transport.connect(context.declaration())
        result = transport.call('describe', {})
        public = result.get('context', {})
        owner, session = public.get('owner'), public.get('session')
        if (not isinstance(owner, dict) or not isinstance(session, dict)
                or owner.get('binding_key') != context.binding_key
                or session.get('binding') != 'unknown'
                or public.get('mutations_allowed') is not False):
            raise SetupError('主脑控制器返回了不匹配的安装预检身份')
        status, account = transport._request('GET', '/api/host-account')
        if (status != 200 or not isinstance(account, dict) or account.get('schema') != 1
                or account.get('source') != 'dsh-account-controller'
                or account.get('refresh') != 'snapshot-only'):
            raise SetupError('目标工作台尚未提供匹配的 DSH 余额接口；请先升级工作台')
    except brain_transport.TransportError:
        raise SetupError('目标工作台没有可用的新主脑接口；先升级/启动匹配的工作台，未安装插件') from None
    finally:
        transport.disconnect()
    return {'brain_interface': True, 'host_account_interface': True, 'task_mutation_authority': False}


def installation_files(root):
    values = {name: digest((Path(root) / '.local-worker' / name).read_bytes()) for name in RUNTIME_FILES}
    for name in ('打开模型工作台.ps1', '启动模型面板.ps1'):
        values['root:' + name] = digest((Path(root) / name).read_bytes())
    return values


def client_bundle(data, panel_url):
    """Bind the prepared client half to the same loopback origin as the Host config.

    The source package keeps a loopback default so it remains usable when linked
    manually. Preparation replaces one quoted marker; a missing or duplicated
    marker fails closed instead of shipping a guessed origin.
    """
    if data.count(CLIENT_ORIGIN_MARKER) != 1:
        raise SetupError('客户端源码的模型工作台来源标记缺失或重复，拒绝打包')
    replacement = json.dumps(panel_url, ensure_ascii=False).encode('utf-8')
    return data.replace(CLIENT_ORIGIN_MARKER, replacement)


def prepare(output, workbench, python, executable, home, panel_url=brain_transport.DEFAULT_BASE_URL):
    folder = explicit_path(output, new=True)
    root = explicit_path(workbench, directory=True)
    interpreter = explicit_path(python)
    exe = explicit_path(executable)
    dsh_home = explicit_path(home, directory=True)
    _, manifest = profile_manifest(dsh_home)
    if has_ours(manifest):
        raise SetupError('已有同名插件，不能收养或覆盖；请使用原安装收据')
    info = probe_dsh(exe)
    config = dsh_brain_plugin.build_config(python=str(interpreter),
        brain_mcp_server=str(root / '.local-worker' / 'brain_mcp_server.py'),
        workflow_settings=str(root / '.local-worker' / 'workflow_settings.py'), panel_url=panel_url)
    fingerprint = installation_files(root)
    source = {}
    for name in FILES:
        path = BUNDLE / name
        if path.is_symlink() or not path.is_file():
            raise SetupError('插件源码清单存在缺项或链接，拒绝打包')
        source[name] = path.read_bytes()
    package = json.loads(source['package.json'])
    if package.get('name') != PACKAGE or package.get('scripts'):
        raise SetupError('插件包身份或安装脚本不符合要求')
    source['client.mjs'] = client_bundle(source['client.mjs'], config['panelUrl'])
    # Exclusive directory creation is the ownership boundary. plan.json is the
    # last commit marker; an interrupted preparation cannot be installed.
    folder.mkdir(mode=0o700)
    with tarfile.open(folder / 'plugin.tgz', 'w:gz') as archive:
        for name, data in source.items():
            member = tarfile.TarInfo('package/' + name)
            member.size, member.mode, member.mtime = len(data), 0o644, 0
            archive.addfile(member, io.BytesIO(data))
    (folder / 'config.json').write_bytes(json_bytes(config))
    plan = {'schema': 1, 'workbench': str(root), 'python': str(interpreter), 'dsh_exe': str(exe),
            'dsh_home': str(dsh_home), 'panel_url': config['panelUrl'], 'host': info,
            'installation': fingerprint, 'artifacts': {name: digest((folder / name).read_bytes())
                                                     for name in ('plugin.tgz', 'config.json')}}
    (folder / 'plan.json').write_bytes(json_bytes(plan))
    return {'prepared': True, 'directory': str(folder), 'installed': False}


def load_plan(folder):
    folder = explicit_path(folder, directory=True)
    plan = read_json(folder / 'plan.json')
    if plan.get('schema') != 1 or set(plan.get('artifacts', {})) != {'plugin.tgz', 'config.json'}:
        raise SetupError('安装计划清单不符合要求')
    for name, expected in plan['artifacts'].items():
        if (folder / name).is_symlink() or digest((folder / name).read_bytes()) != expected:
            raise SetupError('准备后的包或配置已变化，拒绝继续')
    root = explicit_path(plan['workbench'], directory=True)
    explicit_path(plan['python'])
    exe = explicit_path(plan['dsh_exe'])
    home = explicit_path(plan['dsh_home'], directory=True)
    profile_manifest(home)
    if installation_files(root) != plan['installation'] or probe_dsh(exe) != plan['host']:
        raise SetupError('目标工作台或 DSH 安装已变化，请重新准备')
    config = read_json(folder / 'config.json')
    expected_config = dsh_brain_plugin.build_config(python=plan['python'],
        brain_mcp_server=str(root / '.local-worker' / 'brain_mcp_server.py'),
        workflow_settings=str(root / '.local-worker' / 'workflow_settings.py'), panel_url=plan['panel_url'])
    if config != expected_config:
        raise SetupError('配置与安装计划不匹配')
    return folder, plan


class UserEnvironment:
    def __init__(self):
        if os.name != 'nt':
            raise SetupError('实际安装只支持 Windows User 环境，离线准备不受此限制')

    def get(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                value, kind = winreg.QueryValueEx(key, ENV_NAME)
                if kind != winreg.REG_SZ:
                    raise SetupError('已有非本安装器管理的环境值')
                return value
        except FileNotFoundError:
            return None

    def set(self, value):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            if value is None:
                try:
                    winreg.DeleteValue(key, ENV_NAME)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, ENV_NAME, 0, winreg.REG_SZ, value)
        # Notify Explorer; already-running DSH does not inherit this change.
        import ctypes
        result = ctypes.c_size_t()
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                         ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]
        send.restype = ctypes.c_ssize_t
        text = ctypes.c_wchar_p('Environment')
        send(0xffff, 0x001A, 0, ctypes.cast(text, ctypes.c_void_p).value, 2, 2000, ctypes.byref(result))


@contextmanager
def setup_lock(folder, name='operation.lock'):
    path = folder / name
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SetupError('已有安装操作或遗留锁；先核查，不自动重试/删除') from None
    try:
        os.write(fd, str(os.getpid()).encode('ascii'))
        yield
    finally:
        os.close(fd)
        path.unlink()


def save_receipt(folder, value):
    temp = folder / 'receipt.writing'
    with temp.open('xb') as stream:
        stream.write(json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, folder / 'receipt.json')


def receipt_for(folder):
    path = folder / 'receipt.json'
    return read_json(path) if path.exists() else None


def official_cli(plan, arguments):
    env = dict(os.environ, ELECTRON_RUN_AS_NODE='1', DSH_HOME=plan['dsh_home'])
    command = [plan['dsh_exe'], '--expose-internals', plan['host']['cli'],
               'plugin', '--profile', 'desktop', *arguments]
    # Package-manager output may contain arbitrary registry diagnostics. Do not
    # put it into model-visible exceptions or installer receipts.
    result = subprocess.run(command, env=env, cwd=plan['dsh_home'], shell=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        output = (result.stdout + result.stderr).decode('utf-8', 'replace')
        raise PackageCommandFailed(result.returncode, output)


def check(folder, *, environment=None):
    folder, plan = load_plan(folder)
    result = controller_check(plan)
    record = receipt_for(folder)
    state = record.get('state') if record else None
    _, manifest = profile_manifest(plan['dsh_home'])
    installed = bool(state == 'installed' and record.get('plan_hash') == digest(json_bytes(plan))
                     and ours_matches(manifest, folder, plan['dsh_home'])
                     and (environment or UserEnvironment()).get() == str(folder / 'config.json'))
    return {'ready': state is None or installed, **result, 'receipt_state': state,
            'profile': str(Path(plan['dsh_home']) / 'profiles' / 'desktop'),
            'requires_dsh_restart': True, 'installed': installed}


def desktop_stopped(plan):
    """Read process identity only; never close the host or inspect its accounts."""
    if os.name != 'nt':
        raise SetupError('需要在 Windows 核查 DSH 进程状态')
    command = ("$ErrorActionPreference='Stop'; "
               "@(Get-CimInstance Win32_Process | Where-Object { "
               "$_.ExecutablePath -eq $env:MODEL_DESK_SETUP_EXE } | "
               "Select-Object -ExpandProperty ProcessId) | ConvertTo-Json -Compress")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        env=dict(os.environ, MODEL_DESK_SETUP_EXE=plan['dsh_exe']), shell=False,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode or result.stdout.strip():
        raise SetupError('DSH 或其安装进程仍在运行，或无法确认已退出；不继续安装')


def absent_checks(folder, plan, record, environment):
    profile, manifest = profile_manifest(plan['dsh_home'])
    if (has_ours(manifest) or environment.get() is not None
            or os.path.lexists(profile / 'node_modules' / PACKAGE)
            or without_ours(manifest) != record.get('before_unrelated')):
        raise SetupError('不能确认本插件未安装且原配置保持不变；不允许重试')
    for marker in (profile / '.plugin-manager' / 'run.json', profile / 'lock'):
        if os.path.lexists(marker):
            raise SetupError('存在原生安装运行标记或锁；不删除或绕过')
    return {'plugin_references_absent': True, 'package_link_absent': True,
            'environment_absent': True, 'unrelated_manifest_unchanged': True,
            'native_run_and_lock_absent': True}


def reconcile_absent(folder, *, environment=None, stopped=None):
    """Confirm only absence of an installed plugin, not absence of cache effects.

    The original attempt remains in the receipt. This action never runs the CLI;
    a separate, explicit retry is required and is limited to one correction.
    """
    folder, plan = load_plan(folder)
    profile, _ = profile_manifest(plan['dsh_home'])
    with setup_lock(folder), setup_lock(profile, '.model-desk-brain-install.lock'):
        record = receipt_for(folder)
        if (not record or record.get('plan_hash') != digest(json_bytes(plan))
                or record.get('state') != 'attention' or not record.get('effect_unknown')
                or record.get('package_confirmed') or record.get('attempt_history')):
            raise SetupError('不是可核查的首次安装失败收据；不允许恢复重试')
        (stopped or desktop_stopped)(plan)
        checks = absent_checks(folder, plan, record, environment or UserEnvironment())
        original = json.loads(json.dumps(record))
        record.update(state='verified_absent', effect_unknown=False,
                      reconciliation={'checked_at': time.time(), 'desktop_stopped': True, **checks},
                      attempt_history=[original])
        save_receipt(folder, record)
    return {'installed': False, 'reconciled': True, 'retry_automatic': False, **checks}


def install(folder, *, environment=None, runner=None, retry_verified=False, stopped=None,
            resolve_offline_cache=False):
    folder, plan = load_plan(folder)
    profile, _ = profile_manifest(plan['dsh_home'])
    with setup_lock(folder), setup_lock(profile, '.model-desk-brain-install.lock'):
        environment = environment if environment is not None else UserEnvironment()
        record = receipt_for(folder)
        _, before = profile_manifest(plan['dsh_home'])
        value = str(folder / 'config.json')
        if retry_verified and resolve_offline_cache:
            raise SetupError('不能同时指定普通恢复和联网缓存修复')
        history = []
        if record:
            if (record.get('state') == 'installed' and record.get('plan_hash') == digest(json_bytes(plan))
                    and ours_matches(before, folder, plan['dsh_home']) and environment.get() == value):
                return {'installed': True, 'reused': True, 'requires_dsh_restart': True}
            diagnosed_offline = (resolve_offline_cache and record.get('state') == 'attention'
                    and record.get('effect_unknown') and not record.get('package_confirmed')
                    and not record.get('package_network')
                    and len(record.get('attempt_history', [])) <= 1
                    and bool(record.get('cli_diagnostic', {}).get('error_codes'))
                    and set(record['cli_diagnostic']['error_codes']) <= {
                        'ERR_PNPM_NO_OFFLINE_META', 'ERR_PNPM_NO_OFFLINE_TARBALL'})
            verified_retry = (retry_verified and record.get('state') == 'verified_absent'
                    and record.get('plan_hash') == digest(json_bytes(plan))
                    and len(record.get('attempt_history', [])) == 1)
            if (record.get('plan_hash') != digest(json_bytes(plan))
                    or not (diagnosed_offline or verified_retry)):
                raise SetupError('已有安装收据但状态/归属无法确认；先核查或按收据卸载，不重复安装')
            (stopped or desktop_stopped)(plan)
            absent_checks(folder, plan, record, environment)
            history = record.get('attempt_history', [])
            if diagnosed_offline:
                history = [*history, json.loads(json.dumps({k: v for k, v in record.items()
                                                           if k != 'attempt_history'}))]
        elif retry_verified or resolve_offline_cache:
            raise SetupError('明确重试需要原失败收据及未安装核查结果')
        if has_ours(before) or environment.get() is not None:
            raise SetupError('已有同名插件或配置环境值，拒绝接管/覆盖')
        controller_check(plan)  # All preconditions before profile/environment effects.
        baseline = without_ours(before)
        record = {'schema': 1, 'state': 'installing', 'plan_hash': digest(json_bytes(plan)),
                  'before_unrelated': baseline, 'created_at': time.time(), 'effect_unknown': True,
                  'package_network': bool(resolve_offline_cache)}
        if history:
            record['attempt_history'] = history
        save_receipt(folder, record)
        try:
            (runner or official_cli)(plan, ['add', str(folder / 'plugin.tgz'), '--config.ignore-scripts=true',
                                          '--config.auto-install-peers=false',
                                          '--config.offline=' + ('false' if resolve_offline_cache else 'true')])
            record['effect_unknown'] = False
            save_receipt(folder, record)
            _, after = profile_manifest(plan['dsh_home'])
            if not ours_matches(after, folder, plan['dsh_home']) or without_ours(after) != baseline:
                raise SetupError('官方 CLI 结果与预期不一致；保留记录，不覆盖其他元数据')
            record['package_confirmed'] = True
            save_receipt(folder, record)
            if environment.get() is not None:
                raise SetupError('安装期间环境值被其他操作修改，拒绝覆盖')
            environment.set(value)
            if environment.get() != value:
                raise SetupError('环境配置写入未确认')
            record['state'] = 'installed'
            save_receipt(folder, record)
        except Exception as error:
            record['state'] = 'attention'
            if isinstance(error, PackageCommandFailed):
                record['cli_diagnostic'] = error.diagnostic
            save_receipt(folder, record)
            raise SetupError('安装未完全确认；保留收据，未重试或盲目恢复，请核查后按原收据卸载') from None
    return {'installed': True, 'reused': False, 'requires_dsh_restart': True}


def uninstall(folder, *, environment=None, runner=None):
    # A changed/deleted workbench is not a reason to lose the ability to remove
    # an owned plugin, so uninstall validates artifacts/ownership independently.
    folder = explicit_path(folder, directory=True)
    plan = read_json(folder / 'plan.json')
    profile, _ = profile_manifest(explicit_path(plan['dsh_home'], directory=True))
    with setup_lock(folder), setup_lock(profile, '.model-desk-brain-install.lock'):
        record = receipt_for(folder)
        if not record or record.get('plan_hash') != digest(json_bytes(plan)):
            raise SetupError('没有匹配的本安装收据；不能卸载未知插件')
        environment = environment if environment is not None else UserEnvironment()
        _, before = profile_manifest(explicit_path(plan['dsh_home'], directory=True))
        value = str(folder / 'config.json')
        current = environment.get()
        if current not in (None, value):
            raise SetupError('配置环境值已被用户更改；不覆盖或删除')
        if has_ours(before) and not ours_matches(before, folder, plan['dsh_home']):
            raise SetupError('当前同名插件不属于原安装，不能移除')
        if record.get('state') == 'uninstalled':
            if has_ours(before) or current == value:
                raise SetupError('卸载后状态发生变化，不能自动接管')
            return {'uninstalled': True, 'reused': True}
        if record.get('state') not in ('installed', 'installing', 'attention'):
            raise SetupError('未知收据状态，不重复卸载')
        if record.get('effect_unknown'):
            raise SetupError('上次官方 CLI 效果未知；先人工核查，不重发安装或卸载命令')
        baseline = without_ours(before)
        record['state'] = 'removing'
        save_receipt(folder, record)
        try:
            if has_ours(before):
                if probe_dsh(explicit_path(plan['dsh_exe'])) != plan['host']:
                    raise SetupError('DSH 安装版本变化，需先核查')
                record['effect_unknown'] = True
                save_receipt(folder, record)
                (runner or official_cli)(plan, ['remove', PACKAGE, '--config.ignore-scripts=true',
                                              '--config.auto-install-peers=false', '--config.offline=true'])
                record['effect_unknown'] = False
                save_receipt(folder, record)
            _, after = profile_manifest(plan['dsh_home'])
            if has_ours(after) or without_ours(after) != baseline:
                raise SetupError('卸载结果未确认，不能覆盖其他插件')
            if environment.get() != current:
                raise SetupError('卸载期间环境值变化，拒绝恢复')
            if current == value:
                environment.set(None)
            record['state'] = 'uninstalled'
            save_receipt(folder, record)
        except Exception as error:
            record['state'] = 'attention'
            if isinstance(error, PackageCommandFailed):
                record['cli_diagnostic'] = error.diagnostic
            save_receipt(folder, record)
            raise SetupError('卸载未完全确认；保留收据，不重试或覆盖其他设置') from None
    return {'uninstalled': True, 'reused': False, 'requires_dsh_restart': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description='可选 DSH desktop 主脑插件安装；不启动/重启任何服务或模型')
    sub = parser.add_subparsers(dest='mode', required=True)
    prep = sub.add_parser('prepare')
    for field in ('output', 'workbench', 'python', 'executable', 'home'):
        prep.add_argument('--' + field, required=True)
    prep.add_argument('--panel-url', default=brain_transport.DEFAULT_BASE_URL)
    for mode in ('check', 'install', 'uninstall', 'reconcile-absent'):
        cmd = sub.add_parser(mode)
        cmd.add_argument('--directory', required=True)
        if mode == 'install':
            recovery = cmd.add_mutually_exclusive_group()
            recovery.add_argument('--retry-verified', action='store_true')
            recovery.add_argument('--resolve-offline-cache', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.mode == 'prepare':
            result = prepare(args.output, args.workbench, args.python, args.executable, args.home, args.panel_url)
        elif args.mode == 'install':
            result = install(args.directory, retry_verified=args.retry_verified,
                             resolve_offline_cache=args.resolve_offline_cache)
        elif args.mode == 'reconcile-absent':
            result = reconcile_absent(args.directory)
        else:
            result = globals()[args.mode](args.directory)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except SetupError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError, ValueError):
        # Only our fixed diagnostics cross this boundary; never serialize a
        # third-party exception, raw package-manager output or credential.
        print('DSH 插件操作未完成；请核对路径、目标接口、原安装收据和宿主状态。没有自动重试。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
