param(
    [string]$HostsPath = ""
)

$ErrorActionPreference = 'Stop'
if (-not $HostsPath) {
    $HostsPath = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'
}
$resolved = [IO.Path]::GetFullPath($HostsPath)
if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
    throw "The Windows hosts file was not found: $resolved"
}

$encoding = [Text.Encoding]::GetEncoding(28591)
$originalBytes = [IO.File]::ReadAllBytes($resolved)
$originalText = $encoding.GetString($originalBytes)
# One token list for removing and for verifying. Pinning the removal to the
# full hostname "spring18.gosredirector.ea.com" while verifying against the
# bare token left a line written without that prefix in place, and the script
# then reported entries remaining right after saying the file was clean.
$tokens = 'LOCALFUT19|gosredirector|gosca18'
$targetLine = '(?im)^[^\r\n]*(?:' + $tokens + ')[^\r\n]*(?:\r\n|\n|\r|$)'
$cleanText = [Text.RegularExpressions.Regex]::Replace(
    $originalText, $targetLine, '')
# If our lines were the only thing in the file, write the stock Windows
# content rather than leaving it empty. An empty hosts file is what an
# interrupted shutdown leaves behind, and the launcher refuses to restore one,
# so cleaning to empty would hand the user the next failure instead of fixing
# this one. Seen on the reference machine on 2026-09-03: hosts was 113 bytes
# holding nothing but the certificate probe block.
if ($cleanText.Trim().Length -eq 0) {
    $cleanText = @'
# Copyright (c) 1993-2009 Microsoft Corp.
#
# This is a sample HOSTS file used by Microsoft TCP/IP for Windows.
#
# This file contains the mappings of IP addresses to host names. Each
# entry should be kept on an individual line. The IP address should
# be placed in the first column followed by the corresponding host name.
# The IP address and the host name should be separated by at least one
# space.
#
# Additionally, comments (such as these) may be inserted on individual
# lines or following the machine name denoted by a '#' symbol.
#
# For example:
#
#      102.54.94.97     rhino.acme.com          # source server
#       38.25.63.10     x.acme.com              # x client host

# localhost name resolution is handled within DNS itself.
#	127.0.0.1       localhost
#	::1             localhost
'@
    Write-Host 'hosts held nothing but LocalFUT19 entries; stock content restored.'
}
if ($cleanText -ne $originalText) {
    [IO.File]::WriteAllBytes($resolved, $encoding.GetBytes($cleanText))
}

# A snapshot of the original hosts survives an interrupted shutdown. Once
# hosts is clean it has nothing left to restore, and leaving it behind makes
# the next launch try to recover a state that no longer exists.
$dataRoot = Join-Path $env:LOCALAPPDATA 'FIFA19LocalFUT'
if (Test-Path -LiteralPath $dataRoot) {
    Get-ChildItem -LiteralPath $dataRoot -Recurse -Force -File `
        -Filter 'eaapp-full-hosts.original.bin' -ErrorAction SilentlyContinue |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Force
            Write-Host ("Removed stale hosts snapshot: " + $_.FullName)
        }
}

# Verified with the same token list that did the removing, so the check can
# never contradict the result.
$remaining = [Text.RegularExpressions.Regex]::IsMatch($cleanText, '(?i)' + $tokens)
if ($remaining) {
    Write-Host 'These lines could not be removed automatically:'
    foreach ($line in ($cleanText -split "`r`n|`n|`r")) {
        if ($line -match ('(?i)' + $tokens)) { Write-Host ("  " + $line) }
    }
    throw 'One or more LocalFUT19 entries remain in the hosts file.'
}

# A session that did not end normally can still hold another FUT tool's
# redirect lines, set aside while it ran. They go back into hosts now.
if (Test-Path -LiteralPath (Join-Path $dataRoot 'hosts-set-aside.json')) {
    $python = Join-Path (Split-Path $PSScriptRoot -Parent) '.runtime\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        $python = (Get-Command python.exe -ErrorAction Stop).Source
    }
    & $python -B (Join-Path $PSScriptRoot 'session_preflight.py') finish `
        --data-root $dataRoot --hosts $resolved
    if ($LASTEXITCODE -ne 0) {
        throw 'The redirect lines of another FUT tool could not be put back.'
    }
}

Write-Host 'LocalFUT19 hosts cleanup completed.'
