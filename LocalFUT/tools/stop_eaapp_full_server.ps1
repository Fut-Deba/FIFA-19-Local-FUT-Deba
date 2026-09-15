param([string]$DataRoot = "")
if (-not $DataRoot) {
    if ($env:LOCALFUT19_DATA_ROOT) {
        $DataRoot = $env:LOCALFUT19_DATA_ROOT
    } else {
        $DataRoot = Join-Path $env:LOCALAPPDATA `
            'FIFA19LocalFUT\profiles\eaapp-4052077'
    }
}
$stopFile = Join-Path ([IO.Path]::GetFullPath($DataRoot)) 'eaapp-full.stop'
$directory = Split-Path $stopFile -Parent
New-Item -ItemType Directory -Force -Path $directory | Out-Null
[IO.File]::WriteAllText(
    $stopFile,
    [DateTimeOffset]::UtcNow.ToString('O'),
    [Text.UTF8Encoding]::new($false)
)
Write-Host "EA App full-server stop requested: $stopFile"
