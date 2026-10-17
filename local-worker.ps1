param(
    [switch]$Status,
    [switch]$ListModels,
    [string]$ModelId,
    [string]$Project,
    [string[]]$Files,
    [string]$Task,
    [string]$TaskFile,
    [string]$TestCommand,
    [ValidateSet('dsh','pi','aider','direct')][string]$Engine,
    [ValidateSet('chat','edit')][string]$Kind = 'edit',
    [string]$ContinueTask,
    [ValidateSet('auto','manual')][string]$Route,
    [string]$RoutingFile,
    [switch]$Preview,
    [switch]$NoWait,
    [int]$TimeoutSeconds = 0
)
$workerPromptPath = $null
$workerPython = Join-Path $PSScriptRoot '.local-worker\venv\Scripts\python.exe'
$workerScript = Join-Path $PSScriptRoot '.local-worker\run_worker.py'
$workerArguments = @($workerScript)
if ($ListModels) {
    $workerArguments += '--list-models'
} elseif ($Status) {
    $workerArguments += '--status'
} else {
    if (([bool]$Task) -eq ([bool]$TaskFile)) {
        throw 'Provide exactly one of -Task or -TaskFile.'
    }
    if (-not $ContinueTask -and $Kind -eq 'edit' -and (-not $Project -or -not $Files)) {
        throw 'File edits require -Project and -Files.'
    }
    $workerArguments = @((Join-Path $PSScriptRoot '.local-worker\dispatch_task.py'), '--kind', $Kind, '--timeout', [string]$TimeoutSeconds)
    if ($Engine) { $workerArguments += @('--engine', $Engine) }
    if ($Project) { $workerArguments += @('--project', $Project) }
    if ($Task) {
        $workerPromptPath = Join-Path $PSScriptRoot ('.local-worker\tmp-prompt-' + [guid]::NewGuid().ToString('N') + '.txt')
        [System.IO.File]::WriteAllText($workerPromptPath, $Task, [System.Text.UTF8Encoding]::new($false))
        $workerArguments += @('--task-file', $workerPromptPath)
    } else { $workerArguments += @('--task-file', $TaskFile) }
    if ($TestCommand) { $workerArguments += @('--test-cmd', $TestCommand) }
    if ($Files) { $workerArguments += '--files'; $workerArguments += $Files }
    if ($ContinueTask) { $workerArguments += @('--continue-task', $ContinueTask) }
    if ($Route) { $workerArguments += @('--route', $Route) }
    if ($RoutingFile) { $workerArguments += @('--routing-file', $RoutingFile) }
    if ($Preview) { $workerArguments += '--preview' }
    if (-not $NoWait) { $workerArguments += '--wait' }
}
if ($ModelId) { $workerArguments += @('--model-id', $ModelId) }
& $workerPython @workerArguments
$workerExitCode = $LASTEXITCODE
if ($workerPromptPath) { Remove-Item -LiteralPath $workerPromptPath -ErrorAction SilentlyContinue }
exit $workerExitCode
