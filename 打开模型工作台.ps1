#Requires -Version 7.0
param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
try {
    $panelLauncher = Join-Path $PSScriptRoot '启动模型面板.ps1'
    # Call the PowerShell script in this process. Capturing a nested native pwsh
    # can leave its output pipe inherited by the long-running background server.
    & $panelLauncher | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw '面板启动脚本未成功完成。'
    }
    $panelRuntimePath = Join-Path $PSScriptRoot '.local-worker\panel-runtime.json'
    $panelRuntimeRecord = Get-Content -LiteralPath $panelRuntimePath -Raw | ConvertFrom-Json
    $panelAddress = [uri]$panelRuntimeRecord.url
    if ($panelAddress.Scheme -ne 'http' -or $panelAddress.Host -ne '127.0.0.1') {
        throw '面板地址异常，请检查本地服务配置。'
    }
    $panelState = Invoke-RestMethod -Uri ($panelAddress.AbsoluteUri.TrimEnd('/') + '/api/state') -NoProxy -TimeoutSec 3
    if ($panelState.workspace -ne $PSScriptRoot) {
        throw '当前端口上的服务不是这个模型工作台。'
    }
    if (-not $NoBrowser) {
        Start-Process -FilePath $panelAddress.AbsoluteUri
    }
    Write-Output ('模型工作台已就绪：' + $panelAddress.AbsoluteUri)
} catch {
    $panelFailure = '模型工作台打开失败：' + [Environment]::NewLine + $_.Exception.Message
    if (-not $NoBrowser) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show($panelFailure, '模型工作台', 'OK', 'Error') | Out-Null
    }
    Write-Error $panelFailure -ErrorAction Continue
    exit 1
}
