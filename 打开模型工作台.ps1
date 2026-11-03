#Requires -Version 7.0
param([switch]$NoBrowser, [ValidateRange(1,65535)][int]$Port = 8765,
      [ValidateRange(1,27)][int]$StartupTimeoutSeconds = 26)

$ErrorActionPreference = 'Stop'
try {
    $panelLauncher = Join-Path $PSScriptRoot '启动模型面板.ps1'
    # Call the PowerShell script in this process. Capturing a nested native pwsh
    # can leave its output pipe inherited by the long-running background server.
    & $panelLauncher -Port $Port -StartupTimeoutSeconds $StartupTimeoutSeconds | Out-Null
    $panelAddress = [uri]('http://127.0.0.1:' + $Port)
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
