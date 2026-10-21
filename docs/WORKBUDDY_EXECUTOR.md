# WorkBuddy 执行端适配候选

状态：2026-10-21，底层 ACP 聊天传输候选；**尚未接通真实推理，不能在工作台派发业务任务**。主脑适配是另一个工作区中的独立任务，本候选没有修改它。

## 本次明确的产品目标

- 由现有 Codex / DSH 主脑派发任务，WorkBuddy 作为执行端。
- 优先使用 WorkBuddy 登录账户自带的模型与积分。
- 用户指定首次测试模型为 **DeepSeek V4.1 Flash**，原生模型 ID 为 `deepseek-v4.1-flash`；不使用 Auto，不回退到其他模型或 DeepSeek API Key。
- 新执行端共享项目记录、模型范围、独立验收和完成通知语义；原生身份与计费由各自适配器处理。

## 已实现和验证

实现位于 `.local-worker/workbuddy_executor.py`：

1. 只读识别本机 WorkBuddy 5.8.2 安装身份，计算自带 CLI / 产品文件哈希。导入与 `availability` 不启动程序、服务或模型。
2. 使用安装包自带 `cli/bin/codebuddy` 和 `codebuddy-lite-wb.mjs`，由 WorkBuddy 自身的 Electron Node 运行，不安装另一个 CodeBuddy。
3. 此版本单独 `--acp` 会在本机静默退出；已验证 `--serve --acp` 返回真实 ACP 1 握手和新会话响应。保持原生 HTTP 鉴权默认值，不使用 `--auth none`。
4. 配置目录来自显式参数。桌面产品目录交接由原生客户端读取其 `cache/acc-product-config-v3.json`；适配代码不打开此缓存内容或认证文件，不复制令牌。隔离预检使用临时 HOME，不引用桌面目录。
5. 已在原生响应核对 `deepseek-v4.1-flash`：模型目录、`models.currentModelId` 和 `configOptions.model.currentValue` 一致。模型选择还需在发送提示词前再次获得原生确认。
6. 只接受 `session/prompt` 的权威结束标志；截断、拒绝或断线不算完成。已发送而无结果时标未知，不重发。
7. 本候选只支持禁用全部工具的单轮聊天：`--tools ''`、空且严格 MCP 配置、禁用会话落盘和项目/用户设置来源。不支持编辑、会话续接或嵌套代理。
8. 原生人民币费用、积分扣减未知时保留 `null`，不记为免费。强制金额或请求上限开启时拒绝原生调用，不关闭用户上限。
9. `availability.ready=false`，未注册为现有路由的可派发执行端，现有本地/Pi、DeepSeek/DSH 路由不变。

这些底层机制已用真实 Python 子进程模拟 ACP 做离线回归；不调用真实模型作为回归。

## 第一次真实最小验证

用户原先授权一次最小测试，随后将模型明确改为 DeepSeek V4.1 Flash。测试只要求回复 `OK`，禁用工具，在临时项目运行。

- 已临时把范围从 `local_only` 切到 `all`，协作总开关保持关闭；结束后完整核对并恢复原设置。
- 发送一次 `session/prompt`，未重试或回退。
- 原生结果：`stopReason=refusal`，答复为空，没有使用量数据；**本次真实推理验收未通过**。
- 费用与积分扣减未知，不能称为零消耗，也不能宣称登录账户链路已打通。
- 初版只保留了结束原因，未保留原生错误扩展，具体拒绝原因仍未知。已补充后续错误分类与回归，不能据此追认本次错误为登录问题。
- 独立原生测试不是工作台正式任务，没有证明派发、工具保护、交付、验收、整改或通知链路。

本机测试证据保存在忽略目录 `.local-worker/tasks/workbuddy-native-smoke-20261021/result.json`，不发布本机记录。

## 第二次获授权诊断：已定位为认证不可用

用户在桌面聊天里选择相同模型能正常回复，并另行明确授权仅一次诊断。本轮仍只发送一次要求回复 `OK` 的提示词，不重试、不更换模型。

- 原生确认模型仍为 `deepseek-v4.1-flash`。
- 原生返回 `stopReason=refusal`；错误扩展明确返回 `code=-32000`、`data.category=auth`，适配器分类为 `authentication_required`。
- 原始错误体不公开、不写入公共日志；只保留允许列举的错误分类、数值码、是否存在详情。
- 已在 `finally` 恢复原范围，并核对完整工作流设置与调用前一致；协作开关始终关闭。
- 答复为空，使用量、积分和人民币费用仍未知。没有确认此次请求是否产生计费。
- 这证明本轮原生执行进程未获得可用认证；**不能由此声称桌面已退出登录、账户无积分、模型不可用，也不能确定具体根因为路径、初始化时序或凭据保护能力。**
- 没有调用 ACP `authenticate`：5.8.2 的公开实现会等待初始化，然后在已有会话时先 `logout()` 再 `login()`；不能用它作无副作用的登录检查。适配器协议允许列表不包含此方法，离线检查确保未发送该请求。
- 没有读取认证文件、提取 token、降低凭据加密策略、关闭原生 HTTP 鉴权或自动重启桌面程序。

本机证据在忽略目录 `.local-worker/tasks/workbuddy-native-diagnostic-20261021/result.json`。这是第二次独立诊断的新证据，不追认第一次丢失的错误内容。

### 后续只读复核：不能等同于“CLI 没登录”

对相同 5.8.2 原生源码进一步核对：`newSession` 会调用 `initializeSession`，后者先等待 `authManager.initialized`，再检查 `currentSessionSubject` 非空。先前测试已成功创建会话，所以认证报错不能直接归因为本地完全未登录；模型请求阶段的认证失败原因仍需区分远端授权、账户状态变化与运行上下文。

宿主的 `buildAgentCliRuntimeEnv` / `buildWorkbuddyClientInfoEnv` 会提供产品名称、版本等启动信息，初版候选没有提供这组信息。候选已补齐经安装元数据核对的 WorkBuddy 名称与版本，并清除调用方继承的 `CLIENT_INFO_*`；插件标识明确为 `model-desk-workbuddy-executor`。不声明 `CODEBUDDY_HOST=workbuddy-desktop`，不复制设备 ID，不关闭请求校验、HTTP 鉴权或凭据保护。

- 30 项离线检查通过，包含错误分类、禁止重新登录、未知结果不重发、产品上下文隔离与不支持版本拒绝。
- 新的原生预检只调用 `initialize` 和 `session/new`：成功创建会话，当前模型为 `deepseek-v4.1-flash`，且模型目录包含它；`model_prompt_sent=false`。
- 没有改变执行范围、协作开关或其他工作流设置，预检前后完整设置一致。
- 此预检**未发送模型提示词，不能证明模型请求认证已修复、积分可用或业务执行成功**。不能将会话创建当成新的真实推理验收。
- 原生 `getUserInfo` 的公开实现包含令牌字段，因此没有调用该接口作为诊断手段。

忽略目录中的本机证据：`.local-worker/tasks/workbuddy-context-preflight-20261021/result.json`。仍待单独授权的一次指定模型请求，才能评估这项候选是否解决实际失败。

### 后续账户交接要求

安装包公开代码显示桌面宿主与原生执行进程涉及产品配置、账户恢复和凭据保护初始化。直接启动 bundled CLI、取得模型目录，均不足以证明能够使用桌面账户。后续仍需核对请求上下文；若需要宿主能力，应核实由原生宿主承载的执行入口或产品正式支持的授权方式；不能用复制桌面凭据、伪造宿主身份或关闭保护来接通。

WorkBuddy 开放平台的第三方应用走显式 OAuth 授权；分享入口是内容预填，需要用户检查发送，并不等于后台任务已提交。现有公开文档尚未证明该入口满足本适配所需的“明确项目目录 + 固定模型 + 可关联结果”，因此没有自动切换到它。

当前候选继续 `ready=false`。未经另行授权，不再以模型推理验证认证猜测。

## 接下来必须完成

1. **账户执行入口**：通过官方认证/宿主交接确认指定模型真正返回，不从桌面认证文件提取或复制 token；保留原生错误分类。进一步模型调用需另获用户授权，本次一次测试额度不自动重用。
2. **原生模型配置**：为 WorkBuddy 原生模型增加独立来源，不把 `home` 或现有 DeepSeek API 模型偷偷改成 WorkBuddy 内置模型；正确显示积分与未知费用。
3. **编辑与保护**：接入已验证的原生 Hook / 工具代理，复用 `.model-desk` 参数保护并验证确实挂载；没有证据时不能开放编辑。工具保护仍不等于 OS 沙箱。
4. **共享控制器**：接入正式队列、任务状态、交付、人工取消、错误与未知结果、一次整改、会话边界、共享完成事件，支持 Codex / DSH 发起；保留所有默认模型/Agent 设置。
5. **界面与验收**：原生模型明确选择 WorkBuddy 后，可在两种主脑中分别验证最小真实闭环，再考虑部署。WorkBuddy 自身充当主脑的自动唤醒不属于执行端适配。

## 运行离线检查

```powershell
python -m unittest discover -s .local-worker/tests -p test_workbuddy_executor.py -v
```

## 依据

- [官方 ACP 文档](https://www.codebuddy.cn/docs/cli/acp)：会话、模型配置与结束事件。
- [官方 CLI 文档](https://www.codebuddy.cn/docs/cli/cli-reference)：ACP、serve、工具与设置来源参数。
- [第三方应用与分享边界](https://open.workbuddy.cn/docs/third-party-app)：OAuth 授权与原生分享的能力边界。
- [WorkBuddy Open API](https://open.workbuddy.cn/docs/openapi)：本地助理与云端任务并非同一个本地项目执行入口，OAuth 应用授权也不能假设已存在。
- 本机 5.8.2 的公开安装代码：`cli/bin/codebuddy`、`main/workbuddy-product-config.js`、`main/cli-process-env.js`、`main/sidecar-manager.js`。只读包内产品代码，不读取认证文件。
