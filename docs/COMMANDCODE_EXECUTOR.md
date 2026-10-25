# CommandCode 原生执行端与模型顺序

通过已配置绝对路径的官方 Command Code CLI **1.86.1** 运行 headless NDJSON。模型默认 Agent `commandcode` 使用公共队列、项目记录、取消与独立验收；Codex/DSH 共用控制器。CLI 升级后拒绝执行，重新核对适配。原生入口的真实验证与两宿主模拟检查分别记录。

既有型号为 `deepseek/deepseek-v4.1-flash`；选择器还能读取已安装客户端的模型目录及各型号声明的思考选项。目录/额度读取移除 CLI 启动入口并核对原生源码 SHA，不派发 Agent；客户端更新后拒绝复用旧适配。目录不等于账户套餐权益或真实任务验收。当前官方目录及本机 CLI 没有 DeepSeek V4.1 Flash 免费版；可明确保留空位并跳过，不能替换 Beam 等其他免费型号或创建虚构型号。

凭据由工作台加密保存，仅在任务子进程中以 `COMMAND_CODE_API_KEY` 传入。每项任务使用独立 HOME/USERPROFILE 和 `--no-session`；不继承原生账户配置。只支持新会话和精确文件读写，禁用终端、目录遍历、MCP、子代理和会话续接。编辑任务启用官方 headless 写入选项，强制加载工作台原生 mod；beforeToolCall 逐次检查列明文件、真实路径、只读权限和记录目录。控制器核验 mod 加载、任务/job SHA 和每次拒绝记录；未加载或工具被拒绝时不能完成。这是工具参数保护，不是 OS 沙箱。任意脚本及终端均不开放。

原生聚合用量不能证明逐请求次数或人民币账单。金额/调用次数硬上限开启时拒绝，不自动改开关，也不伪造零费用。保留云端授权、范围、执行端开关；执行完成仍待主脑独立验收。

## 新任务模型顺序

控制器在忽略文件 `.local-worker/routing-priority.json` 保存显式顺位；通过 `/api/workflow` 的独立 `routing_priority` 信封更新。空位保留原因。开启后，新自动任务在通过能力、输入容量、权限与执行端检查的候选中按顺位选择；不在清单内的模型不参与自动路由。手动选择独立于顺序，不改运行任务和旧会话。

此策略不是自动重发机制。任务失败或结果未知时先由主脑核对实际文件和会话，再决定新的任务范围与下一候选。关闭执行端不取消运行任务。配置损坏阻止自动路由，不静默恢复费用排序。已在顺序中的两个 OpenCode 型号可共用一个执行端，不受该卡片单一“当前模型”的排除；其余授权门禁仍生效。

离线检查：`test_ordered_native_routing.py`、`test_commandcode_guard.mjs`、执行端/模型默认/路由/主脑网关回归和两宿主页面模拟。实际推理与独立验收另记。

官方参考：[Headless](https://commandcode.ai/docs/headless)、[Models](https://commandcode.ai/docs/models)、[Goat 计划](https://commandcode.ai/docs/plans/goat)、[Settings](https://commandcode.ai/docs/settings)。
