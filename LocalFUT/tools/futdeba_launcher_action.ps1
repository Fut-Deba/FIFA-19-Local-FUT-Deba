param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'RemoveHosts', 'CreateShortcut')]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [string]$LogPath,
    [string]$GamePath = '',
    [string]$ProfileId = '',
    [ValidateSet('NORMAL', 'RTG')]
    [string]$AccountMode = 'NORMAL'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$resolvedLog = [IO.Path]::GetFullPath($LogPath)
$logDirectory = Split-Path -Parent $resolvedLog
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

function Write-ActionLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    Add-Content -LiteralPath $resolvedLog -Encoding UTF8 `
        -Value ('{0:o} {1}' -f [DateTimeOffset]::Now, $Message)
}

try {
    Write-ActionLog ("Starting launcher action: {0}" -f $Action)
    if ($Action -eq 'Start') {
        if (-not $GamePath -or -not $ProfileId) {
            throw 'GamePath and ProfileId are required for Start.'
        }
        # Windows PowerShell turns the first stderr line of a native command
        # into a terminating error under 'Stop'. A Python traceback therefore
        # aborted this action on "Traceback (most recent call last):", and the
        # real error at its end never reached the log (v1 tester, 2026-09-13).
        # The child's exit code decides the result instead.
        $ErrorActionPreference = 'Continue'
        & powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass `
            -File (Join-Path $PSScriptRoot 'start_localfut19.ps1') `
            -GamePath $GamePath -ProfileId $ProfileId `
            -AccountMode $AccountMode 2>&1 |
            Tee-Object -FilePath $resolvedLog -Append
        $result = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
    } elseif ($Action -eq 'RemoveHosts') {
        $hostsPath = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'
        $ErrorActionPreference = 'Continue'
        & powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass `
            -File (Join-Path $PSScriptRoot 'remove_localfut_hosts.ps1') `
            -HostsPath $hostsPath 2>&1 |
            Tee-Object -FilePath $resolvedLog -Append
        $result = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($result -eq 0) {
            ipconfig.exe /flushdns | Out-Null
        }
    } else {
        $desktop = [Environment]::GetFolderPath('Desktop')
        if (-not $desktop) {
            throw 'Windows Desktop folder was not found.'
        }
        $shortcutPath = Join-Path $desktop 'FUT Deba Launcher.lnk'
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = Join-Path $root 'FUT_DEBA_LAUNCHER.cmd'
        $shortcut.WorkingDirectory = $root
        $shortcut.IconLocation = ('{0},0' -f (Join-Path $PSScriptRoot `
            'launcher_assets\launcher-icon-v3.ico'))
        $shortcut.Description = 'FUT Deba Local FUT 19 Control'
        $shortcut.Save()
        Write-ActionLog ("Desktop shortcut created: {0}" -f $shortcutPath)
        $result = 0
    }
    Write-ActionLog ("Launcher action finished with exit code {0}." -f $result)
    exit $result
} catch {
    Write-ActionLog ("ERROR: {0}" -f $_.Exception.Message)
    exit 1
}
