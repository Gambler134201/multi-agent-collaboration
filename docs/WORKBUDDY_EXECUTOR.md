# WorkBuddy 执行端适配候选

状态：2026-10-21，原生宿主 ACP 聊天候选已真实返回指定模型答复；**受控文件工具候选已挂载并通过隔离检查，正式任务派发与真实文件执行尚未验收，不能派发业务任务**。主脑适配是另一个工作区中的独立任务，本候选没有修改它。

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
7. 默认禁用全部内置工具和外部 MCP：`--tools ''`、空且严格 MCP 配置、禁用会话落盘和项目/用户设置来源。显式提供任务工具配置时，只通过 ACP `session/new` 挂载下述受控文件 MCP。不支持终端、会话续接或嵌套代理。
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

宿主的 `buildAgentCliRuntimeEnv` / `buildWorkbuddyClientInfoEnv` 会提供产品名称、版本等启动信息，初版候选没有提供这组信息。直接启动 CLI 的候选已补齐经安装元数据核对的 WorkBuddy 名称与版本，并清除调用方继承的 `CLIENT_INFO_*`；插件标识明确为 `model-desk-workbuddy-executor`。该直接启动路径不声明 `CODEBUDDY_HOST=workbuddy-desktop`，不复制设备 ID，不关闭请求校验、HTTP 鉴权或凭据保护。

- 30 项离线检查通过，包含错误分类、禁止重新登录、未知结果不重发、产品上下文隔离与不支持版本拒绝。
- 新的原生预检只调用 `initialize` 和 `session/new`：成功创建会话，当前模型为 `deepseek-v4.1-flash`，且模型目录包含它；`model_prompt_sent=false`。
- 没有改变执行范围、协作开关或其他工作流设置，预检前后完整设置一致。
- 此预检**未发送模型提示词，不能证明模型请求认证已修复、积分可用或业务执行成功**。不能将会话创建当成新的真实推理验收。
- 原生 `getUserInfo` 的公开实现包含令牌字段，因此没有调用该接口作为诊断手段。

忽略目录中的本机证据：`.local-worker/tasks/workbuddy-context-preflight-20261021/result.json`。

用户随后另行授权一次指定模型验证。产品信息候选发送一次提示词后，仍返回 `refusal / -32000 / auth`，答复为空；完整设置已恢复，未重试。**产品信息修正没有解决本次认证失败**，不能继续以它作为已修复方案。证据：`.local-worker/tasks/workbuddy-context-smoke-20261021/result.json`。

### 原生宿主桥接候选与无提示词预检

新增 `.local-worker/workbuddy_host_bridge.py`，复用 `NativeChatClient` 的模型锁定、执行范围检查、费用未知、错误分类和单次提示词限制，换用已经运行的 WorkBuddy 原生会话工厂创建执行进程。

- 只匹配明确 `.workbuddy` 配置目录对应的一个原生管道；没有或多个匹配时拒绝，不扫描其他配置、不启动或重启宿主。
- 在发送 RPC 前，由 Windows 核对管道服务端进程程序路径；数据通道还需归属于同一个宿主进程。新创建的 CLI 进程同样必须属于已核对的 WorkBuddy 安装。
- 原生控制面只允许为本次随机新建的 `modeldesk-wb-*` 会话调用 `session.create`，以及使用确切 `runtimeId` 清理该会话。不列举、重连、收养或修改已有聊天/会话。
- 不读账户/PID 文件，不调用会返回令牌的 `sidecar.ping` / `getUserInfo`，不发送凭据初始化内容。原生宿主自行把凭据保护初始化交给它创建的子进程，适配器不接收或复制密钥。
- 此时 `CODEBUDDY_HOST=workbuddy-desktop` 只传给确实由原生 WorkBuddy 宿主创建的子进程；Model Desk 保留自己的调用方标识，不更改主脑 owner/project。
- 仅临时独立目录、禁用工具的候选。创建结果未知时不重发；清理需匹配本次会话与运行身份，不按进程名终止程序。未请求的全局生命周期通知不保存或传给执行模型。
- 该通道依据已安装 5.8.2 程序代码，**不是官方承诺稳定的第三方 OpenAPI**；当前只支持该 Windows 安装身份。没有调用含令牌的 ping 复核全局协议版本，也没有把当前可用视为未来版本兼容。

离线共 **42 项**通过（原执行器 30 + 原生宿主桥接 12）。原生实机预检已确认：

1. 原生宿主创建新租约，ACP `initialize` / `session/new` 成功。
2. 当前模型与可选模型目录均确认 `deepseek-v4.1-flash`。
3. 没有发送 `session/prompt`，没有改范围或任何工作流设置。
4. 测试租约已通过本次 `runtimeId` 清理，并确认该子进程退出；未向宿主或其他会话发送终止/修改请求。

证据：`.local-worker/tasks/workbuddy-native-host-preflight-20261021/result.json`。这证明原生宿主传输能建立和清理独立会话；**尚未证明模型请求认证、积分扣减、编辑保护或正式工作台闭环**。仍为未注册候选，`ready=false`。后续真实聊天验证结果见下节；更进一步调用仍不继承已经用完的单次诊断额度。

### 原生宿主真实聊天验证：通过

用户另行授权仅一次原生宿主验证，指定 DeepSeek V4.1 Flash，只回复 `OK`。本次复用已经验证过的原生宿主通道，创建新的独立临时会话，禁用全部工具。

| 检查项 | 实际结果 |
|---|---|
| 请求模型 / 原生当前模型 | 均为 `deepseek-v4.1-flash` |
| 提示词发送次数 | 1；没有重试或替换模型 |
| 执行结果 | `status=completed`，`stopReason=end_turn` |
| 原生答复 | `OK`，与要求精确一致 |
| 原生错误 | 无 |
| 使用量、积分、人民币费用 | 均未返回，保留 `null`；不能称为零消耗 |
| 设置 | 仅临时把范围从 `local_only` 改为 `all`，最终完整设置与调用前一致；协作开关始终关闭 |
| 清理 | 仅清理本次租约，已确认对应子进程退出；没有重启宿主或修改其他会话 |

本机证据：`.local-worker/tasks/workbuddy-native-host-smoke-20261021/result.json`。没有读取账户/PID 文件、调用令牌接口或另配 DeepSeek API Key。

**本次通过仅覆盖 WorkBuddy 原生宿主 + 指定 DeepSeek 模型的单轮无工具聊天。** 它证明该通道在本机可返回指定模型答复；不能把原来直接启动 CLI 失败的唯一根因归因到某一个环境变量或凭据初始化步骤。

尚未验证/接入：积分实际扣减、文件工具与 `.model-desk` 保护、正式队列/任务记录/独立验收/整改/通知、Codex 与 DSH 发起的工作台完整闭环。候选继续 `ready=false`，没有变更现有模型或执行 Agent 默认值。

### 受控文件工具候选：隔离检查与原生挂载通过

新增 `.local-worker/workbuddy_tools.py`，通过现有 MCP SDK 的 stdio 连接提供三个工具：`read_file`、`write_file`、`edit_file`。没有终端、网络、账户、设置修改或子代理工具；WorkBuddy 内置工具保持禁用。

- 配置必须属于明确项目的 `.model-desk/runs/<task_id>`，绑定文件白名单、一次性任务 nonce 与当前保护代码哈希；同一执行配置不能被静默覆盖。
- 文件操作只接受白名单内的相对路径，拒绝 `.model-desk`、`.git`、项目外路径、路径穿越、Windows 设备/流路径，以及受保护目录别名和硬链接；读写限制为 2 MiB UTF-8 文本。不存在的可读目标返回 `exists=false`，允许模型随后创建新文件。
- 内容替换要求原文非空且仅出现一次。拒绝或无法确认完成后暂停后续写入；不能换工具绕过，MCP 重连也继承暂停状态。仍允许读取任务内文件用于说明问题。
- ACP 权限确认只处理本任务已实际挂载的文件 MCP、当前会话、白名单路径，且只选 `allow_once`。其他 MCP、内置工具、范围外请求或缺少加载证据均拒绝并停止本轮；不授权整会话放行。
- 任务证据由工具服务这一控制器组件维护，不是模型业务产物。提示词前验证实际 `tools/list` 证据，结束后再核对；保护拒绝不能因为原生返回 `end_turn` 而变成执行成功。结果仍为 `accepted=false`，不替代主脑验收。
- 这是本文件工具服务的边界，不是操作系统沙箱或对其他应用的约束。终端、删除/移动文件、任意目录浏览尚未提供；执行模型不能自行跑测试命令，后续主脑需独立运行测试。

独立检查：**60 项通过**。包含真实 Python 子进程的 MCP 握手、工具列表与读写/拒绝检查；其余为 ACP/原生租约模拟和路径/暂停状态检查，不调用真实模型。

WorkBuddy 5.8.2 原生会话预检：指定 `deepseek-v4.1-flash`，真实挂载并列出以上三个工具，事件为空、未创建业务文件、未发送 `session/prompt`、未修改用户设置，测试租约已确认清理。证据：`.local-worker/tasks/workbuddy-native-tools-preflight-20261021/result-v2.json`。该预检覆盖挂载通道；之后补充的配置身份校验和原生授权检查由上述隔离检查覆盖，不能把工具加载等同于真实模型已经使用文件工具。

**尚未进行真实模型写/改/读文件验证，也未接入正式模型注册、队列与 UI。** Codex 和 DSH 的现有执行链路均不变；后续经同一公共控制器接入，不能只验证一端就称全局完成。上一轮只回复 `OK` 的单次授权已经用完，新的文件执行模型请求需要另行确认。

### 后续账户交接要求

安装包公开代码显示桌面宿主与原生执行进程涉及产品配置、账户恢复和凭据保护初始化。直接启动 bundled CLI、取得模型目录，均不足以证明能够使用桌面账户。后续仍需核对请求上下文；若需要宿主能力，应核实由原生宿主承载的执行入口或产品正式支持的授权方式；不能用复制桌面凭据、伪造宿主身份或关闭保护来接通。

WorkBuddy 开放平台的第三方应用走显式 OAuth 授权；分享入口是内容预填，需要用户检查发送，并不等于后台任务已提交。现有公开文档尚未证明该入口满足本适配所需的“明确项目目录 + 固定模型 + 可关联结果”，因此没有自动切换到它。

当前候选继续 `ready=false`。未经另行授权，不再以模型推理验证认证猜测。

## 接下来必须完成

1. **账户执行入口（单轮聊天已通过）**：已通过原生宿主确认指定模型返回；仍需保留版本和会话归属校验，不从桌面认证文件提取或复制 token。单次测试授权不自动重用，积分扣减保持未核对。
2. **原生模型配置**：为 WorkBuddy 原生模型增加独立来源，不把 `home` 或现有 DeepSeek API 模型偷偷改成 WorkBuddy 内置模型；正确显示积分与未知费用。
3. **编辑与保护（候选已挂载）**：文件 MCP 已通过隔离和原生加载检查；下一步验证一次真实模型写/改/读文件，再接入正式队列。终端工具与任意命令执行尚未开放。
4. **共享控制器**：接入正式队列、任务状态、交付、人工取消、错误与未知结果、一次整改、会话边界、共享完成事件，支持 Codex / DSH 发起；保留所有默认模型/Agent 设置。
5. **界面与验收**：原生模型明确选择 WorkBuddy 后，可在两种主脑中分别验证最小真实闭环，再考虑部署。WorkBuddy 自身充当主脑的自动唤醒不属于执行端适配。

## 运行离线检查

```powershell
python -m unittest discover -s .local-worker/tests -p 'test_workbuddy*.py' -v
```

## 依据

- [官方 ACP 文档](https://www.codebuddy.cn/docs/cli/acp)：会话、模型配置与结束事件。
- [官方 CLI 文档](https://www.codebuddy.cn/docs/cli/cli-reference)：ACP、serve、工具与设置来源参数。
- [第三方应用与分享边界](https://open.workbuddy.cn/docs/third-party-app)：OAuth 授权与原生分享的能力边界。
- [WorkBuddy Open API](https://open.workbuddy.cn/docs/openapi)：本地助理与云端任务并非同一个本地项目执行入口，OAuth 应用授权也不能假设已存在。
- 本机 5.8.2 的公开安装代码：`cli/bin/codebuddy`、`main/workbuddy-product-config.js`、`main/cli-process-env.js`、`main/sidecar-manager.js`。只读包内产品代码，不读取认证文件。
