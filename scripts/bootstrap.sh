#!/usr/bin/env bash
# One-button setup on Linux/macOS: venv, package, ffmpeg check, doctor, master.
# Nothing here needs Docker or a GPU. Re-running it is a no-op.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -d .venv ] || python3 -m venv .venv
PY=.venv/bin/python

echo "[1/4] installing vodpipe[server,dev]"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -e ".[server,dev]"

echo "[2/4] ffmpeg"
if FF=$("$PY" -c "import vodpipe.ff as f;print(f.find('ffmpeg'))" 2>/dev/null); then
  echo "  using $FF"
else
  echo "  no ffmpeg: install it (apt/brew) or set VODPIPE_FFMPEG." >&2
  exit 1
fi

echo "[3/4] doctor"
"$PY" -m vodpipe doctor

echo "[4/4] 4K master"
[ -f src_clip/4k_master.mp4 ] || "$PY" -m vodpipe master

echo
echo "Next:  .venv/bin/python -m vodpipe demo"
