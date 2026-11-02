# One-button setup on Windows: venv, package, ffmpeg check, doctor, sample master.
# Nothing here needs Docker or a GPU. Re-running it is a no-op.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) { python -m venv .venv }
$py = Join-Path $root ".venv\Scripts\python.exe"

Write-Host "[1/4] installing vodpipe[server,dev]"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -e ".[server,dev]"

Write-Host "[2/4] ffmpeg"
# vodpipe finds it via VODPIPE_FFMPEG, then .\bin, then PATH. We do not
# download a ~100 MB binary without asking; scripts\fetch-ffmpeg.ps1 does that.
$ff = & $py -c "import vodpipe.ff as f;print(f.find('ffmpeg'))" 2>$null
if (-not $ff) {
    Write-Host "  no ffmpeg: put ffmpeg.exe and ffprobe.exe in .\bin, set VODPIPE_FFMPEG," -ForegroundColor Yellow
    Write-Host "  or run scripts\fetch-ffmpeg.ps1 (downloads ~100 MB)."
    exit 1
}
Write-Host "  using $ff"

Write-Host "[3/4] doctor"
& $py -m vodpipe doctor

Write-Host "[4/4] 4K master"
if (-not (Test-Path "src_clip\4k_master.mp4")) { & $py -m vodpipe master }

Write-Host ""
Write-Host "Next:  .venv\Scripts\python.exe -m vodpipe demo"
