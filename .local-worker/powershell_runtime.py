"""Resolve an existing native PowerShell 7; no installs or process startup.

Hosts need not share PATH or be running together. The executable must already
exist on this machine; finding a cached runtime does not start its owning app.
The launch scripts themselves enforce #Requires -Version 7.0.
"""
import os
from pathlib import Path
import shutil


def _native_path(value):
    path = Path(value)
    if (path.is_absolute() and path.name.lower() in {'pwsh', 'pwsh.exe'}
            and path.is_file()):
        return str(path.resolve())
    return None


def find_pwsh():
    override = os.environ.get('MODEL_DESK_PWSH')
    if override:
        result = _native_path(override)
        if not result:
            raise ValueError('MODEL_DESK_PWSH 必须指向已存在的原生 pwsh.exe 绝对路径；未改用其他程序')
        return result
    for variable in ('ProgramW6432', 'ProgramFiles'):
        base = os.environ.get(variable)
        if base:
            result = _native_path(Path(base) / 'PowerShell' / '7' / 'pwsh.exe')
            if result:
                return result
    for name in ('pwsh.exe', 'pwsh'):
        found = shutil.which(name)
        if found:
            result = _native_path(found)
            if result:
                return result
    home = os.environ.get('USERPROFILE') or os.environ.get('HOME')
    if home:
        for relative in (
            'AppData/Local/Programs/PowerShell/7/pwsh.exe',
            '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe',
        ):
            result = _native_path(Path(home) / relative)
            if result:
                return result
    raise ValueError('未找到原生 PowerShell 7；请安装或用 MODEL_DESK_PWSH 指定已有 pwsh.exe。未改用其他 shell')
