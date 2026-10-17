#Requires -Version 7.0
param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$panelPython = Join-Path $PSScriptRoot '.local-worker\venv\Scripts\python.exe'
$panelScript = Join-Path $PSScriptRoot '.local-worker\panel_server.py'
$panelRuntime = Join-Path $PSScriptRoot '.local-worker\panel-runtime.json'
if (Test-Path -LiteralPath $panelRuntime) {
    $panelRecord = Get-Content -LiteralPath $panelRuntime -Raw | ConvertFrom-Json
    try {
        $panelCheck = Invoke-RestMethod -Uri ($panelRecord.url + '/api/state') -NoProxy -TimeoutSec 2
        if ($panelCheck.workspace -eq $PSScriptRoot) {
            Write-Output ('模型面板已运行：' + $panelRecord.url)
            exit 0
        }
    } catch {}
}
$panelStdout = Join-Path $PSScriptRoot '.local-worker\panel.stdout.log'
$panelStderr = Join-Path $PSScriptRoot '.local-worker\panel.stderr.log'
$panelSpawner = Join-Path $PSScriptRoot '.local-worker\start_panel.py'
if (-not (Test-Path -LiteralPath $panelPython)) { throw '请先运行 setup.ps1 安装本目录的依赖。' }
try { $panelExisting = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $Port + '/api/state') -NoProxy -TimeoutSec 2 }
catch { $panelExisting = $null }
if ($panelExisting) {
    if ($panelExisting.workspace -eq $PSScriptRoot) { Write-Output ('模型面板已运行：http://127.0.0.1:' + $Port); exit 0 }
    throw ('端口 ' + $Port + ' 已由其他服务占用；未停止或替换任何进程。')
}
# close_fds + explicit stdio keep the service from retaining Codex action pipes.
& $panelPython $panelSpawner --port $Port | Out-Null
if ($LASTEXITCODE -ne 0) { throw '面板后台进程启动失败。' }
for ($panelAttempt = 0; $panelAttempt -lt 30; $panelAttempt++) {
    Start-Sleep -Milliseconds 200
    try {
        $panelReady = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $Port + '/api/state') -NoProxy -TimeoutSec 1
        if ($panelReady.workspace -ne $PSScriptRoot) { throw '端口上的服务不是当前安装目录的工作台。' }
        Write-Output ('模型面板：http://127.0.0.1:' + $Port)
        exit 0
    } catch {}
}
throw ('面板启动失败，请查看 ' + $panelStderr)
