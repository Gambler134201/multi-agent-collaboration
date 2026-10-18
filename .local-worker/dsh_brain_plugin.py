"""Generate the local JSON configuration for the optional DSH brain bundle.

This helper never touches a DSH profile, a project record, workflow settings or
a model provider.  It validates explicitly supplied absolute paths and creates
exactly one user-specified output file, refusing to overwrite an existing file.
The generated file contains machine paths only: project, owner, role and native
session are deliberately absent because the plugin derives them from the current
DSH Agent at runtime.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = 1
DEFAULT_PANEL_URL = "http://127.0.0.1:8765"
DEFAULT_SERVER_NAME = "model_desk_brain"
DEFAULT_OWNER_PREFIX = "dsh"
DEFAULT_TOOL_CALL_TIMEOUT_MS = 60_000
DEFAULT_STATUS_TIMEOUT_MS = 10_000
MAX_TOOL_CALL_TIMEOUT_MS = 600_000
MAX_STATUS_TIMEOUT_MS = 30_000
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
OWNER_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,63}$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
FORBIDDEN_COMPONENTS = {".model-desk"}


class ConfigError(ValueError):
    """A user-facing configuration error; no secret or account data is involved."""


def _absolute_file(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} 需要是显式绝对路径")
    text = value.strip()
    if "\x00" in text or not os.path.isabs(text):
        raise ConfigError(f"{label} 需要是显式绝对路径")
    path = Path(text)
    if not path.is_file():
        raise ConfigError(f"{label} 指向的文件不存在")
    return str(path)


def _absolute_any(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} 需要是显式绝对路径")
    text = value.strip()
    if "\x00" in text or not os.path.isabs(text):
        raise ConfigError(f"{label} 需要是显式绝对路径")
    path = Path(text)
    if not (path.is_file() or path.is_dir()):
        raise ConfigError(f"{label} 指向的路径不存在")
    return str(path)


def _panel_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("panelUrl 需要是本机回环 HTTP 地址")
    text = value.strip()
    parsed = urlsplit(text)
    if parsed.scheme != "http":
        raise ConfigError("panelUrl 只允许 http:// 回环地址")
    if parsed.username or parsed.password:
        raise ConfigError("panelUrl 不能包含账号或密码")
    try:
        host = (parsed.hostname or "").strip("[]").lower()
        port = parsed.port
    except ValueError:
        raise ConfigError("panelUrl 格式不正确") from None
    if host not in LOOPBACK_HOSTS:
        raise ConfigError("panelUrl 只允许 127.0.0.1 / localhost / ::1")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ConfigError("panelUrl 只能是不带路径、查询或片段的回环源")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("panelUrl 端口不正确")
    origin_host = f"[{host}]" if ":" in host else host
    origin = f"http://{origin_host}"
    if port is not None:
        origin += f":{port}"
    return origin


def _bounded_int(value, fallback: int, minimum: int, maximum: int, label: str) -> int:
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{label} 需要是 {minimum} 到 {maximum} 之间的整数")
    return value


def build_config(
    *,
    python: str,
    brain_mcp_server: str,
    workflow_settings: str,
    panel_url: str = DEFAULT_PANEL_URL,
    server_name: str = DEFAULT_SERVER_NAME,
    owner_prefix: str = DEFAULT_OWNER_PREFIX,
    mcp_client_path: str | None = None,
    tool_call_timeout_ms: int | None = None,
    status_timeout_ms: int | None = None,
) -> dict:
    """Return a validated, JSON-ready plugin configuration.

    The function performs no file write and no host/profile access.  It is kept
    separate from :func:`generate` so tests and callers can inspect the exact
    payload before creating the one output file.
    """
    if not isinstance(server_name, str) or not SERVER_NAME_RE.fullmatch(server_name):
        raise ConfigError("serverName 需要是 1 到 32 位字母、数字、下划线或连字符")
    if not isinstance(owner_prefix, str) or not OWNER_PREFIX_RE.fullmatch(owner_prefix):
        raise ConfigError("ownerPrefix 格式不正确")

    config = {
        "schema": SCHEMA,
        "python": _absolute_file(python, "python"),
        "brainMcpServer": _absolute_file(brain_mcp_server, "brainMcpServer"),
        "workflowSettings": _absolute_file(workflow_settings, "workflowSettings"),
        "panelUrl": _panel_url(panel_url),
        "serverName": server_name,
        "ownerPrefix": owner_prefix,
        "toolCallTimeoutMs": _bounded_int(
            tool_call_timeout_ms,
            DEFAULT_TOOL_CALL_TIMEOUT_MS,
            1000,
            MAX_TOOL_CALL_TIMEOUT_MS,
            "toolCallTimeoutMs",
        ),
        "statusTimeoutMs": _bounded_int(
            status_timeout_ms,
            DEFAULT_STATUS_TIMEOUT_MS,
            1000,
            MAX_STATUS_TIMEOUT_MS,
            "statusTimeoutMs",
        ),
    }
    if mcp_client_path is not None:
        config["mcpClientPath"] = _absolute_any(mcp_client_path, "mcpClientPath")
    return config


def _output_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("output 需要是显式绝对路径")
    text = value.strip()
    if "\x00" in text or not os.path.isabs(text):
        raise ConfigError("output 需要是显式绝对路径")
    path = Path(text)
    if any(part.lower() in FORBIDDEN_COMPONENTS for part in path.parts):
        raise ConfigError("output 不能位于工作台记录目录 .model-desk 中")
    parent = path.parent
    if not parent.is_dir():
        raise ConfigError("output 的父目录必须已经存在")
    if any(part.lower() in FORBIDDEN_COMPONENTS for part in parent.resolve(strict=True).parts):
        raise ConfigError("output 不能通过链接写入工作台记录目录 .model-desk")
    if path.exists():
        raise FileExistsError(f"输出已存在，拒绝覆盖：{path}")
    return path


def generate(
    *,
    output: str,
    python: str,
    brain_mcp_server: str,
    workflow_settings: str,
    panel_url: str = DEFAULT_PANEL_URL,
    server_name: str = DEFAULT_SERVER_NAME,
    owner_prefix: str = DEFAULT_OWNER_PREFIX,
    mcp_client_path: str | None = None,
    tool_call_timeout_ms: int | None = None,
    status_timeout_ms: int | None = None,
) -> dict:
    """Validate inputs and exclusively create ``output``; nothing else is written."""
    config = build_config(
        python=python,
        brain_mcp_server=brain_mcp_server,
        workflow_settings=workflow_settings,
        panel_url=panel_url,
        server_name=server_name,
        owner_prefix=owner_prefix,
        mcp_client_path=mcp_client_path,
        tool_call_timeout_ms=tool_call_timeout_ms,
        status_timeout_ms=status_timeout_ms,
    )
    path = _output_path(output)
    payload = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
    except FileExistsError:
        raise
    except Exception:
        # Do not leave a half-written user output behind.  Only unlink the path
        # this call just created; never touch an existing file.
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return {
        "ok": True,
        "output": str(path),
        "bytes": len(payload),
        "schema": SCHEMA,
        "next": "设置 MODEL_DESK_DSH_BRAIN_CONFIG 指向该文件，或在 DSH profile 中按插件 id 覆盖 config.configFile；本工具未修改任何 profile。",
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="生成 DSH 主脑插件本机配置；只写指定输出，不覆盖已有文件，不修改 DSH profile。",
    )
    parser.add_argument("--python", required=True, help="显式 Python 解释器绝对路径")
    parser.add_argument("--brain-mcp-server", required=True, help="显式 brain_mcp_server.py 绝对路径")
    parser.add_argument("--workflow-settings", required=True, help="显式 workflow_settings.py 绝对路径")
    parser.add_argument("--output", required=True, help="唯一要创建的配置输出绝对路径")
    parser.add_argument("--panel-url", default=DEFAULT_PANEL_URL, help="已运行工作台的回环 HTTP origin")
    parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME, help="MCP serverName")
    parser.add_argument("--owner-prefix", default=DEFAULT_OWNER_PREFIX, help="主脑拥有者前缀")
    parser.add_argument("--mcp-client-path", default=None, help="可选的已安装 dsh-mcp-client 模块路径")
    parser.add_argument(
        "--tool-call-timeout-ms",
        type=int,
        default=DEFAULT_TOOL_CALL_TIMEOUT_MS,
        help="MCP 单次工具超时（毫秒）",
    )
    parser.add_argument(
        "--status-timeout-ms",
        type=int,
        default=DEFAULT_STATUS_TIMEOUT_MS,
        help="workflow_settings --status 超时（毫秒）",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        result = generate(
            output=args.output,
            python=args.python,
            brain_mcp_server=args.brain_mcp_server,
            workflow_settings=args.workflow_settings,
            panel_url=args.panel_url,
            server_name=args.server_name,
            owner_prefix=args.owner_prefix,
            mcp_client_path=args.mcp_client_path,
            tool_call_timeout_ms=args.tool_call_timeout_ms,
            status_timeout_ms=args.status_timeout_ms,
        )
    except FileExistsError as error:
        print(str(error), file=sys.stderr)
        return 3
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError as error:
        print(f"写入输出失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
