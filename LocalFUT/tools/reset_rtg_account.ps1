param(
    [string]$GamePath = "",
    [string]$ProfileId = ""
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

if (@(Get-Process -Name FIFA19 -ErrorAction SilentlyContinue).Count -gt 0) {
    throw 'Close FIFA 19 before resetting the RTG account.'
}
$listener = $null
try {
    $listener = Get-NetTCPConnection -LocalPort 8199 -State Listen `
        -ErrorAction SilentlyContinue
} catch {
    # Older Windows builds may not provide Get-NetTCPConnection. FIFA being
    # closed remains the primary safety gate and SQLite backup is mandatory.
}
if ($listener) {
    throw 'Close the LocalFUT19 launcher before resetting the RTG account.'
}

$privatePython = Join-Path $root '.runtime\Scripts\python.exe'
if (Test-Path -LiteralPath $privatePython -PathType Leaf) {
    $python = $privatePython
} else {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Python was not found. Start FUT_DEBA_LAUNCHER.cmd first; it prepares the runtime.'
    }
    $python = $pythonCommand.Source
}

$detectArguments = @('-B', (Join-Path $PSScriptRoot 'detect_fifa19_build.py'),
                     '--json')
if ($GamePath) {
    $detectArguments += @('--game', $GamePath, '--explicit-only')
}
if ($ProfileId) {
    $detectArguments += @('--profile-id', $ProfileId)
}
$detectionText = (& $python @detectArguments 2>&1) -join "`n"
try {
    $detection = $detectionText | ConvertFrom-Json
} catch {
    throw "Build detector did not return valid JSON:`n$detectionText"
}
if ($detection.decision -ne 'selected') {
    throw $detection.message
}
$selected = $detection.selected

if ($env:LOCALFUT19_DATA_ROOT) {
    $baseDataRoot = [IO.Path]::GetFullPath($env:LOCALFUT19_DATA_ROOT)
} else {
    $baseDataRoot = Join-Path $env:LOCALAPPDATA 'FIFA19LocalFUT'
}
if ($selected.launchStrategy -eq 'eaapp-menu-guarded-bridge') {
    $dataRoot = Join-Path $baseDataRoot 'profiles\eaapp-4052077-rtg'
} elseif ($selected.launchStrategy -eq 'legacy-native-server') {
    # Never reset the historical root-level v1 database implicitly; it is a
    # migration source and must remain untouched until explicitly imported.
    $dataRoot = Join-Path $baseDataRoot 'profiles\v1-3865658-rtg'
} else {
    throw "Unknown launch strategy: $($selected.launchStrategy)"
}
$database = Join-Path $dataRoot 'fut19-rtg.sqlite3'

Write-Host ''
Write-Host 'Protected RTG account reset'
Write-Host ("Build: {0}" -f $selected.displayName)
Write-Host ("Database: {0}" -f $database)
Write-Host ''
Write-Host 'A verified SQLite backup is created before any account data changes.'
Write-Host 'The Normal account is not modified.'
$confirmation = Read-Host 'Type RESET RTG to continue'
if ($confirmation -cne 'RESET RTG') {
    Write-Host 'Reset cancelled. No files were changed.'
    exit 0
}

New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
$env:LOCALFUT19_DATA_ROOT = $dataRoot
$env:LOCALFUT19_PROFILE = 'RTG'
$env:LOCALFUT19_RTG_MODE = '1'
& $python -B (Join-Path $root 'server\fut_tools.py') resetaccount
if ($LASTEXITCODE -ne 0) {
    throw "RTG reset failed with exit code $LASTEXITCODE."
}
Write-Host 'RTG account reset completed. The backup path is printed above.'
