# 多agent协作

本地 Windows 工作台：把「计划、派发、执行、等待、独立验收」拆给多个 agent，由主控（已接入 Codex、DSH 原生聊天）保留需求确认、风险决策和独立验收，把调查、实现、测试交给执行 agent（DSH 或 Pi）。

服务、执行器和记录都在本机运行：面板只监听 `127.0.0.1`，模型密钥用 Windows DPAPI 加密后存在本地，任务记录写在各自项目的 `.model-desk/` 目录里。

项目源码采用 [MIT 许可证](LICENSE)，维护者为 [Gambler134201](https://github.com/Gambler134201)。

最近更新见 [更新记录](CHANGELOG.md)：多主脑核心、DSH 原生工作台与余额同步、单任务/整批完成通知，以及每模型默认执行 Agent。

开发流程见 [Git 工作流](docs/GIT_WORKFLOW.md)，日常操作与测试见 [使用说明](docs/USAGE.md)，DSH 用户见 [DSH 安装与使用](docs/DSH_USAGE.md)。源码整合事项见 [源码整合清单](docs/SOURCE_RECONCILIATION.md)，主脑权限边界见 [多主脑通用接口](docs/MAIN_BRAIN_CORE.md)。功能路线见 [后续待更新功能](后续待更新功能/README.md)。

## 你会得到什么

- 本地面板（`http://127.0.0.1:8765`）：模型与密钥、每个模型自己的默认执行 Agent、任务派发与状态、精简的目标进度与只读历史目标、用量与账单核对、项目与阶段记录。
- 两个执行 Agent 适配：**DSH（DeepSeek Harness）** 走 ACP/acpx，**Pi Agent** 走 Pi RPC。每个模型可保存自己的 `default_agent`（DSH / Pi / 跟随全局）；新任务按「本次显式指定 > 模型默认 > 全局默认」解析，自动路由对每个最终候选分别解析。已有任务、续接、整改与会话复用保留原 Agent。选中的 Agent 不可用时直接报错，不会改用另一个。
- 任务记录、日志、验收与阶段交接写在每个项目自己的 `.model-desk/records.db` 与 `.model-desk/runs/<id>/task.json`；阶段交接带文件哈希校验。
- 可选的 Codex MCP（全局服务名 `model_desk`）与可选的全局协作规则，两者都只在显式命令下安装。
- 通用主脑 stdio MCP（`brain_mcp_server.py`）：独立入口，不替换 Codex 面板 MCP，默认不注册；提供候选、路由、派发、状态/交付、验收与一次整改，不自动启动服务。DSH 原生插件按当前项目/聊天绑定该接口，提供右侧工作台、登录余额同步与完成通知。已实测范围及 0.2.7 发布候选说明见 [DSH 使用指南](docs/DSH_USAGE.md)、[发布说明](docs/DSH_RELEASE.md)。
- 记录写入防护：DSH 与 Pi 侧都带工具参数防护（应用层防护，不是操作系统沙箱）。

## 支持边界（先读这段）

- **只支持 Windows**（当前开发验证在 Windows 11 x64）。Linux / macOS 未适配、未测试。
- Python 只在 **3.12** 实测过（代码要求 3.11+，因为使用 `tomllib`）。
- Node 只在 **22.x** 实测过；本地 acpx 固定 **0.19.5**（`pnpm-lock.yaml`）。
- 执行 Agent 只适配 **Pi 1.3.0** 与本机实测过的 DSH 安装。Pi 包名或版本不符会被判定为「不可用」；DSH 的能力验证与实际安装身份绑定，不会自动换引擎。
- 不承诺任何固定的额度节省，也不承诺输出质量等同 GPT 或其他模型。用量与费用取决于你的模型、提示和任务，面板里的数字是记账与核对，不是保证。
- 「原生入口/按钮/侧栏可见性」需要使用者本人在自己的界面里确认，本仓库不做保证。
- 通知/等待链路（`--notify-on-completion`）依赖本机桌面环境，**新用户需要按 `docs/INSTALL.md` 自行验证**；原作者机器上的验证不能当作通用保证。
- 第三方应用（DSH、Pi、Codex、Node、pnpm）都不随仓库分发，需要你自己安装并保留自己的许可与账号。

## 前提

| 项目 | 要求 | 说明 |
| --- | --- | --- |
| 系统 | Windows 10/11 64 位 | 只在 Windows 验证 |
| PowerShell | 7+（pwsh） | 启动脚本依赖此版本 |
| Python | 3.11+（实测 3.12.14） | 用于 venv，不改动全局 Python |
| Node | 实测 22.x | 驱动本地 acpx 与 Pi 运行器 |
| pnpm | 10.26+，DSH 依赖安装时必需 | 安装 `.local-worker/agent-runtime` 的 acpx 0.19.5 |
| DSH | 用户自装 | 用 `MODEL_DESK_DSH_EXE` 指向 `DeepSeek Harness.exe` |
| Pi | 用户自装，1.3.0 | 用 `MODEL_DESK_PI_HOME` 指向 Pi 安装根目录 |
| Codex（可选） | 使用 MCP / 全局规则时需要 | 只影响当前 Windows 用户 |

仓库内不写死任何作者机器路径：DSH、Pi、Node 的位置只来自环境变量或 PATH 查找（见 `docs/INSTALL.md`）。缺失或版本不符时明确报「不可用」。

## 安装

```powershell
# 在项目根目录（含 setup.ps1 的目录）
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Check      # 只看前提，不写任何东西
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1             # 建 venv + 装依赖
```

默认只做两件事：在 `.local-worker\venv` 建虚拟环境、按 `requirements.txt` 装 Python 依赖，并在 `.local-worker\agent-runtime` 装本地 Node 依赖（acpx）。它不会启动服务、不会调用模型、不会装 DSH/Pi、不会改全局 Codex 配置。

可选动作必须显式请求：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -RegisterMcp   # 注册全局 model_desk MCP
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -InstallRules  # 安装可选全局协作规则
```

## 启动

```powershell
.\打开模型工作台.ps1          # 启动并打开浏览器
.\打开模型工作台.ps1 -NoBrowser
.\启动模型面板.ps1            # 只启动服务（默认 8765）
```

安全退出（停止本地服务进程）：

```powershell
Stop-Process -Id (Get-Content .\.local-worker\panel-runtime.json -Raw | ConvertFrom-Json).pid
```

## 配置模型

全新目录里没有 `models.json`，也不会附带任何模型、密钥或远程地址：初始化为空列表。

1. 打开面板 → 模型编辑界面 → 新增模型，填服务类型、Base URL、模型 ID、Token 预算。
2. 密钥写在保存时由 Windows DPAPI 加密，界面不回显；也可以只填环境变量名，密钥留在你自己的环境变量里。
3. 「协作开关」和「付费授权」默认关闭，打包不会打开；需要你显式开启。付费执行需要显式允许，额度与调用上限的开关按你的设置保留。
4. 每个模型可保存自己的默认执行 Agent：模型编辑器里的「默认执行 Agent」，或协作栏的「模型 / 执行 Agent / 保存」快捷入口。优先级是本次显式指定 > 模型默认 > 全局默认；全局回退仍保留在后台兼容旧配置，面板不再提供容易误解的全局选择器。
5. 「各执行 Agent 参数设置」只调整 DSH/Pi 的适配器参数，不决定谁执行。选中的 Agent 未就绪时任务会被拒绝，不会自动改用另一个；Pi 不加载 DSH 预设。
6. 首页只显示进行中目标的一行折叠进度；已完成 / 已取消目标在「任务记录管理」里只读查看，保留原有删除预览与二次确认。读取失败、HTTP 错误或返回结构异常会明确显示为未知，不会伪装成「没有目标」或「全部完成」。

## 最小使用步骤

1. 设好 `MODEL_DESK_DSH_EXE` 或 `MODEL_DESK_PI_HOME`（见 `docs/INSTALL.md`）。
2. `.\打开模型工作台.ps1`，在面板里添加模型并做一次连接测试。
3. 派发一个范围明确的任务：

```powershell
.\local-worker.ps1 -Project "D:\你的\项目" -Files src\a.py src\b.py `
  -TaskFile .\task.txt -Route auto -Kind edit
```

4. 执行结束后**自己复核**：查看实际改动，跑最小有效检查，再用 `review_task.py` 记录验收结论。执行器报告 `completed` 不等于验收通过。

命令行细节见 `.local-worker/workflow-guides/`（模板里的 `@ROOT@` 会在安装全局规则时绑定成你的实际安装目录）。

## 目录与内部名称

展示名称是「多agent协作」，但内部标识保持不变以兼容既有实现：MCP 服务名 `model_desk`、UI 资源 `ui://model-desk/workbench-v1`、目录 `.local-worker/`、项目记录目录 `.model-desk/`。

```
setup.ps1                     安装脚本（默认只装本目录依赖）
requirements.txt              Python 直接依赖（固定版本）
启动模型面板.ps1 / 打开模型工作台.ps1 / local-worker.ps1   启动与派发脚本
docs/INSTALL.md               安装、环境变量、可选 MCP/规则、退出与故障排查
docs/USAGE.md                 每模型默认 Agent、目标进度、离线与浏览器检查
.local-worker/                服务、worker、执行器适配、单元测试、workflow-guides 模板
.local-worker/agent-runtime/  本地 Node 侧运行器与 package.json / pnpm-lock.yaml
```

运行时生成、不入库的内容（见 `.gitignore`）：`.local-worker/models.json`、`settings.json`、`workflow.json`、账本、`runs/`、`venv/`、`node_modules/`、各项目 `.model-desk/`。

## 测试

```powershell
.\.local-worker\venv\Scripts\python.exe .\.local-worker\tests\test_opensource_bootstrap.py
```

测试只用系统临时目录和 mock，不调用真实模型、不启动服务。需要真实 DSH/Pi 的既有测试会在没装/没配置时自动跳过或明确失败。

可选的 mock 浏览器检查使用真实面板前端与假 HTTP 数据，默认不连接真实工作台或模型服务。先在你自己的 Node 环境准备 Playwright（不加入项目生产依赖），再按 [使用说明](docs/USAGE.md#离线与浏览器检查) 设置依赖查找路径；完整命令见同一节。

## 许可证与后续范围

本项目采用 [MIT 许可证](LICENSE)。第三方依赖和外部应用的许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。

Linux / macOS 适配、任意 DSH/Pi 版本兼容、Aider 路径的开箱即用，均不在当前范围内。
