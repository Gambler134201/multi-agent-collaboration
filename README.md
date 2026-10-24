# 多 Agent 协作工作台

**让主脑负责判断，让合适的执行端完成任务。**

写代码、反复测试、整理文件，往往会消耗大量主脑额度。换用本地模型或低成本模型后，又需要人来回补充要求、追踪进度、检查结果。

这个项目把这套协作过程放进同一个工作台：由 Codex 或 DSH 主脑理解需求、判断风险、选择执行端并独立验收；由 Pi、DSH、WorkBuddy 或 ZCode 完成具体的调查、实现和测试。目标是让高价值模型的额度集中用于关键判断，同时让执行模型在明确目标、指导与验收下交付。

## 用你已有的模型和账户，组织一套协作流程

| 角色 | 当前支持 | 负责什么 |
| --- | --- | --- |
| 主脑 | Codex、DSH | 理解需求、拆分任务、派发、独立验收与整改判断 |
| 执行端 | Pi Agent、DeepSeek Harness、WorkBuddy、ZCode | 调查、设计、实现、测试与修正 |
| 模型来源 | 自建/本地服务、付费 API、WorkBuddy / ZCode 登录账户内置模型 | 根据任务和账户条件选择，保留各自的费用与授权边界 |

**本地模型、低成本 API、平台赠送或限免权益，都可以成为协作资源。** 可用权益取决于平台、模型和你自己的账户；工作台不提供额度，也不保证固定节省比例。

## 当前可以做什么

- **按执行端管理。** Pi、DSH、WorkBuddy、ZCode 各有独立开关，卡片展示当前模型与对应余额来源；设置弹窗集中管理整个执行端的能力与成本预设，以及各模型连接。
- **使用 WorkBuddy 内置模型。** 读取当前安装可用的原生模型列表并明确选择，使用已登录账户的模型与积分；不静默切到 Auto、其他模型或 API Key。
- **使用 ZCode Start Plan。** ZCode 3.16.3 支持任务前自动连接、明确模型与思考强度、原生套餐额度刷新、运行中工具进度和带交接的新会话续接；GLM-5.3-Flash / max 的文件、搜索、读图、终端及续接真实任务已独立验收。无需修改安装包；版本、工具和账户计量边界见 [ZCode 指南](docs/ZCODE_EXECUTOR.md)。
- **让执行端真正动手。** 支持文件编辑、检索和运行测试；WorkBuddy 新任务已接入原生开发工具链。执行器自测之后，主脑仍需独立检查。
- **等待期间释放主脑。** 在已验证的本机桌面通知链路中，主脑订阅任务后结束当前回合，任务结束再回到原聊天验收，减少重复轮询。
- **让协作状态与结果可见。** 首轮回复展示真实预检信号；每个项目保留自己的任务、执行、验收和整改记录，区分“执行结束”和“验收通过”。

## 把安装交给你的 Agent

将仓库链接和 [Agent 下载安装指南](docs/AGENT_INSTALL.md) 发给你的 Agent，即可按本机环境检查依赖、选择主脑与执行端、安装并逐层验收。

[详细安装](docs/INSTALL.md) · [DSH 主脑](docs/DSH_USAGE.md) · [WorkBuddy 执行端](docs/WORKBUDDY_EXECUTOR.md) · [ZCode 执行端](docs/ZCODE_EXECUTOR.md) · [更新记录](CHANGELOG.md)

本地面板只监听 `127.0.0.1`；配置在本机管理，API 密钥用 Windows DPAPI 加密，任务记录写入各项目自己的 `.model-desk/`。调用云模型时，任务内容仍按所选服务发送。

项目采用 [MIT 许可证](LICENSE)，由 [Gambler134201](https://github.com/Gambler134201) 维护。开发流程见 [Git 工作流](docs/GIT_WORKFLOW.md)，主脑接口见 [通用核心](docs/MAIN_BRAIN_CORE.md)，功能路线见 [后续待更新功能](后续待更新功能/README.md)。

## 支持边界（先读这段）

- **只支持 Windows**（当前开发验证在 Windows 11 x64）。Linux / macOS 未适配、未测试。
- Python 只在 **3.12** 实测过（代码要求 3.11+，因为使用 `tomllib`）。
- Node 只在 **22.x** 实测过；本地 acpx 固定 **0.19.5**（`pnpm-lock.yaml`）。
- 执行端已适配 **Pi 1.3.0**、经适配的 DSH 安装及 **WorkBuddy 5.8.2 / 5.8.3**、**ZCode 3.16.3**。WorkBuddy 5.8.3 已有真实任务验收，其他安装需自行验证。Pi 包名或版本不符会被判定为「不可用」；DSH 的能力验证与实际安装身份绑定，不会自动换引擎。
- 不承诺任何固定的额度节省，也不承诺输出质量等同 GPT 或其他模型。用量与费用取决于你的模型、提示和任务，面板里的数字是记账与核对，不是保证。
- 「原生入口/按钮/侧栏可见性」需要使用者本人在自己的界面里确认，本仓库不做保证。
- 通知/等待链路（`--notify-on-completion`）依赖本机桌面环境，**新用户需要按 `docs/INSTALL.md` 自行验证**；原作者机器上的验证不能当作通用保证。
- 当前队列仍为串行，多模型并行尚未上线。WorkBuddy 主脑、Skill/外部连接器/嵌套代理/媒体工具尚未接入，积分余额未同步；DSH 原生 GUI 发起 WorkBuddy 尚未实测。
- ZCode 适配依赖指定版本的非公开桌面服务与本机调试端口；文件、搜索、读图、公开网页与终端使用受控 MCP 工具，不等于全部原生工具。套餐额度可按模型刷新，逐任务费用与底层调用次数仍未知，强制金额/次数上限开启时拒绝执行；DSH 原生入口派发 ZCode 尚未实测。
- 关闭宿主、重启、系统休眠及跨工作区后的自动唤醒没有完整保证。
- 第三方应用（DSH、Pi、Codex、WorkBuddy、ZCode、Node、pnpm）都不随仓库分发，需要你自己安装并保留自己的许可与账号。

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
| WorkBuddy（可选） | 用户自装，5.8.2 / 5.8.3 | 执行端卡片配置安装目录与当前用户 profile；需要已登录的桌面宿主 |
| ZCode（可选） | 用户自装，3.16.3 | 已登录桌面宿主，通过卡片打开本地桥接；见 ZCode 指南 |
| Codex（可选） | 使用 MCP / 全局规则时需要 | 只影响当前 Windows 用户 |

执行器使用你自己的安装路径：DSH、Pi、Node 通过环境变量或 PATH 配置，WorkBuddy 在卡片中保存安装与用户配置目录，PowerShell 7 支持显式路径和本机发现（见 [安装指南](docs/AGENT_INSTALL.md)）。缺失或版本不符时明确报「不可用」。

## 安装

```powershell
# 在项目根目录（含 setup.ps1 的目录）
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Check      # 只看前提，不写任何东西
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1             # 建 venv + 装依赖
```

默认只安装本目录依赖：在 `.local-worker\venv` 建虚拟环境、按 `requirements.txt` 装 Python 依赖，并在 `.local-worker\agent-runtime` 装本地 Node 依赖（acpx）。它不会启动服务、不会调用模型、不会装 DSH/Pi/WorkBuddy/ZCode、不会改全局 Codex 配置。

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

1. 打开面板 → 所选执行端卡片 →“模型与参数设置”→“模型与连接”，添加自己的 API / 本地模型，填服务类型、Base URL、模型 ID、Token 预算。WorkBuddy 按下方说明读取原生模型。
2. 密钥写在保存时由 Windows DPAPI 加密，界面不回显；也可以只填环境变量名，密钥留在你自己的环境变量里。
3. 「协作开关」和「付费授权」默认关闭，打包不会打开；需要你显式开启。付费执行需要显式允许，额度与调用上限的开关按你的设置保留。
4. 各执行端卡片独立启用/禁用；禁用只阻止新任务，不取消运行中任务。模型按其保存的执行 Agent 路由，优先级仍是本次显式指定 > 模型默认 > 全局兼容回退；已有任务和整改保留原模型与执行 Agent。
5. 设置弹窗首页的任务类型、能力说明、成本定位和复杂度上限应用于整个执行端；“模型与连接”页签管理各模型与运行参数。能力声明不是实测评分；预设不改变实际价格、预算或授权。选中的 Agent 未就绪时任务会被拒绝，不会自动改用另一个；Pi 不加载 DSH 预设。
6. 首页只显示进行中目标的一行折叠进度；已完成 / 已取消目标在「任务记录管理」里只读查看，保留原有删除预览与二次确认。读取失败、HTTP 错误或返回结构异常会明确显示为未知，不会伪装成「没有目标」或「全部完成」。

7. WorkBuddy 卡片保存自己的安装目录和当前用户配置目录，读取原生模型列表后保存明确模型；使用已登录账户的原生模型，积分/费用未知时保持未知。不会因调用失败切到 Auto 或 API Key。配置、版本与工具边界见 [WorkBuddy 指南](docs/WORKBUDDY_EXECUTOR.md)。

## 最小使用步骤

1. 按 [Agent 安装指南](docs/AGENT_INSTALL.md) 配置所选执行端；Pi/DSH 设好对应路径，WorkBuddy 保存自己的安装与 profile。
2. `.\打开模型工作台.ps1`，确认项目目录与执行端状态。添加 API / 本地模型，或读取 WorkBuddy 内置模型；真实连接测试可能调用模型，按自己的授权执行。
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
docs/AGENT_INSTALL.md         可直接交给自己 Agent 的下载安装与分层验收指南
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
