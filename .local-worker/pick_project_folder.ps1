#Requires -Version 7.0
param([string]$InitialDirectory)

$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName System.Windows.Forms
    $picker = [System.Windows.Forms.FolderBrowserDialog]::new()
    try {
        $picker.Description = 'Select a project folder'
        $picker.UseDescriptionForTitle = $true
        $picker.ShowNewFolderButton = $true
        if ($InitialDirectory -and (Test-Path -LiteralPath $InitialDirectory -PathType Container)) {
            $picker.SelectedPath = $InitialDirectory
        }
        $answer = $picker.ShowDialog()
        $result = if ($answer -eq [System.Windows.Forms.DialogResult]::OK) {
            @{ status = 'selected'; project = $picker.SelectedPath }
        } else {
            @{ status = 'cancelled' }
        }
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        [Console]::WriteLine(($result | ConvertTo-Json -Compress))
    } finally {
        $picker.Dispose()
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
