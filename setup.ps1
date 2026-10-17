<#
setup.ps1 - 多agent协作 offline-first installation helper (Windows only).

Default run (this installation directory only):
  1. report the prerequisites this script can actually see
  2. create .local-worker\venv from the Python you choose
  3. install requirements.txt into that venv
  4. install the local Node dependency (acpx) inside .local-worker\agent-runtime

Never done unless you ask for it explicitly:
  - starting the panel/service, calling a model or dispatching a worker
  - registering the global model_desk MCP        (-RegisterMcp)
  - installing the optional global rule          (-InstallRules)
  - installing or upgrading Python, Node, pnpm, DSH, Pi or any other third-party app
  - overwriting an existing venv, model configuration, MCP entry or rule file

Use -Check to only inspect the machine; -Check writes nothing.
#>
#Requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$SkipNodeDeps,
    [string]$Python,
    [switch]$RegisterMcp,
    [switch]$InstallRules
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$worker = Join-Path $root '.local-worker'
$venv = Join-Path $worker 'venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$runtime = Join-Path $worker 'agent-runtime'
$requirements = Join-Path $root 'requirements.txt'
$acpxRuntime = Join-Path $runtime 'node_modules\acpx\dist\runtime.js'
$minimumPython = [version]'3.11'
$verifiedPython = [version]'3.12'

function Step([string]$text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Note([string]$text) { Write-Host "   $text" }
function Warn([string]$text) { Write-Host "   [注意] $text" -ForegroundColor Yellow }

function Get-PythonCandidate {
    $candidates = @()
    if ($Python) {
        if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
            throw "找不到 -Python 指定的解释器：$Python。请给出可执行文件路径或命令名。"
        }
        $candidates += , @($Python, @())
    }
    else {
        if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @('python', @()) }
        if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', @('-3')) }
    }
    foreach ($entry in $candidates) {
        $exe = $entry[0]
        $prefix = $entry[1]
        $versionText = $null
        try {
            $versionText = & $exe @prefix -c 'import sys;print(".".join(str(p) for p in sys.version_info[:3]))' 2>$null
        } catch { $versionText = $null }
        if (-not $versionText) { Warn "$exe 无法报告版本，已跳过。"; continue }
        $version = [version]($versionText.Trim())
        if ($version -ge $minimumPython) { return @{ Exe = $exe; Prefix = $prefix; Version = $version } }
        Warn "$exe 是 Python $version；需要 $minimumPython 以上（本次只在 $verifiedPython 验证过）。"
    }
    return $null
}

function Describe-Executor([string]$label, [string]$variable, [string]$value, [string]$hint) {
    if (-not $value) {
        Warn "$label 未配置：设置用户环境变量 $variable 指向本机安装（$hint）。安装脚本不会代你安装或升级它。"
        return
    }
    $probe = $value
    if (Test-Path -LiteralPath $probe) { Note "$label = $value" }
    else { Warn "$label 指向的路径不存在：$variable=$value（$hint）。请核对后再启动执行任务。" }
}

try {
    foreach ($required in @($worker, $requirements, (Join-Path $runtime 'package.json'))) {
        if (-not (Test-Path -LiteralPath $required)) { throw "安装目录不完整，缺少 $required。请在解压后的项目根目录运行本脚本。" }
    }

    Step '步骤 1/4 本机前提（只读取，不修改）'
    $candidate = Get-PythonCandidate
    if ($candidate) { Note "Python：$($candidate.Exe) ($($candidate.Version))；已验证版本 $verifiedPython" }
    else { Warn '没有可用的 Python 3.11+；后续步骤需要 Python（请自行安装，本脚本不代装）。' }
    foreach ($tool in @('node', 'pnpm', 'npm')) {
        $found = Get-Command $tool -ErrorAction SilentlyContinue
        if ($found) { Note "$tool = $($found.Source)" } else { Warn "PATH 中没有 $tool。" }
    }
    Describe-Executor 'DSH' 'MODEL_DESK_DSH_EXE' $env:MODEL_DESK_DSH_EXE '指向 DeepSeek Harness.exe'
    Describe-Executor 'Pi' 'MODEL_DESK_PI_HOME' $env:MODEL_DESK_PI_HOME '指向 Pi 安装根目录，当前只适配 Pi 1.3.0'
    Describe-Executor 'Node（可选覆盖）' 'MODEL_DESK_NODE_EXE' $env:MODEL_DESK_NODE_EXE '不设置时使用 PATH 中的 node'
    if (Test-Path -LiteralPath $venvPython) { Note "venv 已存在：$venvPython" } else { Note 'venv 尚未创建。' }
    if (Test-Path -LiteralPath $acpxRuntime) { Note '本地 acpx 依赖已存在。' } else { Note '本地 acpx 依赖尚未安装。' }

    if ($Check) {
        Step '检查模式结束（未写入任何文件，未启动任何服务）'
        exit 0
    }

    # Fail before installing anything when the requested dependency path cannot
    # be completed. Pi-only users may explicitly choose -SkipNodeDeps.
    if (-not $candidate -and -not (Test-Path -LiteralPath $venvPython)) {
        throw '需要 Python 3.11+（已验证 3.12）；请先安装，或通过 -Python 指定解释器。'
    }
    if (-not $SkipNodeDeps) {
        if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
            throw '安装 acpx 需要 PATH 中的 Node；Pi-only 安装可显式使用 -SkipNodeDeps。'
        }
        if (-not (Test-Path -LiteralPath $acpxRuntime) -and -not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
            throw '需要 pnpm 才能按锁文件安装 acpx；请先安装 pnpm，或显式使用 -SkipNodeDeps。'
        }
        if (Get-Command pnpm -ErrorAction SilentlyContinue) {
            $pnpmVersion = & pnpm --version
            if ($LASTEXITCODE -ne 0 -or [version]$pnpmVersion -lt [version]'10.26.0') {
                throw '本项目需要 pnpm 10.26+，以读取本地依赖构建策略；未改动全局策略。'
            }
        }
    }

    Step '步骤 2/4 创建本目录的 Python 虚拟环境'
    if (Test-Path -LiteralPath $venvPython) {
        Note '已存在，跳过创建（不会覆盖或删除既有 venv）。'
    }
    else {
        if (-not $candidate) { throw '没有可用的 Python 3.11+，无法创建 venv。请先自行安装 Python，再重跑 setup.ps1。' }
        $arguments = @($candidate.Prefix) + @('-m', 'venv', $venv)
        & $candidate.Exe @arguments
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
            throw "创建 venv 失败（退出码 $LASTEXITCODE）。请检查权限或磁盘空间；本脚本不会删除任何已有环境。"
        }
        Note "已创建 $venvPython"
    }

    Step '步骤 3/4 安装 requirements.txt 声明的 Python 依赖（需要联网下载）'
    & $venvPython -m pip install --disable-pip-version-check --no-input --upgrade -r $requirements
    if ($LASTEXITCODE -ne 0) { throw "pip 安装失败（退出码 $LASTEXITCODE）。依赖只装进本目录 venv，不涉及全局 Python。" }

    Step '步骤 4/4 安装本地 Node 依赖（.local-worker\agent-runtime 的 acpx 0.19.5）'
    if ($SkipNodeDeps) {
        Note '按 -SkipNodeDeps 跳过。'
    }
    elseif (Test-Path -LiteralPath $acpxRuntime) {
        $installedAcpx = Get-Content -LiteralPath (Join-Path $runtime 'node_modules\acpx\package.json') -Raw | ConvertFrom-Json
        if ($installedAcpx.version -ne '0.19.5') { throw '已有 acpx 版本不是 0.19.5；请核对安装，本脚本不会覆盖。' }
        Note '已存在，跳过（不会重装第三方应用）。'
    }
    elseif (Get-Command pnpm -ErrorAction SilentlyContinue) {
        Push-Location $runtime
        try {
            & pnpm install --frozen-lockfile
            if ($LASTEXITCODE -ne 0) { throw "pnpm install 失败（退出码 $LASTEXITCODE）。" }
        }
        finally { Pop-Location }
        Note '已按 pnpm-lock.yaml 安装。'
    }
    else {
        throw 'pnpm 不可用，未完成 Node 依赖安装。'
    }

    if ($RegisterMcp) {
        Step '可选：注册全局 model_desk MCP（因为你显式使用了 -RegisterMcp）'
        & $venvPython (Join-Path $worker 'install_codex_workbench.py') --check
        if ($LASTEXITCODE -eq 0) {
            Note '已按当前路径注册，跳过。'
        }
        else {
            & $venvPython (Join-Path $worker 'install_codex_workbench.py')
            if ($LASTEXITCODE -ne 0) {
                throw 'Codex MCP 注册未完成（同名条目冲突或 Codex CLI 不可用）。脚本没有改动其他 MCP 设置。'
            }
        }
        Warn 'MCP 属于当前 Windows 用户的全局配置；已打开的 Codex 会话需要新建会话或重载才会看到工具。'
    }

    if ($InstallRules) {
        Step '可选：安装全局协作规则（因为你显式使用了 -InstallRules）'
        & $venvPython (Join-Path $worker 'install_workflow_rules.py')
        if ($LASTEXITCODE -ne 0) { throw '全局规则安装未完成。既有非托管指令不会被覆盖，脚本已在写入前保留原文件备份。' }
        Warn '规则及其操作指南已绑定本机安装目录；已有会话需要新建会话或重载才会加载。'
        Warn '通知/等待链路依赖本机桌面环境，请按 docs\INSTALL.md 自行验证。'
    }

    Step '完成'
    Note '本脚本没有启动面板、没有调用模型、没有派发任务。'
    if ($RegisterMcp) { Note '已按显式参数处理全局 MCP 注册。' }
    if ($InstallRules) { Note '已按显式参数处理全局协作规则安装。' }
    if (-not $RegisterMcp -and -not $InstallRules) { Note '未修改全局 Codex 配置或规则。' }
    if ($SkipNodeDeps) { Warn '已显式跳过 acpx 安装；DSH 尚需单独安装这项依赖。' }
    Note '下一步：设置 MODEL_DESK_DSH_EXE / MODEL_DESK_PI_HOME（见 docs\INSTALL.md），再运行 启动模型面板.ps1，并在面板的模型编辑界面添加你自己的模型。'
    Note '协作开关与付费授权默认关闭，需要你在面板里显式打开。'
}
catch {
    Write-Host "`n安装未完成：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host '本脚本只在当前安装目录内创建 venv 和安装依赖；全局 MCP/规则只在 -RegisterMcp / -InstallRules 时执行。'
    exit 1
}
