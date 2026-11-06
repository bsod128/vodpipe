"""Transcoding: one pass over the whole file, or N chunks in parallel.

`transcode_whole` is the reference implementation and the baseline every
speedup number is divided by. `transcode_chunked` splits on IDR frames, runs one
ffmpeg process per chunk, then binary-concatenates the MPEG-TS outputs.

Why MPEG-TS for the intermediate: TS is designed to be concatenated. Chunks that
share codec/resolution/timebase and each start with an IDR can be joined with a
plain byte copy - no re-encode, therefore no generation loss and no seam
artefacts. MP4 cannot be joined that way (its moov index describes one file).

The measured caveat, and the reason this module reports both `wall_s` and
`agg_speed_x`: on a single consumer GPU the NVENC block is one piece of silicon,
so N processes contend for it. Chunking scales across *GPUs*, not within one.
See `bench/m2_bench.json` for the curve.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import ff, ts, watermark
from . import probe as probe_mod
from .encoders import Encoder, gop_args, rate_args
from .ladder import Rung
from .probe import Source

#: ffmpeg logging flags shared by every encode path (whole, chunked, bench).
BASE_ARGS = ["-hide_banner", "-loglevel", "info", "-stats"]


@dataclass(frozen=True)
class VideoFilter:
    """scale + setsar, then watermark filters, in that order.

    Watermarking after the scale is deliberate: drawtext is then rendering and
    compositing text at the *output* resolution, which is the cheapest place to
    do it, and the glyph size means the same thing to the viewer on every rung.
    """

    rung: Rung
    src: Source
    wm: str = ""

    def build(self) -> str:
        return watermark.chain(self.rung.scale(self.src.width, self.src.height),
                               "setsar=1", self.wm or None)


def out_fps(src: Source, rung: Rung) -> int:
    """Frame rate the encode will actually produce: source fps, capped by the rung.

    Getting this wrong is expensive twice over. Forcing the rung ceiling (30) on
    a 24 fps master resamples the picture, so the frame-count integrity check
    compares a 30 fps output against a 24 fps expectation and reports a false
    failure; and libvmaf pairs frames by index, so a rate mismatch between the
    rendition and the reference silently misaligns every frame instead of
    failing. A master is produced at a fixed rate in this pipeline, so encode and
    master agree and the two sides of every comparison stay on the same basis.
    """
    fps = round(src.fps) if src.fps else rung.max_fps
    return max(1, min(fps, rung.max_fps))


def audio_args(rung: Rung) -> list[str]:
    """AAC per rung. Audio is re-encoded rather than copied so that every rung
    has the same sample rate and channel layout; a ladder whose audio differs
    between rungs breaks ABR switching on several players."""
    return ["-c:a", "aac", "-b:a", str(rung.audio), "-ac", "2"]


def _output_args(rung: Rung, out_path: str) -> list[str]:
    # muxdelay/muxpreload 0 keeps the first packets from being pushed forward by
    # the muxer's default buffering, which would otherwise offset chunk starts
    # and make the concatenated stream drift.
    return [*audio_args(rung), "-muxdelay", "0", "-muxpreload", "0",
            "-f", "mpegts", out_path]


def whole_args(src: Source, rung: Rung, enc: Encoder, out_path: str, wm: str = "") -> list[str]:
    vf = VideoFilter(rung, src, wm).build()
    return [
        ff.ffmpeg(), "-y", *BASE_ARGS,
        "-i", src.path,
        "-vf", vf,
        *gop_args(out_fps(src, rung)),
        *enc.args(),
        *rate_args(rung),
        *_output_args(rung, out_path),
    ]


def transcode_whole(src: Source, rung: Rung, enc: Encoder, out_path: str, wm: str = "",
                    runner: ff.Runner | None = None) -> dict:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    res = (runner or ff.Runner()).run(whole_args(src, rung, enc, out_path, wm))
    return {
        "mode": "whole", "rung": rung.label, "encoder": enc.name,
        "speed_x": round(res.speed_x, 3), "wall_s": round(res.wall_s, 2),
        "out_fps": out_fps(src, rung),
        "out": out_path, "size": os.path.getsize(out_path),
    }


def chunk_frames(src: Source, rung: Rung, start: float, end: float) -> int:
    """Exact output-video frame count for [start, end), on the rung's output grid.

    Cut on absolute boundaries (`round(end*fps) - round(start*fps)`) rather than
    on the span, so the per-chunk counts telescope to
    `round(duration*fps)` = `ts.expected_video_frames` no matter where the borders
    fall: the first boundary contributes 0 and the last is the whole file, so the
    intermediate boundaries cancel. A `-t`/duration trim cannot promise this: it
    asks ffmpeg to keep frames within a half-open time interval, and whether the
    boundary frame survives depends on the exact `(frame, fps)` pair, so a
    sub-frame trim silently drops a frame and the concat check false-fails.
    """
    fps = out_fps(src, rung)
    return round(end * fps) - round(start * fps)


def chunk_args(src: Source, rung: Rung, enc: Encoder, start: float, end: float,
               out_path: str, wm: str = "") -> list[str]:
    vf = VideoFilter(rung, src, wm).build()
    return [
        ff.ffmpeg(), "-y", *BASE_ARGS,
        # -ss BEFORE -i is a fast seek that lands on an IDR: accurate and cheap.
        # -ss AFTER -i decodes everything up to the cut (accurate but slow) and is
        # the wrong choice here because we already chose IDR boundaries.
        # -t bounds the AUDIO (which the frame check does not count); -frames:v
        # pins the VIDEO to an exact frame count, see chunk_frames.
        "-ss", str(start), "-t", str(round(end - start, 4)),
        "-i", src.path,
        "-vf", vf,
        *gop_args(out_fps(src, rung)),
        *enc.args(),
        *rate_args(rung),
        "-frames:v", str(chunk_frames(src, rung, start, end)),
        *_output_args(rung, out_path),
    ]


def encode_chunk(src: Source, rung: Rung, enc: Encoder, start: float, end: float,
                 out_path: str, wm: str = "", runner: ff.Runner | None = None) -> dict:
    res = (runner or ff.Runner()).run(
        chunk_args(src, rung, enc, start, end, out_path, wm))
    return {
        "start": start, "end": end,
        "speed_x": round(res.speed_x, 3), "wall_s": round(res.wall_s, 2),
        "size": os.path.getsize(out_path),
    }


def transcode_chunked(src: Source, rung: Rung, enc: Encoder, out_path: str,
                      nchunks: int, workers: int, chunk_dir: str, wm: str = "",
                      runner: ff.Runner | None = None) -> dict:
    """Encode nchunks ranges with up to `workers` parallel ffmpeg processes.

    Returns both numbers on purpose:
      * `wall_s`      - what the user waits for;
      * `agg_speed_x` - mean per-process throughput, i.e. how busy the GPU was.
    When agg_speed_x rises while wall_s also rises, parallelism is buying
    utilisation, not latency - which is exactly what one GPU does.
    """
    os.makedirs(chunk_dir, exist_ok=True)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    bounds = probe_mod.plan_chunks(src.duration, src.keyframes, nchunks, fps=src.fps)
    paths = [os.path.join(chunk_dir, f"c{i:03d}.ts") for i in range(len(bounds))]

    run = runner or ff.Runner()
    t0 = time.perf_counter()
    stats: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(bounds)))) as pool:
        futures = [
            pool.submit(encode_chunk, src, rung, enc, s, e, p, wm, run)
            for (s, e), p in zip(bounds, paths, strict=True)
        ]
        for fut in futures:
            stats.append(fut.result())
    encode_wall = time.perf_counter() - t0

    t1 = time.perf_counter()
    ts.concat(paths, out_path)
    concat_wall = time.perf_counter() - t1

    stats.sort(key=lambda d: d["start"])
    total = encode_wall + concat_wall
    return {
        "mode": "chunked", "rung": rung.label, "encoder": enc.name,
        "nchunks": len(bounds), "workers": max(1, min(workers, len(bounds))),
        "encode_wall_s": round(encode_wall, 2),
        "concat_wall_s": round(concat_wall, 3),
        "wall_s": round(total, 2),
        "speed_x": round(src.duration / total, 3),
        "agg_speed_x": round(sum(s["speed_x"] for s in stats) / max(len(stats), 1), 3),
        "out_fps": out_fps(src, rung),
        "out": out_path, "size": os.path.getsize(out_path),
        "chunks": stats,
    }
