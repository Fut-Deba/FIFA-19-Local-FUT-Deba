param(
    [string]$GamePath = "",
    [string]$ProfileId = "",
    [string]$AccountMode = ""
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

function Disable-ConsoleSelectionPause {
    try {
        if (-not ('LocalFutConsoleMode' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class LocalFutConsoleMode
{
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr GetStdHandle(int handleId);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool GetConsoleMode(IntPtr handle, out uint mode);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetConsoleMode(IntPtr handle, uint mode);

    [DllImport("kernel32.dll")]
    public static extern IntPtr GetConsoleWindow();

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool RedrawWindow(
        IntPtr window, IntPtr updateRect, IntPtr updateRegion, uint flags);

    public static void Redraw()
    {
        IntPtr window = GetConsoleWindow();
        if (window != IntPtr.Zero)
        {
            RedrawWindow(window, IntPtr.Zero, IntPtr.Zero, 0x0585);
        }
    }
}
'@
        }
        $inputHandle = [LocalFutConsoleMode]::GetStdHandle(-10)
        [uint32]$consoleMode = 0
        if ([LocalFutConsoleMode]::GetConsoleMode(
                $inputHandle, [ref]$consoleMode)) {
            $consoleMode = [uint32](
                ($consoleMode -bor 0x0080) -band 0xFFFFFFBF
            )
            [void][LocalFutConsoleMode]::SetConsoleMode(
                $inputHandle, $consoleMode)
        }
    } catch {
        # Console mode support varies by host; launch must remain available.
    }
}

Disable-ConsoleSelectionPause

function Write-LauncherStatus {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host $Message
    try {
        [Console]::Out.Flush()
        [LocalFutConsoleMode]::Redraw()
    } catch {}
}

function Enable-FifaConfigAutoLaunch {
    param([Parameter(Mandatory = $true)][string]$GameDirectory)

    $configPath = Join-Path $GameDirectory 'FIFASetup\config.ini'
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        Write-LauncherStatus ('Official launcher auto-start is unavailable; ' +
            'use Play when the FIFA 19 launcher appears.')
        return $null
    }

    try {
        [byte[]]$originalBytes = [IO.File]::ReadAllBytes($configPath)
        $text = [Text.Encoding]::UTF8.GetString($originalBytes)
        $newline = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
        if ($text -match '(?im)^[ \t]*AUTO_LAUNCH[ \t]*=') {
            $updated = [regex]::Replace(
                $text,
                '(?im)^[ \t]*AUTO_LAUNCH[ \t]*=.*$',
                'AUTO_LAUNCH = 1'
            )
        } else {
            $updated = $text
            if ($updated.Length -gt 0 -and
                -not $updated.EndsWith("`n") -and
                -not $updated.EndsWith("`r")) {
                $updated += $newline
            }
            $updated += 'AUTO_LAUNCH = 1' + $newline
        }
        [IO.File]::WriteAllBytes(
            $configPath,
            [Text.Encoding]::UTF8.GetBytes($updated)
        )
        Write-LauncherStatus 'Official FIFA 19 launcher auto-start is active.'
        return [pscustomobject]@{
            Path = $configPath
            Bytes = $originalBytes
        }
    } catch {
        Write-LauncherStatus ('Official launcher auto-start could not be enabled; ' +
            'use Play when the FIFA 19 launcher appears.')
        return $null
    }
}

function Restore-FifaConfigAfterAutoLaunch {
    param($Snapshot)

    if ($null -eq $Snapshot) {
        return
    }
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            [IO.File]::WriteAllBytes(
                [string]$Snapshot.Path,
                [byte[]]$Snapshot.Bytes
            )
            Write-LauncherStatus 'Official FIFA 19 launcher settings restored.'
            return
        } catch {
            if ($attempt -eq 5) {
                throw ('Could not restore the official FIFA 19 launcher settings: ' +
                       $_.Exception.Message)
            }
            Start-Sleep -Milliseconds 250
        }
    }
}

function Get-SelectedFifaProcesses {
    param([Parameter(Mandatory = $true)][string]$Executable)

    $expectedPath = [IO.Path]::GetFullPath($Executable)
    $matches = @()
    foreach ($candidate in @(Get-Process -Name FIFA19 -ErrorAction SilentlyContinue)) {
        try {
            if (-not $candidate.HasExited -and
                [IO.Path]::GetFullPath($candidate.Path) -eq $expectedPath) {
                $matches += $candidate
            }
        } catch {}
    }
    return $matches
}

function Test-EaAppBridgeProcessReady {
    param([Parameter(Mandatory = $true)]$Process)

    $readiness = Get-EaAppBridgeProcessReadiness -Process $Process
    return [bool]$readiness.Ready
}

function Get-EaAppBridgeProcessReadiness {
    param([Parameter(Mandatory = $true)]$Process)

    try {
        $Process.Refresh()
        if ($Process.HasExited -or -not $Process.Responding) {
            return [pscustomobject]@{
                Ready = $false
                Stage = 'process'
                Summary = 'waiting for FIFA 19 to respond'
            }
        }
        $ageSeconds = ((Get-Date) - $Process.StartTime).TotalSeconds
        $moduleCount = $Process.Modules.Count
        $threadCount = $Process.Threads.Count
        $handleCount = $Process.HandleCount
        $runtimeReady = (
            $ageSeconds -ge 8 -and
            $moduleCount -ge 100 -and
            $threadCount -ge 60 -and
            $handleCount -ge 600
        )
        $ready = $runtimeReady

        if (-not $runtimeReady) {
            $remainingSeconds = [math]::Max(0, [math]::Ceiling(8 - $ageSeconds))
            if ($moduleCount -ge 100 -and
                $threadCount -ge 60 -and
                $handleCount -ge 600) {
                $summary = ('game runtime loaded; stabilizing (about {0}s)' -f
                    $remainingSeconds)
            } else {
                $summary = 'FIFA 19 is still loading'
            }
        } else {
            $summary = 'FIFA 19 runtime is ready for the local connection'
        }

        return [pscustomobject]@{
            Ready = $ready
            Stage = $(if ($ready) { 'ready' } else { 'runtime' })
            Summary = $summary
        }
    } catch {
        return [pscustomobject]@{
            Ready = $false
            Stage = 'metrics'
            Summary = ('reading FIFA 19 runtime state ({0})' -f
                $_.Exception.GetType().Name)
        }
    }
}

function Wait-ForEaAppBridgeProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [int]$TimeoutSeconds = 360,
        [string]$SignalPath = '',
        [Parameter(Mandatory = $true)][ref]$ManualContinue
    )

    $ManualContinue.Value = $false
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $announcedPid = 0
    $stablePid = 0
    $stableSamples = 0
    $handoffPid = 0
    $handoffStableSamples = 0
    $lastProcessSeenAt = $null
    $lastPendingStage = ''
    $lastPendingStatusAt = [datetime]::MinValue
    Write-LauncherStatus 'Automatic local connection preparation is active.'
    Write-LauncherStatus 'Complete any EA App prompt; the launcher will continue automatically.'

    # Clear a stale manual-continue signal so a previous attempt cannot
    # auto-continue this one.
    if ($SignalPath -and (Test-Path -LiteralPath $SignalPath)) {
        Remove-Item -LiteralPath $SignalPath -Force -ErrorAction SilentlyContinue
    }

    while ((Get-Date) -lt $deadline) {
        # Manual override: on some PCs the automatic readiness check never
        # recognizes a fully-loaded FIFA 19, so the game sits at the menu while
        # this wait times out (a123, 2026-09-15). When the user confirms the
        # game is at the main menu, connect to the real game process - the one
        # with the largest working set - regardless of the readiness heuristic.
        if ($SignalPath -and (Test-Path -LiteralPath $SignalPath)) {
            Remove-Item -LiteralPath $SignalPath -Force -ErrorAction SilentlyContinue
            $manual = @(Get-SelectedFifaProcesses -Executable $Executable) |
                Sort-Object WorkingSet64 -Descending | Select-Object -First 1
            if ($manual) {
                $ManualContinue.Value = $true
                Write-LauncherStatus 'Manual continue: connecting to FIFA 19 at the main menu.'
                return $manual
            }
            Write-LauncherStatus 'Manual continue requested, but FIFA 19 is not running yet; still waiting.'
        }
        $candidates = @(Get-SelectedFifaProcesses -Executable $Executable)
        if ($candidates.Count -ne 1) {
            $stablePid = 0
            $stableSamples = 0
            $handoffStableSamples = 0
            $now = Get-Date
            $pendingStage = if ($lastProcessSeenAt) { 'handoff' } else { 'activation' }
            $pendingSummary = if ($lastProcessSeenAt) {
                'Waiting for the official launcher to start the final FIFA 19 game process'
            } else {
                'Waiting for EA App and the official FIFA 19 launcher'
            }
            if ($pendingStage -ne $lastPendingStage -or
                ($now - $lastPendingStatusAt).TotalSeconds -ge 20) {
                Write-LauncherStatus ($pendingSummary + '.')
                $lastPendingStage = $pendingStage
                $lastPendingStatusAt = $now
            }
            Start-Sleep -Seconds 1
            continue
        }
        $candidate = $candidates[0]
        $lastProcessSeenAt = Get-Date
        if ($candidate.Id -ne $announcedPid) {
            if ($announcedPid -eq 0) {
                Write-LauncherStatus 'FIFA 19 detected. Waiting for the game to finish loading...'
            } else {
                Write-LauncherStatus 'Final FIFA 19 game process detected. Continuing automatically...'
                $handoffPid = $candidate.Id
            }
            $announcedPid = $candidate.Id
            $stablePid = $candidate.Id
            $stableSamples = 0
            $handoffStableSamples = 0
            $lastPendingStage = ''
            $lastPendingStatusAt = [datetime]::MinValue
        }

        $readiness = Get-EaAppBridgeProcessReadiness -Process $candidate
        if ($readiness.Ready) {
            if ($stablePid -ne $candidate.Id) {
                $stablePid = $candidate.Id
                $stableSamples = 0
            }
            $stableSamples++
            if ($stableSamples -eq 1) {
                Write-LauncherStatus 'FIFA 19 runtime detected; confirming process stability...'
            }
            if ($stableSamples -ge 2) {
                Write-LauncherStatus 'Local connection preparation can start safely.'
                return $candidate
            }
        } else {
            $stableSamples = 0
            if ($candidate.Id -eq $handoffPid) {
                $handoffStableSamples++
                if ($handoffStableSamples -ge 8) {
                    Write-LauncherStatus ('Final FIFA 19 process is stable; ' +
                        'starting local connection preparation before the main menu.')
                    return $candidate
                }
            }
            $now = Get-Date
            if ($readiness.Stage -ne $lastPendingStage -or
                ($now - $lastPendingStatusAt).TotalSeconds -ge 20) {
                Write-LauncherStatus ('Waiting: ' + $readiness.Summary + '.')
                $lastPendingStage = $readiness.Stage
                $lastPendingStatusAt = $now
            }
        }
        Start-Sleep -Seconds 1
    }
    throw ('FIFA 19 did not become ready for local connection preparation within ' +
           $TimeoutSeconds + ' seconds. No redirect was installed.')
}

if (-not $AccountMode) {
    if ($env:LOCALFUT19_PROFILE -eq 'RTG' -or
        $env:LOCALFUT19_RTG_MODE -match '^(?i:1|true|yes|on)$') {
        $AccountMode = 'RTG'
    } else {
        $AccountMode = 'NORMAL'
    }
}
if ($AccountMode -notin @('NORMAL', 'RTG')) {
    throw "Unknown account mode: $AccountMode"
}
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run PLAY_FUT19_LOCAL.cmd as Administrator.'
}

$privatePython = Join-Path $root '.runtime\Scripts\python.exe'
if (Test-Path -LiteralPath $privatePython -PathType Leaf) {
    $python = $privatePython
} else {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Python was not found. Start FUT_DEBA_LAUNCHER.cmd from the package root first; it prepares the runtime.'
    }
    $python = $pythonCommand.Source
}

Write-Host 'Checking LocalFUT19 runtime assets...'
& $python -B (Join-Path $PSScriptRoot 'beta_readiness.py')
if ($LASTEXITCODE -ne 0) {
    throw 'The LocalFUT19 runtime preflight failed.'
}

$detectArguments = @('-B', (Join-Path $PSScriptRoot 'detect_fifa19_build.py'), '--json')
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

if ($detection.decision -eq 'ambiguous') {
    Write-Host ''
    Write-Host 'Multiple exact FIFA 19 installations were found:'
    for ($index = 0; $index -lt $detection.installations.Count; $index++) {
        $row = $detection.installations[$index]
        if ($row.known) {
            Write-Host ("  [{0}] {1}" -f ($index + 1), $row.displayName)
            Write-Host ("      {0}" -f $row.gameDirectory)
        }
    }
    $answer = Read-Host 'Choose the installation number'
    $selectedIndex = 0
    if (-not [int]::TryParse($answer, [ref]$selectedIndex) -or
        $selectedIndex -lt 1 -or
        $selectedIndex -gt $detection.installations.Count -or
        -not $detection.installations[$selectedIndex - 1].known) {
        throw 'No valid FIFA 19 installation was selected.'
    }
    $selected = $detection.installations[$selectedIndex - 1]
} elseif ($detection.decision -eq 'selected') {
    $selected = $detection.selected
} else {
    Write-Host ''
    Write-Host 'No safe launch strategy was selected.' -ForegroundColor Red
    foreach ($row in $detection.installations) {
        Write-Host ("  {0}: {1}" -f $row.gameDirectory, $row.reason)
        Write-Host ("  {0}" -f $row.fingerprintSummary)
    }
    throw $detection.message
}

Write-Host ''
Write-Host ("Detected: {0}" -f $selected.displayName)
Write-Host ("Strategy: {0} [{1}]" -f $selected.launchStrategy, $selected.supportStatus)
Write-Host ("Game: {0}" -f $selected.executable)

$runningFifa = @(Get-Process -Name FIFA19 -ErrorAction SilentlyContinue)
if ($runningFifa.Count -gt 0) {
    throw 'Close every FIFA19.exe process before using the unified launcher.'
}

if ($env:LOCALFUT19_DATA_ROOT) {
    $baseDataRoot = [IO.Path]::GetFullPath($env:LOCALFUT19_DATA_ROOT)
} else {
    $baseDataRoot = Join-Path $env:LOCALAPPDATA 'FIFA19LocalFUT'
}

# Other FUT revival tools redirect the same EA hostname and listen on the same
# local ports. Before anything changes, stop with the name of a program that
# holds a port, and set the other tool's redirect lines aside. They go back
# when the session ends, also when it fails.
$preflight = Join-Path $PSScriptRoot 'session_preflight.py'
$ErrorActionPreference = 'Continue'
$prepared = @(& $python -B $preflight prepare --data-root $baseDataRoot 2>&1 |
    ForEach-Object { [string]$_ })
$preparedExitCode = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
$prepared | ForEach-Object { Write-Host $_ }
if ($preparedExitCode -ne 0) {
    $reasons = @($prepared | Where-Object { $_.Trim() })
    if ($reasons.Count -eq 0) {
        throw 'The session preflight stopped without a reason.'
    }
    throw $reasons[-1]
}

$sessionExitCode = 1
try {
    switch ($selected.launchStrategy) {
        'legacy-native-server' {
            # Historical v1 builds wrote directly below the shared root. Keep the
            # current account isolated until the owner explicitly runs a verified,
            # backup-first migration of that older database.
            $v1ProfileDirectory = if ($AccountMode -eq 'RTG') {
                'profiles\v1-3865658-rtg'
            } else {
                'profiles\v1-3865658'
            }
            $v1DataRoot = Join-Path $baseDataRoot $v1ProfileDirectory
            & $python -B (Join-Path $PSScriptRoot 'run_legacy_full_server.py') `
                --game $selected.executable `
                --data-root $v1DataRoot `
                --account-mode $AccountMode
            $sessionExitCode = $LASTEXITCODE
        }
        'eaapp-menu-guarded-bridge' {
            $hostsPath = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'
            $hostsText = [IO.File]::ReadAllText($hostsPath, [Text.Encoding]::ASCII)
            if ($hostsText -match '(?i)spring18\.gosredirector\.ea\.com|gosca18\.ea\.com') {
                # A session that is killed instead of stopped leaves its redirect
                # behind, and the next launch then refuses to start even though
                # nothing is wrong. Remove that leftover only when it is exactly
                # the block this launcher writes: one marker pair, the exact
                # suffix, and no other occurrence of the hostnames anywhere else.
                # Every FIFA19.exe is already known to be closed at this point, so
                # no live session can own it. Anything unexpected still refuses,
                # because the guard exists to keep EA App able to reach the real
                # activation servers.
                $strandedSuffix = "`r`n`r`n# BEGIN LOCALFUT19_EAAPP_CERT_PROBE`r`n127.0.0.1 spring18.gosredirector.ea.com`r`n# END LOCALFUT19_EAAPP_CERT_PROBE`r`n"
                $beginCount = [regex]::Matches(
                    $hostsText, 'BEGIN LOCALFUT19_EAAPP_CERT_PROBE').Count
                $endCount = [regex]::Matches(
                    $hostsText, 'END LOCALFUT19_EAAPP_CERT_PROBE').Count
                $repaired = $false
                if ($beginCount -eq 1 -and $endCount -eq 1 -and
                        $hostsText.EndsWith(
                            $strandedSuffix, [StringComparison]::Ordinal)) {
                    $preserved = $hostsText.Substring(
                        0, $hostsText.Length - $strandedSuffix.Length)
                    if ($preserved -notmatch
                            '(?i)spring18\.gosredirector\.ea\.com|gosca18\.ea\.com') {
                        [IO.File]::WriteAllBytes(
                            $hostsPath,
                            [Text.Encoding]::ASCII.GetBytes($preserved + "`r`n"))
                        ipconfig /flushdns | Out-Null
                        $verify = [IO.File]::ReadAllText(
                            $hostsPath, [Text.Encoding]::ASCII)
                        if ($verify -match ('(?i)LOCALFUT19_EAAPP_CERT_PROBE|' +
                                'spring18\.gosredirector\.ea\.com|' +
                                'gosca18\.ea\.com')) {
                            throw ('The leftover Local FUT redirect could not be ' +
                                   'removed. Run ADVANCED\CLEANUP_OLD_REDIRECTS.cmd once, ' +
                                   'then retry.')
                        }
                        Write-Host ('Removed a leftover Local FUT redirect from a ' +
                                    'previous session that did not stop cleanly.')
                        $repaired = $true
                    }
                }
                if (-not $repaired) {
                    throw ('EA App must complete activation before any redirect. ' +
                           'Run ADVANCED\CLEANUP_OLD_REDIRECTS.cmd once, then retry.')
                }
            }
            Write-Host ''
            Write-Host 'Starting FIFA 19 through its official activation path...'
            $autoLaunchSnapshot = $null
            try {
                $autoLaunchSnapshot = Enable-FifaConfigAutoLaunch `
                    -GameDirectory $selected.gameDirectory
                Start-Process -FilePath $selected.executable `
                    -WorkingDirectory $selected.gameDirectory | Out-Null
                $manualContinue = $false
                $gameProcess = Wait-ForEaAppBridgeProcess `
                    -Executable $selected.executable `
                    -SignalPath (Join-Path $baseDataRoot 'eaapp-continue.signal') `
                    -ManualContinue ([ref]$manualContinue)
            } finally {
                Restore-FifaConfigAfterAutoLaunch -Snapshot $autoLaunchSnapshot
            }
            Write-Host ("Using live FIFA19.exe process {0}." -f $gameProcess.Id)
            $eaProfileDirectory = if ($AccountMode -eq 'RTG') {
                'profiles\eaapp-4052077-rtg'
            } else {
                'profiles\eaapp-4052077'
            }
            $eaDataRoot = Join-Path $baseDataRoot $eaProfileDirectory
            $bridgeArguments = @(
                '-NoProfile',
                '-ExecutionPolicy', 'Bypass',
                '-File', (Join-Path $PSScriptRoot 'run_eaapp_full_server_guarded_at_menu.ps1'),
                '-DataRoot', $eaDataRoot,
                '-PythonExe', $python,
                '-GamePid', $gameProcess.Id,
                '-AccountMode', $AccountMode
            )
            if ($manualContinue) {
                $bridgeArguments += '-ManualContinue'
            }
            & powershell @bridgeArguments
            $sessionExitCode = $LASTEXITCODE
        }
        default {
            throw "Unknown launch strategy: $($selected.launchStrategy)"
        }
    }
} finally {
    $ErrorActionPreference = 'Continue'
    & $python -B $preflight finish --data-root $baseDataRoot 2>&1 |
        ForEach-Object { Write-Host ([string]$_) }
    $ErrorActionPreference = 'Stop'
}
exit $sessionExitCode
