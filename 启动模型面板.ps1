#Requires -Version 7.0
param([ValidateRange(1,65535)][int]$Port = 8765,
      [ValidateRange(1,27)][int]$StartupTimeoutSeconds = 26)
$ErrorActionPreference = 'Stop'
$panelClock = [Diagnostics.Stopwatch]::StartNew()
$panelBase = 'http://127.0.0.1:' + $Port
function Get-PanelIdentity {
    $panelRemaining = $StartupTimeoutSeconds - $panelClock.Elapsed.TotalSeconds
    if ($panelRemaining -le 0) { throw 'MD_START_TIMEOUT: 就绪检查预算已耗尽。' }
    $panelListening = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
        Where-Object { $_.Port -eq $Port -and $_.Address.ToString() -in @('127.0.0.1', '0.0.0.0', '::') }
    if (-not $panelListening) { return $false }
    try {
        $panelReply = Invoke-WebRequest -Uri ($panelBase + '/api/ready') -NoProxy -MaximumRedirection 0 -SkipHttpErrorCheck -TimeoutSec ([Math]::Min(1, $panelRemaining))
        if ($panelReply.StatusCode -ne 200) { throw 'MD_START_FOREIGN: 端口响应无法核实；保留原服务。' }
        $panelData = $panelReply.Content | ConvertFrom-Json
    } catch {
        if ($_.Exception.Message -like '*MD_START_*') { throw }
        $panelException = $_.Exception
        while ($panelException.InnerException) { $panelException = $panelException.InnerException }
        if ($panelException -is [Net.Sockets.SocketException] -and
            $panelException.SocketErrorCode -eq [Net.Sockets.SocketError]::ConnectionRefused) { return $false }
        throw 'MD_START_FOREIGN: 端口连接结果未知；保留已有服务，请稍后重试。'
    }
    if ($panelData.service -ne 'model-desk' -or $panelData.ready -ne $true -or
        $panelData.workspace -ne $PSScriptRoot) {
        throw 'MD_START_FOREIGN: 端口不属于当前安装目录的工作台；未停止或替换进程。'
    }
    return $true
}
$panelHashAlgorithm = [Security.Cryptography.SHA256]::Create()
try {
    $panelLockKey = [BitConverter]::ToString($panelHashAlgorithm.ComputeHash(
        [Text.Encoding]::UTF8.GetBytes($PSScriptRoot.ToLowerInvariant()))).Replace('-', '')
} finally { $panelHashAlgorithm.Dispose() }
$panelStartupMutex = [Threading.Mutex]::new($false, ('Local\ModelDeskStartup_' + $panelLockKey))
$panelStartupLockHeld = $false
try {
    try { $panelStartupLockHeld = $panelStartupMutex.WaitOne([TimeSpan]::FromSeconds([Math]::Max(0, $StartupTimeoutSeconds - $panelClock.Elapsed.TotalSeconds))) }
    catch [Threading.AbandonedMutexException] { $panelStartupLockHeld = $true }
    if (-not $panelStartupLockHeld) { throw 'MD_START_BUSY: 另一个入口仍在启动；未重复创建进程。' }
$panelPython = Join-Path $PSScriptRoot '.local-worker\venv\Scripts\python.exe'
if (Get-PanelIdentity) { Write-Output ('模型面板已运行：' + $panelBase); return }
$panelSpawner = Join-Path $PSScriptRoot '.local-worker\start_panel.py'
if (-not (Test-Path -LiteralPath $panelPython)) { throw 'MD_START_INSTALL: 请先运行 setup.ps1 安装本目录依赖。' }
# close_fds + explicit stdio keep the service from retaining Codex action pipes.
& $panelPython $panelSpawner --port $Port | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'MD_START_SPAWN: 后台进程创建失败。' }
while ($panelClock.Elapsed.TotalSeconds -lt $StartupTimeoutSeconds) {
    if (Get-PanelIdentity) { Write-Output ('模型面板：' + $panelBase); return }
    Start-Sleep -Milliseconds 150
}
throw 'MD_START_TIMEOUT: 后台未在预算内就绪，请查看 .local-worker/panel.stderr.log。'
} finally {
    if ($panelStartupLockHeld) { $panelStartupMutex.ReleaseMutex() }
    $panelStartupMutex.Dispose()
}
