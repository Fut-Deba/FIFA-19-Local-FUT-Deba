param(
    [string]$StatusPath = "",
    [string]$DataRoot = "",
    [string]$PythonExe = "",
    [int]$GamePid = 0,
    [switch]$ManualContinue,
    [ValidateSet('NORMAL', 'RTG')]
    [string]$AccountMode = 'NORMAL'
)

$root = Split-Path $PSScriptRoot -Parent
if (-not $DataRoot) {
    if ($env:LOCALFUT19_DATA_ROOT) {
        $DataRoot = $env:LOCALFUT19_DATA_ROOT
    } else {
        $DataRoot = Join-Path $env:LOCALAPPDATA `
            'FIFA19LocalFUT\profiles\eaapp-4052077'
    }
}
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
if (-not $StatusPath) {
    $diagnosticsDirectory = Join-Path $DataRoot 'diagnostics'
    New-Item -ItemType Directory -Force -Path $diagnosticsDirectory | Out-Null
    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $StatusPath = Join-Path $diagnosticsDirectory "eaapp-full-server-guarded-$timestamp.jsonl"
}
$StatusPath = [IO.Path]::GetFullPath($StatusPath)
$networkLog = [IO.Path]::ChangeExtension($StatusPath, '.network.log')
$stopFile = Join-Path $DataRoot 'eaapp-full.stop'
Write-Host "Diagnostic log: $StatusPath"
Write-Host "Network log: $networkLog"
Write-Host "Safe stop: run tools\stop_eaapp_full_server.ps1 (do not use Ctrl+C)."

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this diagnostic as Administrator.'
}
if (-not $PythonExe) {
    $privatePython = Join-Path $root '.runtime\Scripts\python.exe'
    if (Test-Path -LiteralPath $privatePython -PathType Leaf) {
        $PythonExe = $privatePython
    } else {
        $PythonExe = (Get-Command python.exe -ErrorAction Stop).Source
    }
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable not found: $PythonExe"
}
if ($GamePid -gt 0) {
    $processes = @(Get-Process -Id $GamePid -ErrorAction SilentlyContinue)
} else {
    $processes = @(Get-Process -Name FIFA19 -ErrorAction SilentlyContinue)
}
if ($processes.Count -ne 1 -or $processes[0].ProcessName -ne 'FIFA19') {
    throw "Expected exactly one FIFA19.exe at the main menu, found $($processes.Count)."
}

$runner = Join-Path $PSScriptRoot 'run_eaapp_full_server_guarded_at_menu.py'
$arguments = @(
    '-B', $runner, '--pid', $processes[0].Id,
    '--data-root', $DataRoot,
    '--network-log', $networkLog,
    '--stop-file', $stopFile,
    '--account-mode', $AccountMode
)
if ($ManualContinue) {
    $arguments += '--manual-continue'
}
& $PythonExe @arguments 2>&1 | Tee-Object -FilePath $StatusPath
$runnerExitCode = $LASTEXITCODE
if ($runnerExitCode -ne 0) {
    Write-Error "EA App guarded full-server diagnostic failed with exit code $runnerExitCode. Log preserved at $StatusPath"
}
exit $runnerExitCode
