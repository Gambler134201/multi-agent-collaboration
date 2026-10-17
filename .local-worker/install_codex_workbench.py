"""Register the desktop workbench in the current user's Codex configuration."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

from codex_usage import find_codex

HERE = Path(__file__).resolve().parent
NAME = 'model_desk'


def config_path():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'


def servers():
    path = config_path()
    return tomllib.loads(path.read_text(encoding='utf-8')).get('mcp_servers', {}) if path.exists() else {}


def meaningful(entry):
    """Normally equal entries: the CLI drops empty lists such as `args = []`."""
    if not isinstance(entry, dict):
        return entry
    return {key: value for key, value in entry.items() if value not in (None, [], "")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    desired = {'command': str(HERE / 'venv' / 'Scripts' / 'python.exe'),
               'args': [str(HERE / 'codex_mcp_server.py')]}
    before = servers()
    current = before.get(NAME)
    matches = current is not None and all(current.get(k) == v for k, v in desired.items()) and current.get('enabled', True)
    if args.check:
        print(json.dumps({'registered': bool(matches), 'scope': 'current Windows user, all local projects',
                          'config': str(config_path()), 'server': NAME}, ensure_ascii=False))
        return 0 if matches else 1
    if current is not None and not matches:
        raise ValueError('同名 MCP 配置已存在且不同；请先核对，不会自动覆盖。')
    if not (HERE / 'codex_mcp_server.py').is_file():
        raise ValueError('模型工作台 MCP 程序尚未准备好。')
    if not matches:
        completed = subprocess.run([find_codex(), 'mcp', 'add', NAME, '--', desired['command'], *desired['args']],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if completed.returncode:
            raise ValueError('Codex 未能注册全局 MCP；未请求生成或修改协作设置。')
    after = servers()
    if any(meaningful(after.get(name)) != meaningful(value) for name, value in before.items() if name != NAME):
        raise ValueError('注册后其他 MCP 设置发生变化，请核对。')
    if not all(after.get(NAME, {}).get(k) == v for k, v in desired.items()):
        raise ValueError('注册结果与启动器配置不一致。')
    print(json.dumps({'registered': True, 'scope': 'current Windows user, all local projects',
                      'config': str(config_path()), 'server': NAME, 'other_mcp_settings_preserved': True,
                      'native_sidebar_click_verified': False}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
