"""Quality metrics: VMAF (what streaming services tune ladders with) plus
SSIM/PSNR as cheap sanity checks.

The methodology note is the whole point of this module. Comparing a 1080p rung
against a 1080p slice of the source measures mostly the *scaling*, not the
encoding. The industry-standard comparison is the opposite direction: upscale the
distorted rendition back to the source resolution and compare against the full
reference. That way a rung is scored on what the encoder did, at the resolution
the viewer will actually see the artefacts at.

Two practical traps:

* `libvmaf` defaults to a single thread: ~1.8 fps on a 4K reference. With
  `n_threads=8` it ran at ~13.7 fps (measured, 7.5x). A metric slower than the
  encode is a metric nobody runs on a catalogue.
* the option is `log_fmt`, not `log_format`, and the JSON aggregates live under
  `pooled_metrics.vmaf` (older builds: `pooled.VMAF`). Parsing guesses either.
"""

from __future__ import annotations

import json
import os
import re

from . import ff


def default_threads() -> int:
    """VMAF is embarrassingly parallel; cap it so it cannot starve the encoder."""
    return max(1, min(8, os.cpu_count() or 1))


def graph(ref_size: str | None) -> str:
    """Two labelled inputs: [d] distorted, [r] reference.

    `force_original_aspect_ratio=decrease` + `pad` keeps the aspect ratio while
    reaching the reference frame size; `setsar=1`/`format=yuv420p` on both sides
    removes the pixel-format and SAR mismatches libvmaf otherwise rejects.
    """
    if ref_size:
        w, h = ref_size.lower().split("x", 1)
        return (
            f"[0:v]scale={w}:{h}:flags=lanczos:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[d];"
            f"[1:v]format=yuv420p,setsar=1[r];"
        )
    return "[0:v]format=yuv420p[d];[1:v]format=yuv420p[r];"


def escape_log_path(path: str) -> str:
    """`log_path` sits inside a filtergraph, where `:` ends an option.

    A Windows drive colon therefore has to be escaped as `\\\\:` in the argv
    string: the graph level consumes one backslash, the remaining one escapes the
    colon. Quoting the path does not help. A relative path is cheaper - which is
    why callers run with cwd set to the work dir when they can.
    """
    return ff.clean_path(path).replace(":", "\\\\:")


def vmaf_args(ref: str, dist: str, log_json: str, threads: int | None = None,
              ref_size: str | None = None) -> list[str]:
    threads = default_threads() if threads is None else threads
    opts = [f"log_path={escape_log_path(log_json)}", "log_fmt=json"]
    if threads:
        opts.append(f"n_threads={threads}")
    return [
        ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", dist, "-i", ref,
        "-lavfi", graph(ref_size) + "[d][r]libvmaf=" + ":".join(opts),
        "-f", "null", "-",
    ]


def parse_vmaf_json(probe: dict) -> dict:
    """Pull the aggregates out of a libvmaf JSON log.

    Prefers the pooled numbers; recomputes from per-frame values if the build
    wrote no pooled block. Harmonic mean is the number the industry quotes for a
    ladder because it punishes the bad frames instead of averaging them away.
    """
    pooled = (probe.get("pooled_metrics") or probe.get("pooled") or {}).get("vmaf") or {}
    frames = probe.get("frames", [])
    vals = [f["metrics"]["vmaf"] for f in frames
            if "vmaf" in f.get("metrics", {}) and f["metrics"]["vmaf"] is not None]

    mean = pooled.get("mean") or (sum(vals) / len(vals) if vals else 0.0)
    return {
        "vmaf_mean": round(float(mean), 2),
        "vmaf_harmonic": round(float(pooled.get("harmonic_mean") or 0.0), 2),
        "vmaf_min": round(min(vals), 2) if vals else 0.0,
        "vmaf_p99": round(percentile(vals, 0.99), 2) if vals else 0.0,
        "frames": len(frames),
    }


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile. p99 of VMAF is where ladder tuning lives: the
    mean hides the few frames that look bad, the tail does not."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(q * len(ordered)) - 1))
    return ordered[idx]


def vmaf(ref: str, dist: str, log_json: str, threads: int | None = None,
         ref_size: str | None = None, runner: ff.Runner | None = None) -> dict:
    run = runner or ff.Runner()
    res = run.run(vmaf_args(ref, dist, log_json, threads=threads, ref_size=ref_size))
    if not os.path.exists(log_json):
        return {"error": "libvmaf wrote no log"}
    with open(log_json, encoding="utf-8", errors="replace") as f:
        parsed = parse_vmaf_json(json.load(f))
    parsed["wall_s"] = round(res.wall_s, 2)
    return parsed


# --------------------------------------------------------------------------
# ssim / psnr: no model, no file, cheap
# --------------------------------------------------------------------------

_SSIM_RE = re.compile(r"All:([0-9.]+)")
_PSNR_RE = re.compile(r"average ([0-9.]+) dB")


def ssim_psnr_args(ref: str, dist: str, name: str) -> list[str]:
    return [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "info",
            "-i", dist, "-i", ref, "-lavfi", f"[0:v][1:v]{name}", "-f", "null", "-"]


def ssim_psnr(ref: str, dist: str, runner: ff.Runner | None = None) -> dict:
    """ssim/psnr print their summary to **stderr** - there is no log file."""
    run = runner or ff.Runner()
    out: dict = {}
    for name, regex, key in (("ssim", _SSIM_RE, "ssim"), ("psnr", _PSNR_RE, "psnr_db")):
        res = run.run(ssim_psnr_args(ref, dist, name), check=False)
        m = regex.search(res.stderr or "")
        if m:
            out[key] = float(m.group(1))
        out[f"{name}_wall_s"] = round(res.wall_s, 2)
    return out


def bitrate_args(path: str) -> list[str]:
    return ff.probe_args(path, [
        "stream=codec_type,codec_name,bit_rate,width,height:format=bit_rate,duration"
    ])


def parse_bitrate_probe(probe: dict) -> dict:
    """Bitrate per track, with the container's total as the fallback.

    MPEG-TS does not carry a per-stream video bit_rate: ffprobe answers N/A for
    it while the format level has a real number. Reading only the stream field
    silently reports 0 kbps, which makes a ladder look like it produced nothing.
    So the video figure falls back to (container - audio) and the result says
    which basis was used - an estimate labelled as one is better than a zero
    that reads like a measurement.
    """
    streams = probe.get("streams", [])
    video: dict = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio: dict = next((s for s in streams if s.get("codec_type") == "audio"), {})
    fmt: dict = probe.get("format", {})

    resolution = None
    if video:
        resolution = f"{video.get('width')}x{video.get('height')}"

    total_bps = ff.as_int(fmt.get("bit_rate"))
    audio_bps = ff.as_int(audio.get("bit_rate"))
    video_bps = ff.as_int(video.get("bit_rate"))
    source = "stream"
    if not video_bps and total_bps:
        video_bps = max(0, total_bps - audio_bps)
        source = "container"

    return {
        "video_kbps": video_bps // 1000,
        "audio_kbps": audio_bps // 1000,
        "codec": video.get("codec_name"),
        "resolution": resolution,
        "total_kbps": total_bps // 1000,
        "bitrate_source": source,
    }


def bitrate(path: str, runner: ff.Runner | None = None) -> dict:
    """Bitrate actually achieved. Compared against the ladder target this is how
    you find a rung whose rate control drifted."""
    probe = ff.probe_json(path, [
        "stream=codec_type,codec_name,bit_rate,width,height:format=bit_rate,duration"
    ], runner=runner)
    return parse_bitrate_probe(probe)
