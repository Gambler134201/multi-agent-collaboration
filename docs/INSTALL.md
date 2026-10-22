# 安装与配置（Windows）

本文只写这台机器上能实际执行的命令，示例路径都是泛用占位（`D:\Apps\...`、`D:\dev\...`），不代表原作者的目录，也不会替你决定装在哪。

当前只在 Windows 11 x64 实测：Python 3.12.14、Node 22.x、pnpm、Pi 1.3.0、本机已装的 DSH。Linux / macOS 未适配。

## 0. 放置项目

安装 DSH 依赖需要 pnpm 10.26+（本轮验证为 11.25.0）。仓库本地策略明确禁用 esbuild 的安装脚本，保留 strictDepBuilds，使用锁文件中的 Windows 平台组件；不会关闭全局构建检查。策略说明见 [pnpm 官方文档](https://github.com/pnpm/pnpm.io/blob/main/docs/settings/build.md)。

先安装 PowerShell 7，并在 `pwsh` 终端执行下列命令。Windows 自带的 PowerShell 5.1 不支持启动脚本所需参数。

把整个目录放在任意位置，例如 `D:\Apps\model-desk`。下面命令都在该目录（含 `setup.ps1` 的目录）里执行。启动脚本用 `$PSScriptRoot` 定位自身，不依赖固定盘符。

## 1. 安装依赖

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Check     # 只检查前提，不写文件
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1            # 建 venv + 装依赖
```

`setup.ps1` 默认只做四步：检查前提 → 在 `.local-worker\venv` 建虚拟环境 → 按 `requirements.txt` 装 Python 依赖 → 在 `.local-worker\agent-runtime` 装本地 Node 依赖（acpx 0.19.5）。

- 已存在的 venv 不会重建，但仍按 requirements.txt 安装/更新本目录依赖；已有 acpx 版本正确时跳过安装。
- 不会安装/升级 Python、Node、pnpm、DSH、Pi、Codex；缺什么只会明确报出来。
- 不会启动面板、不会调用模型、不会改全局 Codex 配置。
- Node 依赖只用 `pnpm install --frozen-lockfile` 按锁文件安装；缺少 Node/pnpm 会在安装前报错。只使用 Pi 时可显式加 `-SkipNodeDeps`。

## 2. 告诉工作台你的执行器在哪里

只支持下面这些显式配置，仓库里没有写死任何机器路径：

| 环境变量 | 含义 | 备注 |
| --- | --- | --- |
| `MODEL_DESK_DSH_EXE` | `DeepSeek Harness.exe` 的绝对路径 | 未设置时退化为在 PATH 里找同名可执行文件 |
| `MODEL_DESK_PI_HOME` | Pi 安装根目录 | 内部按 Pi 1.3.0 的布局解析 `npm-global/...` 与 `node/node/node.exe` |
| `MODEL_DESK_NODE_EXE` | 指定 Node 可执行文件 | 不设置时用 PATH 里的 `node` |

永久设置（当前 Windows 用户）：

```powershell
setx MODEL_DESK_DSH_EXE "D:\Apps\DeepSeek Harness\DeepSeek Harness.exe"
setx MODEL_DESK_PI_HOME "D:\Apps\pi"
# 然后重开终端，让变量生效
```

只在当前会话试一次：

```powershell
$env:MODEL_DESK_DSH_EXE = 'D:\Apps\DeepSeek Harness\DeepSeek Harness.exe'
$env:MODEL_DESK_PI_HOME = 'D:\Apps\pi'
```

验证（只读，不启动服务、不调用模型）：

```powershell
.\.local-worker\venv\Scripts\python.exe .\.local-worker\dsh_worker.py --status
.\.local-worker\venv\Scripts\python.exe .\.local-worker\pi_worker.py --status
```

`ready: false` 时 `message` 会写清楚缺什么（未设置哪个变量、检测到什么版本、缺哪个文件）。未配置或版本不符就是「不可用」：任务会被拒绝，不会改用别的引擎、别的版本或别的安装位置。当前只适配 Pi 1.3.0；DSH 的身份与能力判定和 worker 用同一个变量解析，不会出现「面板看到的」和「实际跑的」是两个安装。

## 3. 启动与退出

```powershell
.\打开模型工作台.ps1                 # 启动服务并打开浏览器（默认 http://127.0.0.1:8765）
.\打开模型工作台.ps1 -NoBrowser      # 只启动，不开浏览器
.\启动模型面板.ps1 -Port 8765        # 只启动服务
```

退出（停掉本地服务进程；面板没有 HTTP 关闭接口）：

```powershell
Stop-Process -Id (Get-Content .\.local-worker\panel-runtime.json -Raw | ConvertFrom-Json).pid
```

日志：`.local-worker\panel.stdout.log`、`.local-worker\panel.stderr.log`。

## 4. 配置你自己的模型

全新目录里没有 `models.json`，也不带任何模型、密钥或远程地址：`run_worker.py --list-models` 会返回空列表。

1. 面板 → 模型编辑界面 → 新增：服务类型、Base URL、模型 ID、Token 预算、能力说明。
2. 密钥在保存时用 Windows DPAPI（当前 Windows 用户）加密写入 `.local-worker\models.json`，界面不回显。换机器或换 Windows 用户后旧密钥无法解密，需要重填。也可以只填「环境变量名」，密钥留在你自己的环境变量里。
3. 协作开关（`enabled`）与付费授权（`paid_allowed`）默认关闭，安装不会打开；`enforce_paid_budget` / `enforce_model_call_limit` 一旦被你关掉就保持关掉，不会被自动恢复或提高。
4. 执行 Agent（DSH / Pi）在面板里选；未就绪就报错，不会自动切换。

查看当前开关与模型（只读）：

```powershell
.\.local-worker\venv\Scripts\python.exe .\.local-worker\workflow_settings.py --status
.\.local-worker\venv\Scripts\python.exe .\.local-worker\run_worker.py --list-models
```

## 5. 可选：注册全局 MCP

只在显式请求时执行；这会写入当前 Windows 用户的 Codex 配置，影响所有本地项目。

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -RegisterMcp
# 或单独操作
.\.local-worker\venv\Scripts\python.exe .\.local-worker\install_codex_workbench.py --check
.\.local-worker\venv\Scripts\python.exe .\.local-worker\install_codex_workbench.py
```

- 服务名固定 `model_desk`，命令指向本目录 venv 里的 `codex_mcp_server.py`。
- 已有同名但不同的条目：脚本会拒绝并提示，不会覆盖；其他 MCP 条目会被核对是否变化。
- 注册后**已打开的 Codex 会话不会自动出现新工具**：需要新建会话或重载客户端。原生侧栏/按钮是否可见必须由你本人确认。
- 撤销：用你的 Codex 客户端移除该 MCP（例如 `codex mcp remove model_desk`，具体命令名以你的 Codex 版本为准，本候选未验证）。

### 5.1 可选：主脑 stdio MCP（阶段 A，未默认注册）

多主脑通用入口是**独立**的 `brain_mcp_server.py`，不会替换或修改上面的 Codex 面板 MCP。`setup.ps1 -RegisterMcp` 不会注册它；需要由宿主/本机配置显式添加，且不要挂到 DSH/Pi 执行器。

先显式启动面板，再让宿主配置以下命令（路径替换成你自己的安装目录与项目绝对目录）：

```powershell
.\.local-worker\venv\Scripts\python.exe .\.local-worker\brain_mcp_server.py `
  --project "D:\dev\my-project" `
  --host dsh `
  --owner main-a `
  --role brain `
  --session-binding user_bound `
  --session-id "main-a-user-session-1" `
  --panel-url http://127.0.0.1:8765
```

- `--project` 必须是已存在的本机绝对目录；不从 cwd、安装目录或最近项目推断。
- `--role brain` 才允许变更；`executor` 只能读取且变更会被拒绝。
- `--session-id` 是用户显式绑定的专用会话标识，不是宿主原生会话核实；阶段 A 不接受 `host_verified`。省略时责任归属为 `unknown`，变更工具会被拒绝。
- 工具提供连接、说明、候选模型、按模型路由预览、快速派发、状态、交付、验收/退回和一次整改；不提供任意 HTTP、命令、文件、配置或授权接口。
- 首次必须显式调用 `brain_connect`；服务未运行只返回「不可用」，不会自动启动。面板重启后需要重新连接。
- 控制器能力只保存在 MCP 传输进程内，不写入任务、提示、日志、结果或执行器环境。普通面板/CLI 的 `actor=main` 不能验收主脑任务。
- 该入口是应用层权限边界，不是 OS 沙箱；同一 Windows 用户下的恶意脚本不在防护承诺内。

详细边界与自测范围见 `docs/MAIN_BRAIN_CORE.md`。

### 5.2 可选：DSH 原生主脑插件

在 DSH 里日常使用时，优先按 [DSH 安装与使用](DSH_USAGE.md) 安装 desktop 插件。它自动从所属原生聊天取得项目/会话身份，不需要把上面的固定项目示例配置到所有聊天。主脑插件与 DSH/Pi 执行器分开安装。

当前插件源码版本为 0.2.11，包含原生入口按需启动、首轮预检状态以及公共 PowerShell 7 解析修复。插件仍需匹配的完整工作台源码，不能只更新八文件包而保留不兼容后台；版本与分层验收范围见 [发布说明](DSH_RELEASE.md)。完整的 Agent 安装路径见 [Agent 安装指南](AGENT_INSTALL.md)，WorkBuddy 执行端配置与限制见 [执行端说明](WORKBUDDY_EXECUTOR.md)。

## 6. 可选：安装全局协作规则

规则是可选的，只影响当前 Windows 用户的规则目录（默认 `CODEX_HOME`，否则用户目录下的 `.codex`）。

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -InstallRules
# 或单独操作
.\.local-worker\venv\Scripts\python.exe .\.local-worker\install_workflow_rules.py --check
.\.local-worker\venv\Scripts\python.exe .\.local-worker\install_workflow_rules.py
.\.local-worker\venv\Scripts\python.exe .\.local-worker\install_workflow_rules.py --home "D:\Notes\my-rules"
```

- 仓库里的 `.local-worker\workflow-guides\*.md` 是**模板**，用 `@ROOT@` 表示自己的位置。安装时 `@ROOT@` 会被绑定成真实安装目录，规则正文和它链接的操作指南都不会残留未解析的 `@ROOT@`。
- 操作指南的解析副本写在规则文件旁边的 `model-desk-workflow-guides\` 目录，每次安装重写。

- 首次用户回复前，全局规则调用同一 Python 下的 `collaboration_status.py --project "受信调用项目的绝对目录"`，展示与通用主脑预检一致的状态行。该只读 CLI 不启动工作台/模型，不创建项目记录；关闭、未知项目、无候选和失败均有不同提示。规则更新后用新聊天验证，已有聊天不保证重载。跨主脑更新约定见 [CROSS_HOST_FEATURES.md](CROSS_HOST_FEATURES.md)。
- 只改动 `<!-- model-desk-workflow:start/end -->` 之间的托管块；已有非托管指令原样保留，首次写入前会备份原文件。托管标记不完整时会报错并要求你先检查文件。
- 执行器路径环境变量修改后，需要重新启动工作台和对应命令进程才能加载。
- 原生启动和系统目录选择共用 PowerShell 7 解析器，支持系统安装、PATH、已存在的本机运行时副本，或 `MODEL_DESK_PWSH` 指定的 `pwsh.exe` 绝对路径。已有缓存副本可在 Codex 关闭后使用；缓存被删除则需另行安装或指定路径。缺少依赖时不会自动安装或改用其他 shell。
- 规则改动后**已有会话不会自动加载**：新建会话或重载客户端。
- 通知/等待链路（`--notify-on-completion`）依赖本机桌面环境，请按 `.local-worker\workflow-guides\waiting.md`（安装后的副本）在你的机器上自行验证；不要假定别人机器上的验证结果适用。

## 7. 跑测试

```powershell
.\.local-worker\venv\Scripts\python.exe .\.local-worker\tests\test_opensource_bootstrap.py
.\.local-worker\venv\Scripts\python.exe .\.local-worker\tests\test_workflow_guides.py
```

测试只用操作系统临时目录和 mock，不调用真实模型、不启动服务、不注册 MCP。需要真实 DSH/Pi 的既有测试（例如预设读取、Pi 运行时）在没装或未配置时会跳过或明确失败——那是环境缺件，不是代码回退成别的引擎。

可选的 mock 浏览器检查使用真实面板前端和假 HTTP 数据，默认也不连接真实工作台。Playwright 需要在你自己的 Node 环境准备，不能加进项目生产依赖；依赖查找和完整命令见 [使用说明的离线与浏览器检查](USAGE.md#离线与浏览器检查)。

## 8. 常见问题

| 现象 | 处理 |
| --- | --- |
| `MODEL_DESK_DSH_EXE` 未设置 / 路径不存在 | 面板与 `--status` 会报「DSH 可执行文件未找到」；设置变量或把其目录加入 PATH |
| `Pi 安装不可用或版本未经验证` | 检查 `MODEL_DESK_PI_HOME` 是否指向完整 Pi 安装根目录；当前只适配 1.3.0 |
| `本地 acpx 0.19.5 依赖未安装` | 在 `.local-worker\agent-runtime` 执行 `pnpm install --frozen-lockfile` |
| `Node 未找到` | 安装 Node 到 PATH，或设置 `MODEL_DESK_NODE_EXE` |
| 模型列表为空 / 「请选择一个已配置的执行模型」 | 还没配模型：去面板模型编辑界面新增 |
| 端口 8765 被占用 | 先确认占用进程；MCP 集成固定使用 8765，不要把任意占用进程直接停止 |
| 全局规则里看到 `@ROOT@` | 说明规则不是由 `install_workflow_rules.py` 安装的；重跑安装命令，别手工复制模板 |
| 换机器后密钥失效 | DPAPI 绑定当前 Windows 用户，需重新录入密钥 |

## 9. 回退

```powershell
Remove-Item -Recurse -Force .\.local-worker\venv, .\.local-worker\agent-runtime\node_modules
```

规则托管块按标记删除（或删掉安装时生成的备份之外的托管块），MCP 用你的 Codex 客户端移除。任务与项目记录在各项目的 `.model-desk\` 里，删除它们会丢掉历史，请自行决定是否保留。
