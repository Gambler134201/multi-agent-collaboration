# 给你的 Agent：下载、安装与首次验收指南

更新日期：2026-10-22。仓库：[Gambler134201/multi-agent-collaboration](https://github.com/Gambler134201/multi-agent-collaboration)。

这是一份交给使用者自己的 Agent 阅读并执行的安装说明。安装位置、账号、模型和授权来自使用者；不要照抄维护者机器的路径或把维护者的验收当成当前机器已通过。

## 直接复制给 Agent 的指令

```text
请帮我安装这个项目：
https://github.com/Gambler134201/multi-agent-collaboration

先阅读仓库 AGENTS.md、docs/AGENT_INSTALL.md 和 docs/INSTALL.md，再按实际代码确认命令。
先检查本机环境并询问我希望使用的主脑（Codex 或 DSH）、执行端（Pi、DSH、WorkBuddy，可多选）及安装目录；已有明确答案就沿用。
在我选择的目录下载源码，记录提交号，使用 PowerShell 7 和项目自己的 Python venv 安装依赖。
不要覆盖已有目录、模型配置、账号、MCP 条目或安装收据；不要读取或输出认证文件、Token 和密钥。
按我选择的主脑和执行端完成配置。只安装我需要的集成；不要把执行端装成主脑，不要默认安装所有第三方应用。
真实模型调用、付费授权和范围调整按我的明确选择执行。没有得到授权时，先完成离线检查，报告还需要我做什么；不要用真实推理替代安装回归。
最后分开报告：安装完成、服务可达、主脑已加载、执行端就绪、真实任务验收。每项用实际证据说明，未知项保持未知。
```

## 1. 先确定使用方式

| 角色 | 当前适配 | 需要使用者提供 |
| --- | --- | --- |
| 主脑 | Codex；DSH 原生主脑插件 | 已安装的宿主、选定项目目录；DSH 还需自己的 profile 和插件安装资料目录 |
| Pi 执行端 | 已适配 Pi 1.3.0，连接使用者自己的 API 或本地服务 | 完整 Pi 安装目录、模型连接；本地模型需要独立推理服务 |
| DSH 执行端 | 经适配的 DSH 安装，ACP/acpx 通道 | DSH 可执行文件、工作台中的模型连接 |
| WorkBuddy 执行端 | 登录账户内置模型；当前适配 5.8.2 / 5.8.3 | 完整安装目录、当前用户配置目录、已登录并可用的桌面宿主 |

WorkBuddy **作为主脑尚未完成适配**。DSH 登录余额、DeepSeek API 余额和 WorkBuddy 积分属于不同账户来源，不互通。WorkBuddy 积分目前显示未同步，未知费用不能记成零。

建议首次只配一个执行端，通过最小任务后再增加。当前仍是串行执行队列；整批完成通知不等于多模型并行。

## 2. 下载与环境检查

当前支持 Windows；维护者主要在 Windows 11 x64、Python 3.12、Node 22.x、PowerShell 7 上验证。代码要求 Python 3.11+。默认 Node 依赖安装要求 pnpm 10.26+；外部应用不随仓库分发。

先检查 `git`、`pwsh`、可运行的 Python、`node` 和 `pnpm`。WindowsApps 的 Python 占位别名不算可运行解释器。缺依赖时说明缺项，遵照使用者选择安装，不擅自升级已有软件或放宽版本校验。

下面以使用者已选定的 `D:\Apps` 为例；目标目录必须不存在，父目录必须已存在。在 **PowerShell 7** 中执行：

```powershell
$installParent = 'D:\Apps' # 替换成使用者选定的目录
$installRoot = Join-Path $installParent 'multi-agent-collaboration'
if (-not (Test-Path -LiteralPath $installParent -PathType Container)) { throw '请先选择已存在的父目录' }
if (Test-Path -LiteralPath $installRoot) { throw '目标已存在，先核对是否是已有安装，不覆盖' }
git clone --branch main https://github.com/Gambler134201/multi-agent-collaboration.git $installRoot
if ($LASTEXITCODE -ne 0) { throw '下载失败，停止后续步骤' }
Set-Location -LiteralPath $installRoot
git rev-parse HEAD
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Check
```

也可在 GitHub 下载 main 的 ZIP 并完整解压，确认根目录有 `setup.ps1`、`.local-worker` 和 `docs`。ZIP 安装以下载来源/日期与文件清单记录版本；不要为了安装按钮初始化 Git。`SOURCE-MANIFEST.json` 提供文件哈希供核对，不能代替来源信任或签名。

`setup.ps1 -Check` 是前提报告，不代表所需执行端均已可用。Agent 应读取报告并解决实际选用路径的缺项。

## 3. 安装项目依赖

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，不继续启动' }
```

可用 `-Python '实际 python.exe 的绝对路径'` 显式指定解释器。依赖只装在本目录的 venv 和 agent-runtime，不默认注册全局 MCP、安装全局规则、启动工作台或调用模型。只使用 Pi、明确不需要 DSH/acpx 时可选 `-SkipNodeDeps`；后续启用 DSH 时补装锁定依赖。

离线安装回归：

```powershell
$workbenchPython = Join-Path $installRoot '.local-worker\venv\Scripts\python.exe'
& $workbenchPython -B .\.local-worker\tests\test_opensource_bootstrap.py
& $workbenchPython -B .\.local-worker\tests\test_workflow_guides.py
```

这些检查通过只说明对应安装与规则逻辑通过；不会证明当前用户的模型账户可用。

## 4. 配置实际使用的执行端

### Pi / DSH

先核对实际安装，再在当前会话设置选用的变量。不要填不存在的示例路径，也不要把另一个 Agent 的目录当作该执行端：

```powershell
$env:MODEL_DESK_PI_HOME = 'D:\Apps\pi' # 仅选 Pi 时配置
$env:MODEL_DESK_DSH_EXE = 'D:\Apps\DSH\DeepSeek Harness.exe' # 仅选 DSH 时配置
# 必要时显式指定现有 Node 和 PowerShell 7：
# $env:MODEL_DESK_NODE_EXE = 'D:\Apps\node\node.exe'
# $env:MODEL_DESK_PWSH = 'C:\Program Files\PowerShell\7\pwsh.exe'
```

按需运行 `.local-worker/pi_worker.py --status` 或 `.local-worker/dsh_worker.py --status`。如果需要宿主重启后仍生效，按使用者同意的范围保存用户环境变量，并说明当前已运行进程不会自动继承；不重写系统 PATH。

在工作台“执行端 → 模型与参数设置”中添加使用者自己的 API / 本地模型，确认默认执行 Agent 和启用状态。弹窗首页的能力与成本预设应用于**整个执行端**；API 连接、模型上下文和实际单价仍各自保存。

### WorkBuddy

使用者自行登录；Agent 不读取认证文件或复制其他用户的 profile。

1. 核对安装版本；在 WorkBuddy 卡片保存实际安装目录和当前用户配置目录，也可显式提供 `MODEL_DESK_WORKBUDDY_HOME` / `MODEL_DESK_WORKBUDDY_PROFILE`。
2. WorkBuddy 保持打开。读取原生模型列表，选择使用者指定的**明确模型**并保存；不要默认选 Auto，不因调用失败改用另一个模型或 API Key。
3. 列表刷新会创建并关闭本工具自己的无提示词元数据会话，不等同真实模型执行验收。若原生通道尚未建立，可由使用者在桌面完成一轮普通对话后重新检查；这轮对话可能消耗积分。
4. 卡片启用与模型范围、付费授权、预算约束共同生效。原生调用无法落实工作台金额/请求硬上限时会拒绝，必须向使用者说明；不得偷偷关闭限制来“修好”。

新编辑任务接入 12 项原生开发/读取工具，含文件编辑、项目检索、终端测试及联网读取。真实验证覆盖 Bash、Write、PowerShell、Read、Glob、Grep；其余工具没有逐项真实验证。Skill、外部连接器、嵌套代理、媒体工具、后台任务及旧聊天续接尚未接入。详见 [WorkBuddy 执行端说明](WORKBUDDY_EXECUTOR.md)。

## 5. 启动工作台与接入主脑

在用户已经请求启动的前提下运行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\打开模型工作台.ps1 -NoBrowser
```

检查回环服务可达，再在宿主打开绑定使用者**实际业务项目绝对目录**的页面：`http://127.0.0.1:8765/?project=<URL编码后的绝对目录>&host=codex`。DSH 使用其原生入口。不要把工作台安装目录自动当作每个任务的业务项目。端口被占用时核对进程与安装归属，不停止不明进程。

### Codex 主脑

使用者选择此集成后执行：

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -RegisterMcp -InstallRules
```

这会修改当前用户的 MCP 和托管规则；同名冲突应停止核对，不覆盖其他条目。之后由使用者新建聊天或重载客户端，核对 `model_desk` 与首轮协作状态信号。MCP 可见不等于规则已加载。协作开关仍按使用者设置，不因安装自动开启。

### DSH 主脑

完整步骤见 [DSH 使用指南](DSH_USAGE.md)。当前插件源码版本 **0.2.11**，必须匹配完整工作台源码，不能把单独插件包替代工作台。

使用者先初始化自己的 DSH profile；等任务结束并完全退出 DSH 后，在一个**新的**安装资料目录执行 `dsh_brain_desktop.py prepare`，再显式启动匹配后台，执行 `check → install`。所需绝对路径和全部参数见 DSH 指南。保留本机生成的 plan/config/receipt，用于升级和卸载；这些文件不要上传。

由使用者重新打开 DSH，在原生项目聊天从右侧打开工作台。此入口能按需启动后台；仍要求安装依赖真实存在。安装后的冷启动、余额和通知需要在自己的机器验证，不能据维护者结果直接打勾。

## 6. 首次验收：逐层确认

| 层级 | 必须看到的证据 | 不足以代替的证据 |
| --- | --- | --- |
| 安装 | 依赖安装成功；安装回归通过 | 仅解压成功 |
| 工作台 | 正确安装的回环服务可达；页面项目正确 | 别的安装占用 8765 |
| 主脑 | 新聊天首次回复有真实预检状态；项目、开关、候选正确 | Agent 自称“已加载” |
| 执行端 | 卡片状态/版本/模型符合选择 | UI 能显示模型名 |
| 真实闭环 | 唯一 task_id、实际模型/Agent、真实文件及测试证据、主脑独立 review | 执行器自报 completed |
| 完成通知 | 确认订阅后结束回合；无人新发消息时原聊天收到通知 | sleep 或循环 status 等到完成 |

真实测试需先取得使用者对该模型调用和任何必要范围调整的授权。建议在新临时业务目录做一个两文件标准库小任务，明确允许文件和可判定条件；执行后由主脑检查改动、独立跑测试再记录验收。不要一开始运行高成本压力测试或一次测试所有模型。

记录 task_id、执行端、真实模型、测试结果、验收状态和仍未验证项。余额/积分、底层调用数未知时直接写未知。单任务成功不代表所有模型、宿主 GUI 或异常路径均通过。

## 7. 升级与常见停点

- 已有安装：先只读检查 Git 状态、版本、队列和插件收据，备份本机配置/记录。工作树不干净或更新关系不清楚时停止整合；不自动 stash、reset、clean 或覆盖目录。升级代码与重启后台要等任务空闲，保持原开关/预算/默认模型。
- DSH 插件：使用原收据核对归属，按 DSH 指南卸载原安装，再在新资料目录准备匹配版本；不编辑旧收据、直接删插件或自动重启 DSH。
- 新聊天没有状态行：核对 Codex 全局规则或 DSH 插件加载、新会话重载情况。状态行由模型遵循规则输出，尚无宿主强制渲染保证；不把“没提示”当成正常加载。
- 没有可用执行端：读取真实排除原因，检查卡片禁用、范围、模型默认 Agent、原生宿主就绪和用户授权；不擅自放宽。
- 派发结果未知：保留原 request_id/task_id，按原引用核查，不换 ID 重派。已有任务、整改仍保持原模型/执行端与原工具配置。
- 通知未返回：核对原订阅和宿主状态。DSH 关闭/重启、系统休眠及跨工作区唤醒没有完整保证；不能承诺关闭软件也必达。

工具保护是应用层参数检查，不是操作系统沙箱。任何验收都要核对实际文件范围。不要读取认证文件，也不要把 `.model-desk` 当作业务成果目录。

安装完成的交付应包含：安装路径与源码版本、所选主脑/执行端、已执行命令与退出状态、用户配置变更清单、各层验收结果、未验证项及下一步人工动作。不要在报告里放密钥、认证内容或完整账户配置。
