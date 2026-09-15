param(
    [string]$CacheRoot = "",
    [string]$BackupRoot = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))

if ([string]::IsNullOrWhiteSpace($CacheRoot)) {
    $documents = [Environment]::GetFolderPath("MyDocuments")
    $CacheRoot = Join-Path $documents "FIFA 19\filesystemcache"
}
$CacheRoot = [System.IO.Path]::GetFullPath($CacheRoot)

if ([string]::IsNullOrWhiteSpace($BackupRoot)) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $BackupRoot = Join-Path $projectRoot "backups\filesystemcache_invalid\$stamp"
}
$BackupRoot = [System.IO.Path]::GetFullPath($BackupRoot)

$allowedBackupRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $projectRoot "backups\filesystemcache_invalid"))
if (-not $BackupRoot.StartsWith(
        $allowedBackupRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup destination is outside the allowed project folder: $BackupRoot"
}

if (-not (Test-Path -LiteralPath $CacheRoot)) {
    Write-Host "[cache] filesystemcache is absent; no cleanup is required."
    exit 0
}

$names = @(
    "atlItemRarityImagecollapsed",
    "atlItemRarityImagelarge",
    "atlItemRarityImagesmall"
)
$invalid = @()
$imageCount = 0
foreach ($name in $names) {
    $source = [System.IO.Path]::GetFullPath((Join-Path $CacheRoot $name))
    if (-not $source.StartsWith(
            $CacheRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Cache source is outside the allowed folder: $source"
    }
    if (Test-Path -LiteralPath $source) {
        $images = @(Get-ChildItem -LiteralPath $source -File -Filter "*.dds")
        $imageCount += $images.Count
        $invalid += $images | Where-Object { $_.Length -le 2 }
    }
}

$rarityJsonRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $CacheRoot "atlItemRarityJson"))
$rarityManifest = [System.IO.Path]::GetFullPath(
    (Join-Path $rarityJsonRoot "cachedrarities.json"))
if (-not $rarityManifest.StartsWith(
        $CacheRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Rarity manifest is outside the allowed cache folder: $rarityManifest"
}

# A previous recovery may already have removed the corrupt two-byte images
# while leaving cachedrarities.json behind.  With all rarity image folders
# empty, that manifest falsely tells the retail client that promo backgrounds
# are cached, so it never asks the local content server for fresh DDS files.
$staleRarityManifest = ((Test-Path -LiteralPath $rarityManifest) -and
                        ($imageCount -eq 0))
$moveRarityManifest = ((Test-Path -LiteralPath $rarityManifest) -and
                       (($invalid.Count -gt 0) -or $staleRarityManifest))

if (($invalid.Count -eq 0) -and (-not $moveRarityManifest)) {
    Write-Host "[cache] no invalid DDS files or stale rarity manifest found."
    exit 0
}

if (Get-Process -Name "FIFA19" -ErrorAction SilentlyContinue) {
    $message = (("FIFA19.exe is still open; cache recovery has {0} invalid DDS files " +
                 "and staleManifest={1}. " +
                 "Close the game and restart PLAY_FUT19_LOCAL.cmd.") -f
                $invalid.Count, $moveRarityManifest)
    Write-Host ("[cache] ERROR: " + $message) -ForegroundColor Red
    exit 11
}

foreach ($file in $invalid) {
    $destinationDirectory = Join-Path $BackupRoot $file.Directory.Name
    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
    Move-Item -LiteralPath $file.FullName -Destination (
        Join-Path $destinationDirectory $file.Name)
}

if ($moveRarityManifest) {
    $manifestDestination = Join-Path $BackupRoot "atlItemRarityJson"
    New-Item -ItemType Directory -Path $manifestDestination -Force | Out-Null
    Move-Item -LiteralPath $rarityManifest -Destination (
        Join-Path $manifestDestination "cachedrarities.json")
}

Write-Host ("[cache] moved {0} invalid DDS files and rarityManifest={1} to backup: {2}" -f
            $invalid.Count, $moveRarityManifest, $BackupRoot)
exit 0
