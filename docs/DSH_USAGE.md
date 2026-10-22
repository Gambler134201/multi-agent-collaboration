# DSH 安装与使用

更新于 2026-10-22。本指南描述当前入口；开发历史另见 [实现记录](DSH_BRAIN_PLUGIN.md)，版本与交付边界见 [发布说明](DSH_RELEASE.md)。也可将 [Agent 安装指南](AGENT_INSTALL.md) 交给自己的 Agent 执行。

## 适用版本

- Windows；维护者实测的 DSH 产品为 `0.2.1-rc.2`，Pi 执行器为 `1.3.0`。
- 当前插件源码为 **0.2.11**。0.2.6 覆盖原生任务/通知闭环，0.2.10 的右侧入口和首轮状态信号已由用户确认；0.2.11 的无 Codex 环境隔离冷启动已通过。实际用户机器的独立冷启动、安装及主脑加载仍需单独验收，不从源码发布推定通过。
- DSH 主脑插件、DSH/Pi 执行器和工作台是不同组件。只安装一个 tgz 无法替代完整工作台；其他宿主或版本不由本次测试保证。

## 首次安装

先完成 [工作台安装](INSTALL.md)：准备 Python 环境、所需执行器和模型配置。`setup.ps1` 不安装 DSH 主脑插件，不修改 DSH 登录账户。

DSH 需已自行打开过并初始化 desktop profile。等现有执行任务结束，再完全退出 DSH。下面在 PowerShell 7 中执行；路径均为示例，替换为自己的绝对路径：

```powershell
$workbenchRoot = 'D:\Work\multi-agent-collaboration'
$workbenchPython = Join-Path $workbenchRoot '.local-worker\venv\Scripts\python.exe'
$dshExecutable = 'D:\Apps\DSH\DeepSeek Harness.exe'
$dshHome = Join-Path $env:USERPROFILE '.dsh' # 自定义 DSH_HOME 时改成实际目录
$installRecord = 'D:\ToolConfigs\ModelDesk\dsh-brain-0.2.11'
$installer = Join-Path $workbenchRoot '.local-worker\dsh_brain_desktop.py'

& $workbenchPython $installer prepare --output $installRecord --workbench $workbenchRoot --python $workbenchPython --executable $dshExecutable --home $dshHome
```

`$installRecord` 必须尚不存在，其父目录由用户预先建立。`prepare` 静态核对安装和源码，写本机配置/包/计划，不启动应用、工作台或模型。若已安装同名插件，先按下方升级流程处理，不删除插件目录绕过归属检查。

显式启动匹配的工作台，再分别预检和安装：

```powershell
pwsh -NoProfile -File (Join-Path $workbenchRoot '打开模型工作台.ps1') -NoBrowser
& $workbenchPython $installer check --directory $installRecord
& $workbenchPython $installer install --directory $installRecord
```

预检成功才执行安装。`check` 会连接已运行的工作台，无任务变更权限，不查询模型。`install` 调用 DSH 官方 desktop CLI，只维护本插件及 `MODEL_DESK_DSH_BRAIN_CONFIG`；不会开启协作/付费、改执行 Agent 或自动重启 DSH。官方包管理过程可能需要已有依赖缓存；失败按原诊断处理，不反复运行。

自行打开 DSH，在项目所属原生聊天中打开右侧“模型工作台”。若之前有旧页面，关闭旧 tab 后重新从原生入口打开。保留安装资料目录及其 `config.json`、`plugin.tgz`、`plan.json`、`receipt.json`，以后更新/卸载要用。

## 日常使用

1. 在 DSH 选择项目及其聊天，从右侧入口打开工作台，确认“当前项目”正确。没有明确项目就保持未绑定，不借用服务目录。
2. 在工作台添加本地/自建或付费 API 执行模型。给各模型保存自己的执行 Agent，例如本地 Qwen → Pi、DeepSeek API → DSH；“各执行 Agent 参数设置”只调整 Agent 参数。见 [每模型默认 Agent](USAGE.md#每模型默认执行-agent)。
3. 通过各执行端卡片的独立开关选择可用执行端；旧模型范围仍作为兼容限制共同生效。打开多 Agent 协作，正常描述需求。主脑按真实预检、候选和任务风险决定是否派发；手动默认越界不代表整个范围没有模型，不静默切换。
4. 主脑取得原任务编号，确认通知订阅成功后结束轮次。任务和等待状态可在工作台查看；用户仍可主动询问或补充需求。
5. 原聊天打开且空闲时，任务结束后自动回来取交付、独立测试并验收；不把执行模型自报完成直接算通过。

工作台不会为新项目按钮初始化 Git。任务记录由控制器保存在该项目 `.model-desk`；执行模型不应把业务成果放进去。切换项目不搬记录；同项目新聊天可只读历史，原 owner 的验收/整改权限不会自动转移。

## 等待、完成与费用

| 显示/回执 | 含义 | 后续 |
| --- | --- | --- |
| 未确认监听/心跳过期 | 不能确认正在监听 | 核对原任务/通知，不能重派 |
| `waiting=true` | 原聊天后台通知已确认 | 主脑结束当前轮次，本地进程等待 |
| 执行 `completed` | 执行器结束，尚不代表验收通过 | 主脑独立检查 |
| 通知 `received` | 原事件已登记接收 | 不再重复订阅/登记同一事件 |
| `accepted` | 逐条验收已有独立证据 | 当前任务闭环完成 |
| `failed` / `needs_attention` | 执行失败或需要介入 | 保留实际错误与未验收状态 |
| `recovery_pending` | 需要明确恢复判断 | 不自动重发 |

一批任务取得全部编号后只登记一次 `brain_wait`，全部结束后通知一次。当前是串行执行队列；批量通知不等于多模型并行。后台等待不调用主脑模型，派发、唤醒与验收会有主脑用量，执行模型也有自身用量，整个流程不称零额度。

DSH 顶部显示其官方登录账户余额的最近公开快照，通常约两分钟同步；不是工作台 DeepSeek API Key 的余额。主脑账户权益、API 费用和本地推理分开看，不假定同品牌额度互通。界面的刷新只读最近快照；来源失联/过期保留未知。

## 升级与卸载

1. 等待执行队列空闲，完全退出 DSH；备份并保留现有安装资料及用户记录。
2. 使用原收据目录卸载自己的旧插件：

```powershell
$oldRecord = 'D:\ToolConfigs\ModelDesk\dsh-brain-0.2.6'
& $workbenchPython $installer uninstall --directory $oldRecord
```

3. 确认卸载结果。将匹配工作台代码及插件源码部署到目标位置，在**新的**资料目录重新 prepare → 显式启动工作台 → check → install。
4. 自行重开 DSH；先核对项目/候选/余额，再用最小任务验收。后台重启后需要 `brain_connect`，Python MCP 文件变化后已有 MCP 进程需要重新加载。

旧 `check` 因后台指纹改变而失败，不表示旧任务损坏；不要修改旧 `plan.json`/`receipt.json`。卸载按原安装归属独立校验，不依赖后台仍是旧源码。遗失收据或 CLI 效果未知时先核查，不能收养同名插件或重发命令。

回滚也走“卸载当前拥有的插件 → 还原匹配的旧代码 → 新资料目录准备旧版本 → 预检安装”。不把旧收据改成 installed，不回滚/覆盖用户密钥、设置或项目历史。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 无法连接工作台 | 当前原生入口可按需启动匹配后台；检查 PowerShell 7、Python venv 和安装指纹。也可显式启动，不能以启动 Codex 作为依赖修复 |
| 项目未选择/不正确 | 在原生项目聊天重新打开入口，不把 URL 改到别的项目来冒充绑定 |
| 仅付费却说无模型 | 读取真实候选/范围排除原因，检查手动默认是否越界；不自动换模型或放宽授权 |
| 执行失败详情读不到 | 匹配后台应返回 `execution_error`，status 还兼容 `error`；更新后重开 MCP，不反复派同一任务 |
| 结果未知 | 保留原 request_id/task_id，按原引用核查；不换 ID 补派 |
| 没有自动回来 | 查看原通知是否已确认、聊天/宿主是否关闭、心跳是否新鲜；不据沉默判失败或取消 |
| 页面余额未知 | 检查 DSH 原生登录状态和快照新鲜度，不读取认证文件或把 API 余额当主脑余额 |
| 已有同名插件/计划失效 | 使用原资料核对归属；新版本重新准备，不覆盖收据或直接删包 |

取消、待恢复、未知投递已有隔离测试；原生这些异常路径、关闭/重启 DSH 后恢复、系统休眠和跨工作区仍未完整实测。不要因此承诺关闭软件也能必达。
