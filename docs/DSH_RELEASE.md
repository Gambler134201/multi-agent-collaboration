# DSH 0.2.7 发布候选

日期：2026-10-20。状态为本地候选准备，未公开发布、未安装到用户 DSH。现有 0.2.6 可继续使用。

## 本次整理

- 用当前指南替代包内零散历史说明，固定为八文件清单；入口见 [安装与使用](DSH_USAGE.md)。
- 工作台 README 不再把 DSH 写成“尚未接入”，历史实施记录仍保留。
- 配套后台包含运行中任务后的串行排队修复、失败状态 `error` 的客户端识别修复，以及 status/delivery 的 `execution_error`。
- 包版本提升为 0.2.7；Host、Client、账户、通知脚本及 Cordis patch 没有行为改动。候选不是要求现有用户重装，也没有替换用户原收据。

## 配套交付

| 项目 | 作用/范围 |
| --- | --- |
| `model-desk-dsh-brain-0.2.7.tgz` | 仅固定八文件插件，不能独立代替工作台 |
| `RELEASE-MANIFEST.json` | 候选归档、八文件和直接配套模块 SHA-256、源码提交及验证层级 |
| 完整匹配工作台源码 | 还需要 Python/前端/执行器等传递依赖；不能只复制十个指纹模块运行 |
| 本机安装资料 | 每台电脑按实际路径 prepare 生成，不包含在公共包里 |

安装器固定指纹直接覆盖 `brain_mcp_server.py`、`brain_context.py`、`brain_transport.py`、`brain_gateway.py`、`workflow_settings.py`、`host_balance.py`、`completion_wait.py`、`desktop_notify.py`、`completion_host.py`、`dsh_notify.py`。这不是完整工作台文件清单；其用途是防止安装计划绑定的入口被静默替换。

独立归档和源码中的客户端来源标记不同：prepare 会将标记替换为配置的回环 `panelUrl`。分发默认端口的通用归档时不携带用户路径；自定义来源应由安装器按实际参数重新准备，不直接改 live 配置。

## 已验证与待验证

| 范围 | 证据 |
| --- | --- |
| DSH 原生右侧入口、项目/聊天绑定、登录余额 | 已安装 0.2.6 及其前序修复的原生观察和独立复核 |
| DeepSeek/DSH 与 home/Pi 执行、独立验收及一次整改 | 小任务闭环已实测；不保证所有模型/任务同等质量 |
| 原聊天正常单任务/两任务批次的一次完成唤醒 | 已实测并复核；两项仍按原串行队列执行 |
| 受控失败通知与正式错误详情读取 | 已实测并复核，failed/needs_attention、未验收保持 |
| 取消/待恢复/忙碌/未知投递/停止通知 | 有隔离检查；不能冒充全部原生异常路径已通过 |
| 0.2.7 八文件打包、来源替换、安装器边界 | 用临时 profile、mock 环境/CLI 和本机静态 DSH 版本检查验证，不安装到真实 profile |
| 0.2.7 新装/升级的真实 GUI 与模型闭环 | 尚未进行，需单独授权和记录 |
| 关闭/重启 DSH、OS 休眠、跨工作区恢复 | 未由本轮实现或原生验证 |

本机原生功能证据来自已安装 0.2.6 加后台修复，不把它写成新包已安装。其他 DSH 版本仍需验证；安装器目前只认可 `0.2.1-rc.2`，包的 engines 范围不是逐版本实测证明。Pi 仍限已适配的 1.3.0。

## 发布前检查与本地验证

在仓库根目录、已安装本目录 Python 依赖的环境执行：

```powershell
.\.local-worker\venv\Scripts\python.exe -m unittest discover -s .local-worker/tests -p test_dsh_brain_desktop.py
.\.local-worker\venv\Scripts\python.exe -m unittest discover -s .local-worker/tests -p test_dsh_brain_plugin.py
node --test .local-worker/tests/test_dsh_brain_plugin.cjs .local-worker/tests/test_dsh_brain_sidebar.cjs .local-worker/tests/test_dsh_account.cjs
```

这些检查使用临时 profile、假 User 环境和模拟 CLI/账户，不安装到用户 DSH、不启动模型。新包清单必须与安装器 `FILES`、package.files 一致，归档不能有链接、额外文件、脚本或本机配置。

公开发布前还要核对匹配的完整源码、安装回归、文档链接及全部待上传历史。已有 `SOURCE-MANIFEST.json` 属于初次发行，不能当作当前整分支的源码校验清单；本候选的 manifest 也只声明它实际列出的内容。不要把本机恢复用 baseline 历史、密钥、DSH profile、项目 `.model-desk`、安装计划/配置/收据、诊断日志或媒体混入发布。详见 [源码整合清单](SOURCE_RECONCILIATION.md)。

本轮仅准备归档与说明，不执行 GitHub 推送、创建正式 Release、npm 发布、用户安装或账户操作。正式发布前的新包实装与干净安装仍需按证据分别记录。
