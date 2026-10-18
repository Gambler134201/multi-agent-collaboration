# 使用说明

本文件补充 `README.md` 里的日常操作，重点说明每模型默认执行 Agent、精简目标进度和离线检查。

## 每模型默认执行 Agent

每个执行模型可以在自己的配置里保存 `default_agent`：

- `follow_global`：没有模型偏好时继承全局 `execution_agent`（旧模型行缺少该字段时同样继承）。
- `dsh`：这个模型的新任务默认交给 DeepSeek Harness。
- `pi`：这个模型的新任务默认交给 Pi Agent。

新任务的解析顺序固定为：

1. 本次任务显式指定（例如 `--engine dsh`、任务表单里明确选择）；
2. 所选模型的 `default_agent`；
3. 全局回退 `execution_agent`。

自动路由会对每个最终模型候选分别执行这个顺序，然后用解析出的 Agent 检查可用性和付费边界。无效或不可用的偏好会被拒绝，不会静默改用其他模型、执行器或提供方。

面板提供两个入口：

- 协作栏的「模型 / 执行 Agent / 保存」快捷入口：只保存所选模型这一个字段，一次请求只提交 `{default_agent}`。
- 模型编辑器的「默认执行 Agent」：保存模型时同样只改这个字段；如果连接配置没有变化，保存请求只带 `default_agent`。

「各执行 Agent 参数设置」只调整适配器参数（例如 DSH 运行预设），不决定谁执行。全局回退仍保留在后台以兼容旧配置，但面板不再提供容易误解的全局选择器。

已有任务保持 `task.json` 里记录的 Agent：

- 排队中、运行中、已完成任务不会被新偏好重新路由；
- 续接、整改、会话复用和恢复任务保留原 Agent / 模型 / 项目；
- 需要切换 Agent 时创建新任务，而不是修改运行中的任务。

Pi 不加载 DSH 预设、扩展、MCP 或子代理授权。

## 目标进度与历史目标

- 首页只在存在进行中目标时显示进度区；默认是一行折叠摘要，只统计进行中目标的阶段。
- 摘要区分「执行中」「执行完成待验收」「待前置验收」「待恢复」「失败/受阻」等状态；执行器完成不等于独立验收通过。
- 展开状态在自动轮询时保留；切换项目、目标全部结束或重新进入时按项目重置。
- 已完成 / 已取消目标不再常驻首页；在「任务记录管理」的「历史目标与阶段（只读查看）」里查看阶段、任务、交接和验收记录。
- 历史目标只读，不会归档、删除、改状态或派发任务；原有记录删除仍需预览和二次确认。
- 读取失败、HTTP 错误或返回结构异常会显示为明确的未知状态，不会显示成「没有目标」或「全部完成」。
- 「＋ 新建目标」始终可用；未绑定项目时会提示先选择项目。

## 离线与浏览器检查

所有回归检查默认离线，只使用临时记录、mock 提供方和假 HTTP 数据，不调用真实模型、不发送真实桌面消息、不启动服务。

### Python 检查

在仓库根目录执行：

```powershell
.\.local-worker\venv\Scripts\python.exe -m unittest discover -s .local-worker\tests -p "test_*.py"
```

只跑与本次改动相关的检查时，可显式列出模块：

```powershell
.\.local-worker\venv\Scripts\python.exe .\.local-worker\tests\test_opensource_bootstrap.py
.\.local-worker\venv\Scripts\python.exe -m unittest discover -s .local-worker\tests -t .local-worker\tests -p "test_model_agent_defaults.py"
.\.local-worker\venv\Scripts\python.exe -m unittest discover -s .local-worker\tests -t .local-worker\tests -p "test_execution_engines.py"
```

以上命令均在仓库根目录执行。部分既有集成检查会读取用户安装的 DSH 预设或检查执行器依赖，需先按安装说明配置运行时；缺件时会明确失败，不能把完整回归描述为全部通过。单元与浏览器检查不发送真实推理请求。

### 浏览器检查

浏览器检查使用已有的 Playwright 安装，**不把 Playwright 加入项目生产依赖**。依赖查找顺序：

1. `MODEL_DESK_PLAYWRIGHT`：Playwright 模块或包的绝对路径；
2. `MODEL_DESK_NODE_MODULES`：包含 `playwright` 的 `node_modules` 目录；
3. 标准 `require('playwright')` 解析，包括 `NODE_PATH`。

例如，使用你自己的 Node 依赖目录：

```powershell
$env:NODE_PATH = 'D:\path\to\node_modules'
node .local-worker\tests\test_model_agent_defaults_ui.cjs
node .local-worker\tests\model-agent-entry-checks\check_model_agent_entry_ui.cjs
node .local-worker\tests\model-agent-entry-checks\check_entry_refresh_failure.cjs
node .local-worker\tests\goals-progress-checks\check_goals_progress.cjs
node .local-worker\tests\check_task_history.cjs
node .local-worker\tests\check_project_views.cjs
node .local-worker\tests\panel-observability.cjs
node .local-worker\tests\test_panel_lifecycle.cjs
```

浏览器通道可通过 `MODEL_DESK_BROWSER_CHANNEL` 选择；不设置时检查会先尝试调用方给的通道，再尝试本机可用的 Chromium/Edge 回退。可用值例如 `msedge`、`chrome`，或 `chromium` 表示 Playwright 自带的 Chromium。

浏览器检查默认只访问本地 mock 或进程内拦截，不会连接真实工作台。`panel-observability.cjs` 额外提供一次手工只读诊断：只有显式设置 `MODEL_DESK_LIVE_READONLY=1` 时才会 GET 运行中面板的 `/api/state`。这个只读探测是手工诊断，不是离线回归证据；`check_live_readonly.cjs`、`check-live-ui.cjs` 等旧 live 脚本同样不纳入离线回归。

生成的报告和截图写入 `.local-worker\tests\` 下被 `.gitignore` 忽略的 `*-report.json` / `*.png` 证据路径，不进入源码提交。
