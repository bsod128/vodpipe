"""Pick a GPU that can really encode, without hardcoding an index.

Two facts make this necessary:

* **Indexes move.** This machine gained a fourth V100 and the RTX 3060 shifted
  from index 3 to 4. Any code that said `CUDA_VISIBLE_DEVICES=3` broke silently.
* **Datacenter cards have no NVENC block at all.** A Tesla V100 encodes nothing
  with `h264_nvenc`; worse, when its VRAM is already taken the encoder fails with
  `CUDA_ERROR_OUT_OF_MEMORY` rather than a clean "unsupported".

So the answer is not a config value, it is a probe: run a one-second 640x360
encode per card and keep the first card where it worked. The by-product is the
part that matters in production - the *reason* each card was rejected, which goes
into every report and turns a three-hour debugging session into one log line.
"""

from __future__ import annotations

import os
import re
import subprocess

from . import ff

_SMI_QUERY = ["nvidia-smi",
              "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
              "--format=csv,noheader,nounits"]


def probe_gpu_args(codec: str, index: int) -> list[str]:
    """One second of synthetic video through the real encoder on one card."""
    return [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1",
            "-c:v", codec, "-preset", "p5", "-f", "null", "-"]


def parse_smi(stdout: str) -> list[dict]:
    """`nvidia-smi --query-gpu=...` CSV -> [{index,name,used,total,util}]."""
    cards = []
    for line in (stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            cards.append({"index": int(parts[0]), "name": parts[1],
                          "used": int(parts[2]), "total": int(parts[3]),
                          "util": int(parts[4])})
        except ValueError:
            continue
    return cards


def gpus(runner: ff.Runner | None = None) -> list[dict]:
    try:
        res = (runner or ff.Runner()).run(_SMI_QUERY, check=False)
    except (FileNotFoundError, OSError):
        return []
    return parse_smi(res.stdout)


_CONSUMER_RE = re.compile(r"GeForce|RTX|GTX", re.I)


def rank(card: dict) -> tuple:
    """Consumer GPUs first (their NVENC block is guaranteed present), then by
    free VRAM, then by idle-ness.

    Sorting is not cosmetic: probing an exhausted datacenter card first wastes
    seconds and produces a misleading first error in the log.
    """
    consumer = 0 if _CONSUMER_RE.search(card["name"]) else 1
    free = card["total"] - card["used"]
    return (consumer, -free, card["util"])


def probe_index(index: int, codec: str = "h264_nvenc") -> tuple[bool, str]:
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(index))
    proc = subprocess.run(probe_gpu_args(codec, index), capture_output=True,
                          text=True, errors="replace", env=env)
    if proc.returncode == 0:
        return True, "ok"
    return False, " ".join((proc.stderr or "").split())[:200]


_CACHE: dict[str, dict] = {}


def pick(codec: str = "h264_nvenc", set_env: bool = True, refresh: bool = False) -> dict:
    """Find the encoding card and pin it in the environment.

    Cached per codec for the life of the process: probing costs a couple of
    seconds per rejected card, and a worker that re-probed per task would pay it
    thousands of times. Only successes are cached - a card that failed because
    its VRAM was busy may be free later, and remembering that as permanent would
    strand the pool. Force a re-probe with `refresh=True` or
    `VODPIPE_GPU_REFRESH=1`.
    """
    if not refresh and not os.environ.get("VODPIPE_GPU_REFRESH"):
        hit = _CACHE.get(codec)
        if hit is not None:
            if set_env and hit["picked"] is not None:
                os.environ["CUDA_VISIBLE_DEVICES"] = str(hit["picked"])
            return hit

    report: dict = {"codec": codec, "cards": [], "picked": None}
    for card in sorted(gpus(), key=rank):
        ok, reason = probe_index(card["index"], codec)
        report["cards"].append({**card, "nvenc": ok, "reason": reason})
        if ok and report["picked"] is None:
            report["picked"] = card["index"]
            if set_env:
                os.environ["CUDA_VISIBLE_DEVICES"] = str(card["index"])

    if report["picked"] is not None:
        _CACHE[codec] = report
    return report


def summary(report: dict) -> str:
    lines = []
    for c in report.get("cards", []):
        flag = "NVENC OK" if c["nvenc"] else "NVENC FAIL"
        note = "" if c["nvenc"] else f"  <- {c['reason'][:80]}"
        lines.append(f"  GPU {c['index']} {c['name']:<26} "
                     f"free={c['total'] - c['used']:>5}MiB util={c['util']:>3}% {flag}{note}")
    if report.get("picked") is None:
        lines.append("  no card can encode with this codec")
    else:
        lines.append(f"  CUDA_VISIBLE_DEVICES={report['picked']}")
    return "\n".join(lines)
