"""Where the external runtimes live: one portable, mockable place.

Nothing here knows any particular machine.  There is no user name, no drive
letter and no editor cache directory baked in.  Every external runtime is
either declared by an explicit environment variable (``MODEL_DESK_DSH_EXE``,
``MODEL_DESK_PI_HOME``, ``MODEL_DESK_NODE_EXE``) or found through a verifiable
PATH lookup.  When it is missing the caller gets ``None`` / an
:data:`UNSET` marker, so the workbench reports the engine as unavailable and
refuses to run instead of silently substituting another engine, version or
install location.

DSH and Pi are the user's own applications: this repository never bundles,
downloads or reinstalls them.  Only the versions this build was actually
verified against are accepted (see :data:`PI_VERSION` / :data:`ACPX_VERSION`).
"""
from pathlib import Path
import json
import os
import shutil

HERE = Path(__file__).resolve().parent
AGENT_RUNTIME = HERE / 'agent-runtime'

# --- user-configured locations (documented in docs/INSTALL.md) -------------
DSH_EXE_ENV = 'MODEL_DESK_DSH_EXE'
PI_HOME_ENV = 'MODEL_DESK_PI_HOME'
NODE_EXE_ENV = 'MODEL_DESK_NODE_EXE'

# --- what this build was actually verified against ------------------------
DSH_EXECUTABLE_NAME = 'DeepSeek Harness.exe'
DSH_CLI = 'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'
ACPX_VERSION = '0.19.5'                      # agent-runtime/package.json + pnpm-lock.yaml
ACPX_RUNTIME = 'node_modules/acpx/dist/runtime.js'
PI_PACKAGE = '@earendil-works/pi-coding-agent'
PI_VERSION = '1.3.0'
PI_PACKAGE_DIR = 'npm-global/node_modules/' + PI_PACKAGE
PI_CLI = PI_PACKAGE_DIR + '/dist/bundle/cli.js'
PI_NODE = 'node/node/node.exe'

# Stand-in for "the user has not configured this runtime".  It is a relative
# path that never resolves to a real file, and its name is readable in the
# availability report instead of a fabricated drive letter.
UNSET = Path('MODEL-DESK-NOT-CONFIGURED')


def declared(name):
    """Absolute :class:`Path` from an environment variable, else ``None``.

    A relative value is rejected rather than interpreted against some current
    directory, because a mistyped path must surface as "not configured".
    """
    raw = str(os.environ.get(name) or '').strip().strip('"')
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else None


def which(name):
    """PATH lookup that always yields a :class:`Path` (``shutil.which`` gives str)."""
    found = shutil.which(name)
    return Path(found) if found else None


def dsh_executable():
    """The user's DSH executable, or ``None`` when it is not configured."""
    return declared(DSH_EXE_ENV) or which(DSH_EXECUTABLE_NAME)


def dsh_cli(executable=None):
    """The ACP entrypoint inside the installed DSH package, or ``None``."""
    exe = Path(executable) if executable else dsh_executable()
    return exe.parent / DSH_CLI if exe else None


def node_executable():
    """Node used to drive the local acpx/Pi runners: env var, then PATH."""
    return declared(NODE_EXE_ENV) or which('node')


def acpx_runtime(runtime_dir=None):
    """The pinned acpx runtime inside this installation (never installed here)."""
    return Path(runtime_dir or AGENT_RUNTIME) / ACPX_RUNTIME


def acpx_version(runtime_dir=None):
    """Installed acpx version, or ``''`` when the local dependency is absent."""
    try:
        manifest = json.loads((Path(runtime_dir or AGENT_RUNTIME) / 'node_modules/acpx/package.json')
                              .read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    return str(manifest.get('version') or '')


def pi_home():
    """Root of the user's own Pi installation, or ``None`` when unset."""
    return declared(PI_HOME_ENV)


def pi_package(home=None):
    home = Path(home) if home else pi_home()
    return home / PI_PACKAGE_DIR if home else None


def pi_cli(home=None):
    home = Path(home) if home else pi_home()
    return home / PI_CLI if home else None


def pi_node(home=None):
    home = Path(home) if home else pi_home()
    return home / PI_NODE if home else None


def pi_release(package=None):
    """``(package name, version)`` of a Pi package directory.

    Defaults to the configured install.  An unreadable or missing install
    reports ``('', '')``: the caller then reports the engine as unverified
    instead of inventing a version.
    """
    package = Path(package) if package else pi_package()
    if package is None:
        return '', ''
    try:
        manifest = json.loads((package / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return '', ''
    return str(manifest.get('name') or ''), str(manifest.get('version') or '')
