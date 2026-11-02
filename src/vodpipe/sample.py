"""Reproducible test media and disk guards.

Benchmarks are only worth anything if someone else can reproduce them, which
means they must not depend on a file that lives on my D: drive. Two options, and
both are supported:

* `vodpipe master --synthetic` builds a deterministic 4K clip from lavfi
  (`testsrc2` + moving gradients + noise). Same seed, same frames, no download,
  no licensing question. It is synthetic, so absolute VMAF numbers differ from a
  film master - the *ratios* between encoders and watermark modes are what carry.
* `vodpipe master --url ...` fetches a real clip you have the rights to.

The disk guard exists because 4K ladders fill drives: a 24 s 4K master here is
620 MB, and a five-rung ladder plus chunks and VMAF logs multiplies that. A run
that dies at 90% from ENOSPC produces a corrupt file rather than an error, which
is the worst possible failure mode for a media pipeline.
"""

from __future__ import annotations

import os
import shutil
import urllib.request

from . import ff

#: Free space required before a run, GB, per rung-class of work.
MIN_FREE_GB_4K = 4.0


def master_args(out: str, duration: float = 24.0, size: str = "3840x2160",
                fps: int = 30, bitrate: int = 220_000_000, seed: int = 42) -> list[str]:
    """argv for a deterministic, encoding-demanding 4K master.

    Content choices are deliberate: `testsrc2` gives fine detail and moving
    structures (flat colour would let any encoder look good), a second gradient
    layer pans to create motion that rate control must chase, and `noise`
    emulates film grain, which is what actually exposes encoder differences.
    IDR interval of 2 s matches `encoders.gop_args` so chunk boundaries exist.
    """
    graph = (
        f"testsrc2=size={size}:rate={fps}:duration={duration}:max_dropout=0.005,"
        f"gradients=size={size}:rate={fps}:duration={duration}:nb_colors=6:speed=0.6:seed={seed},"
        f"blend=all_mode=screen:all_opacity=0.35,"
        f"noise=alls=6:allf=t+u,"
        f"format=yuv420p,setsar=1"
    )
    audio = f"aevalsrc=0.25*sin(880*2*PI*t)+0.1*sin(220*2*PI*t):s=48000:d={duration}"
    return [
        ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-stats",
        "-f", "lavfi", "-i", graph,
        "-f", "lavfi", "-i", audio,
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(bitrate),
        "-maxrate:v", str(bitrate), "-bufsize:v", str(bitrate * 2),
        "-pix_fmt", "yuv420p",
        "-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "+faststart",
        out,
    ]


def make_master(out: str, duration: float = 24.0, size: str = "3840x2160",
                fps: int = 30, seed: int = 42, runner: ff.Runner | None = None) -> str:
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    (runner or ff.Runner()).run(master_args(out, duration, size, fps, seed=seed))
    return out


def fetch(url: str, out: str, timeout: int = 120) -> str:
    """Download a source clip. Verified by size, never by trust: a truncated
    download later surfaces as a confusing demuxer error, not as a download
    error."""
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".part"
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    if os.path.getsize(tmp) == 0:
        os.remove(tmp)
        raise RuntimeError(f"download of {url} produced an empty file")
    os.replace(tmp, out)
    return out


def free_gb(path: str) -> float:
    drive = os.path.splitdrive(os.path.abspath(path))[0] or "/"
    return shutil.disk_usage(drive).free / 1e9


def require_space(path: str, min_free_gb: float = MIN_FREE_GB_4K) -> float:
    free = free_gb(path)
    if free < min_free_gb:
        raise RuntimeError(
            f"only {free:.2f} GB free at {path}; need {min_free_gb:.1f} GB. "
            f"Free space or pass --work <path>."
        )
    return free
