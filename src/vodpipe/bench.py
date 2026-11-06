"""Benchmark matrices.

Two questions the pipeline has to answer with numbers rather than opinions:

1. **Encoder/parallelism matrix** (`matrix`): what does NVENC buy over the CPU
   baseline, and does splitting a job across processes help? The honest answer
   measured here: NVENC is 1.3-1.7x over x264-medium at matched bitrate, and
   chunking on a *single* GPU makes wall time monotonically worse (+19% at 8
   workers on 2160p). Chunking is a multi-GPU tool.
2. **Watermark overhead matrix** (`watermark_overhead`): what does content
   protection cost? Fused into the encode pass: nothing measurable. As a separate
   pass: +103.9% wall and another generation of loss.

Both compare against a baseline row on the *same rung*, which is the only
comparison that means anything: a speedup measured against a different rung or a
different target bitrate is marketing, not data.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass

from . import encode, encoders, ff, metrics, probe, ts, watermark
from .ladder import Rung
from .ladder import select as ladder_select

# (tag, encoder, mode, nchunks, workers, preset)
MATRIX = [
    ("x264_medium_whole", "libx264", "whole", 1, 1, "medium"),
    ("nvenc_p5_whole", "h264_nvenc", "whole", 1, 1, "p5"),
    ("nvenc_p5_c2w2", "h264_nvenc", "chunked", 2, 2, "p5"),
    ("nvenc_p5_c4w4", "h264_nvenc", "chunked", 4, 4, "p5"),
    ("nvenc_p5_c8w8", "h264_nvenc", "chunked", 8, 8, "p5"),
    ("nvenc_p5_c8w4", "h264_nvenc", "chunked", 8, 4, "p5"),
    ("nvenc_p1_c8w8", "h264_nvenc", "chunked", 8, 8, "p1"),
]

# (tag, watermark spec, run as a separate second pass?)
WATERMARK_VARIANTS: list[tuple[str, dict[str, object], bool]] = [
    ("none", {}, False),
    ("static", {"static": "MAXIMA VOD"}, False),
    ("dyn", {"dynamic": "viewer-42f7a9"}, False),
    ("dyn_ghost", {"dynamic": "viewer-42f7a9", "ghost": True}, False),
    ("twopass", {"static": "MAXIMA VOD"}, True),
]


@dataclass(frozen=True)
class Selection:
    rungs: list[str]
    configs: list[str]

    @staticmethod
    def parse(rungs: str, configs: str) -> Selection:
        """Parse and validate the requested matrix slice.

        Configs are checked against `MATRIX` for the same reason `pick_rungs`
        validates labels: a benchmark that quietly drops a typo'd config and
        exits 0 prints a shorter table than it was asked for, and the reader has
        no way to know a row is missing.
        """
        wanted = [c.strip() for c in configs.split(",") if c.strip()]
        if "all" in wanted:
            wanted = sorted({c[0] for c in MATRIX})
        known = {c[0] for c in MATRIX}
        unknown = [c for c in wanted if c not in known]
        if unknown:
            raise ValueError(f"unknown config(s): {', '.join(unknown)}; "
                             f"choose from {', '.join(sorted(known))} or 'all'")
        return Selection(
            rungs=[r.strip() for r in rungs.split(",") if r.strip()],
            configs=wanted,
        )


def pick_rungs(labels: list[str]) -> list[Rung]:
    """Rungs for the requested labels, raising on a typo.

    Strict here (unlike the queue path): a benchmark that silently encodes a
    different rung set than it was asked for produces a table nobody can trust.
    """
    return ladder_select(labels, strict=True)


def run_matrix(src: probe.Source, work: str, sel: Selection, do_vmaf: bool = False,
               runner: ff.Runner | None = None) -> list[dict]:
    run = runner or ff.Runner()
    ref = src.resolution
    rows: list[dict] = []
    # One `ffmpeg -encoders` dump for the whole matrix, not one per row: the
    # result is constant for a given binary.
    available = ff.encoder_names(run)
    for rung in pick_rungs(sel.rungs):
        for tag, enc_name, mode, nchunks, workers, preset in MATRIX:
            if tag not in sel.configs:
                continue
            enc = encoders.build(enc_name, preset)
            if enc.hw and enc.codec not in available:
                print(f"  [{rung.label}] {tag}: skipped, {enc.codec} unavailable")
                continue

            out = os.path.join(work, f"m2_{tag}_{rung.label}.ts")
            if mode == "whole":
                r = encode.transcode_whole(src, rung, enc, out, runner=run)
            else:
                chunk_dir = os.path.join(work, f"m2_chunks_{tag}")
                r = encode.transcode_chunked(src, rung, enc, out, nchunks, workers,
                                             chunk_dir, runner=run)
                # Integrity is part of the measurement: a faster encode that
                # drops frames is not faster, it is wrong. The expectation uses
                # the fps this encode was commanded to emit, not the source fps.
                check = ts.verify_concat(
                    out, ts.expected_video_frames(src.duration, r["out_fps"]), runner=run)
                r["concat_ok"] = check["ok"]
                r["vpkts"] = check["vpkts"]
                shutil.rmtree(chunk_dir, ignore_errors=True)

            r["tag"] = tag
            r["kbps"] = round(r["size"] * 8 / src.duration / 1000)
            if do_vmaf:
                r["vmaf"] = metrics.vmaf(src.path, out,
                                         os.path.join(work, f"m2_{tag}_{rung.label}_vmaf.json"),
                                         ref_size=ref, runner=run)
            print(f"  [{rung.label:>5}] {tag:<18} wall={r['wall_s']:>6}s "
                  f"speed={r['speed_x']:>5}x kbps={r['kbps']:>6}"
                  + (f" vmaf={r['vmaf'].get('vmaf_mean')}" if do_vmaf else "")
                  + (f" concat_ok={r.get('concat_ok')}" if mode == "chunked" else ""))
            os.remove(out)
            rows.append(r)
    return with_speedup(rows)


def with_speedup(rows: list[dict]) -> list[dict]:
    """Speedup vs the x264 baseline **on the same rung**."""
    for r in rows:
        base = next((b for b in rows if b["rung"] == r["rung"]
                     and b["encoder"] == "libx264" and b["mode"] == "whole"), None)
        if base and base["wall_s"]:
            r["vs_x264"] = round(base["wall_s"] / r["wall_s"], 2)
    return rows


def noise_runs(src: probe.Source, work: str, sel: Selection, repeats: int,
               runner: ff.Runner | None = None) -> list[dict]:
    """Run the selected matrix `repeats` times and report per-(rung, tag) spread.

    This is not a second measurement method: each repeat goes through the same
    `run_matrix` encode path, so the spread it reports is the spread of the very
    numbers the M2 table quotes. Wall time is the only quantity kept - the point
    is run-to-run variance, not another throughput figure. Spread is
    `(max-min)/min`, so "0.8%" reads as "these two runs differ by less than 1%".
    """
    walls: dict[tuple[str, str], list[float]] = {}
    for _ in range(max(1, repeats)):
        for r in run_matrix(src, work, sel, do_vmaf=False, runner=runner):
            walls.setdefault((r["rung"], r["tag"]), []).append(r["wall_s"])
    out: list[dict] = []
    for (rung, tag), vals in walls.items():
        lo = min(vals)
        hi = max(vals)
        spread = round((hi - lo) / lo * 100, 1) if lo else 0.0
        out.append({"rung": rung, "tag": tag, "repeats": len(vals),
                    "runs_s": vals, "min_s": lo, "max_s": hi, "spread_pct": spread})
    out.sort(key=lambda d: (d["rung"], d["tag"]))
    return out


def two_pass(src: probe.Source, rung: Rung, enc: encoders.Encoder, out: str, work: str,
             wm_spec: dict, runner: ff.Runner | None = None) -> dict:
    """The approach nobody should ship: clean transcode, then a second pass that
    only adds the watermark. Kept so the fused approach has a measured rival.

    It also compounds generation loss: the second pass re-encodes an already
    lossy stream, with the overlay composited on top of compression artefacts.
    """
    run = runner or ff.Runner()
    base = os.path.join(work, "twopass_base.ts")
    first = encode.transcode_whole(src, rung, enc, base, runner=run)

    # The second pass reuses the *same* rate control, GOP and scale filter the
    # fused path uses (`encode`/`encoders` builders), so the "+~99%" overhead is
    # the cost of the extra pass itself and not an artefact of a different
    # encoder configuration. The only intentional difference: audio is copied,
    # because pass 1 already encoded it.
    chain = watermark.from_spec(wm_spec)
    vf = watermark.chain(rung.scale(src.width, src.height), "setsar=1", chain)
    argv = [ff.ffmpeg(), "-y", *encode.BASE_ARGS,
            "-i", base, "-vf", vf,
            *encoders.gop_args(encode.out_fps(src, rung)),
            *enc.args(), *encoders.rate_args(rung),
            "-c:a", "copy", "-f", "mpegts", out]
    t0 = time.perf_counter()
    res = run.run(argv)
    second = time.perf_counter() - t0

    total = first["wall_s"] + round(second, 2)
    return {
        "mode": "twopass", "rung": rung.label, "encoder": enc.name,
        "pass1_wall_s": first["wall_s"], "pass2_wall_s": round(second, 2),
        "wall_s": round(total, 2), "speed_x": round(src.duration / total, 3),
        "out": out, "size": os.path.getsize(out), "speed_x_proc": round(res.speed_x, 3),
    }


def watermark_overhead(src: probe.Source, rung: Rung, work: str,
                       variants: list[tuple[str, dict[str, object], bool]] | None = None,
                       reps: int = 2, encoder: str = "h264_nvenc",
                       runner: ff.Runner | None = None) -> list[dict]:
    """Wall time and VMAF delta per watermark variant, averaged over `reps`.

    Repetition is not decoration: the spread between two runs of the *baseline*
    (9.37 s and 9.10 s measured here) is the same size as the effect being
    claimed, so a single run would have "proven" a 2% speedup from watermarking.
    """
    run = runner or ff.Runner()
    chosen: list[tuple[str, dict[str, object], bool]] = list(variants or WATERMARK_VARIANTS)
    enc = encoders.build(encoder)
    rows: list[dict] = []

    for tag, spec, two in chosen:
        walls, vmafs = [], []
        last: dict = {}
        for rep in range(reps):
            out = os.path.join(work, f"m3_{tag}_r{rep}.ts")
            if two:
                r = two_pass(src, rung, enc, out, work, spec, runner=run)
                pass1 = os.path.join(work, "twopass_base.ts")
                if os.path.exists(pass1):
                    os.remove(pass1)
            else:
                r = encode.transcode_whole(src, rung, enc, out,
                                           wm=watermark.from_spec(spec), runner=run)
            walls.append(r["wall_s"])
            vmafs.append(metrics.vmaf(src.path, out,
                                      os.path.join(work, f"m3_{tag}_r{rep}_vmaf.json"),
                                      ref_size=src.resolution, runner=run))
            last = r
            os.remove(out)

        mean = lambda xs: round(sum(xs) / len(xs), 2)  # noqa: E731
        rows.append({
            "tag": tag, "two_pass": two, "rung": rung.label, "encoder": enc.name,
            "wall_s": mean(walls), "speed_x": round(src.duration / mean(walls), 3),
            "size": last["size"],
            "kbps": round(last["size"] * 8 / src.duration / 1000),
            "vmaf_mean": mean([v.get("vmaf_mean", 0.0) for v in vmafs]),
            "vmaf_min": mean([v.get("vmaf_min", 0.0) for v in vmafs]),
            "reps": walls,
        })
        print(f"  {tag:<10} wall={rows[-1]['wall_s']:>7}s "
              f"vmaf={rows[-1]['vmaf_mean']:>6} kbps={rows[-1]['kbps']:>6}")

    base = next((r for r in rows if r["tag"] == "none"), None)
    if base:
        for r in rows:
            r["overhead_pct"] = round((r["wall_s"] / base["wall_s"] - 1) * 100, 1)
            r["vmaf_drop"] = round(base["vmaf_mean"] - r["vmaf_mean"], 2)
    return rows
