# Download the ffmpeg release build into .\bin, where find() picks it up.
# Separate from bootstrap.ps1 on purpose: it pulls ~100 MB, so the caller
# decides whether that is acceptable.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bin = Join-Path $root "bin"
New-Item -ItemType Directory -Force $bin | Out-Null

$url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$zip = Join-Path $env:TEMP "ffmpeg-release-essentials.zip"
Write-Host "downloading $url"
Invoke-WebRequest -Uri $url -OutFile $zip

$tmp = Join-Path $env:TEMP "ffmpeg-extract"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
Expand-Archive -Path $zip -DestinationPath $tmp

Get-ChildItem $tmp -Recurse -Filter "ff*.exe" |
    Where-Object { $_.Directory.Name -eq "bin" } |
    ForEach-Object { Copy-Item $_.FullName $bin -Force }

Get-ChildItem $bin -Filter "ff*.exe" | ForEach-Object { Write-Host "  $($_.Name)" }
Remove-Item -Recurse -Force $tmp, $zip -ErrorAction SilentlyContinue
