"""Single entrypoint: `python -m vodpipe <command>` (or the `vodpipe` script).

The CLI is deliberately thin: every command parses arguments, calls one library
function, and prints. No encoding logic lives here, so the same operations are
available to the queue worker, the tests and a notebook without going through
argparse.

Commands
    doctor    what ffmpeg/GPU/encoders are actually available (start here)
    master    generate or fetch the source clip the benchmarks use
    demo      end-to-end pipeline on one small ladder (no Docker needed)
    bench     encoder + parallelism matrix (M2)
    watermark watermark overhead matrix (M3)
    figures   SVG charts from the JSON reports
    job       submit/status/cancel against a running API (M4)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import bench as bench_mod
from . import ff, figures, gpu, probe, report, sample
from . import pipeline as pl
from .encoders import BACKENDS
from .ladder import select as select_rungs
from .ladder import validate
from .watermark import is_safe_id

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_SRC = os.path.join(ROOT, "src_clip", "4k_master.mp4")
WORK = os.path.join(ROOT, "work")
OUT = os.path.join(ROOT, "out")
BENCH = os.path.join(ROOT, "bench")


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- doctor


def cmd_doctor(_: argparse.Namespace) -> int:
    print("ffmpeg:", end=" ")
    try:
        h = ff.health()
    except ff.FFmpegNotFound as exc:
        print(f"MISSING ({exc})")
        return 1
    print(h.version)
    print(f"binary: {h.ffmpeg}")
    print("\nencoders:")
    for name, ok in h.encoders.items():
        print(f"  {'ok ' if ok else '-- '} {name}")
    print("\nfilters:")
    missing = [f for f, ok in h.filters.items() if not ok]
    print("  all present" if not missing else "  missing: " + ", ".join(missing))

    print("\nGPU:")
    rep = gpu.pick()
    print(gpu.summary(rep))
    if rep["picked"] is None:
        print("  NVENC unavailable: fall back to --encoder libx264")
    return 0


# ---------------------------------------------------------------- master


def cmd_master(args: argparse.Namespace) -> int:
    os.makedirs(os.path.dirname(args.src) or ".", exist_ok=True)
    sample.require_space(os.path.dirname(args.src), 3.0)
    if args.url:
        sample.fetch(args.url, args.src)
    else:
        print(f"generating a deterministic {args.size} master "
              f"({args.duration:g} s, seed {args.seed}) ...")
        sample.make_master(args.src, duration=args.duration, size=args.size, seed=args.seed)
    src = probe.probe_source(args.src)
    print("source:", src.summary())
    return 0


# ------------------------------------------------------------------ demo


def cmd_demo(args: argparse.Namespace) -> int:
    """The one-button path: probe -> ladder -> verify -> package -> VMAF."""
    sample.require_space(WORK, args.min_free_gb)
    if not os.path.exists(args.src):
        print(f"no source at {args.src}; run: vodpipe master")
        return 2

    gpurep = None
    if args.encoder.endswith("nvenc"):
        gpurep = gpu.pick(args.encoder)
        print(gpu.summary(gpurep))
        if gpurep["picked"] is None:
            print("NVENC unavailable here; rerun with --encoder libx264")
            return 1

    # strict=True: `--rungs 2160p,480p` used to drop the unknown label silently
    # and encode the rest, so the run looked fine and measured something else.
    try:
        rungs = select_rungs(args.rungs.split(","))
    except ValueError as exc:
        print(exc)
        return 2
    problems = validate(rungs)
    if problems:
        print("ladder rejected:")
        for problem in problems:
            print("  " + problem)
        return 2

    wm_spec = None
    if args.watermark:
        wm_spec = {"dynamic": args.watermark, "ghost": bool(args.ghost)}
        if not is_safe_id(args.watermark):
            print(f"refusing watermark id {args.watermark!r}: use [A-Za-z0-9_.:@#-]")
            return 2

    work = os.path.join(WORK, "demo")
    out = os.path.join(OUT, "demo")
    pipe = pl.Pipeline(args.src, work, wm_spec=wm_spec)
    print("source:", pipe.src.summary())
    if not pipe.src.is_4k():
        print("note: source is not 4K; numbers will not match the README tables")

    def progress(i: int, n: int, results: list) -> None:
        done = sum(r.get("wall_s", 0) for r in results)
        print(f"    progress {i}/{n} rungs, {done:.1f}s of encode so far")

    t0 = time.perf_counter()
    results, wall = pipe.run_ladder(encoder=args.encoder, preset=args.preset,
                                    nchunks=args.chunks, workers=args.chunks,
                                    tag="demo", rungs=rungs, on_rung_done=progress)
    print(f"ladder wall = {wall:.1f}s")

    verify = pipe.verify(results)
    for v in verify:
        print(f"  concat check {v['rung']}: ok={v['ok']} vpkts={v['vpkts']}"
              f"/{v['expected_frames']}")

    pkg = pipe.package(results, out)
    if pkg["hls"]:
        print("packaged:", len(pkg["hls"]["variants"]), "HLS variants,",
              pkg["hls"]["segments"], "segments;",
              len(pkg["dash"]["representations"]), "DASH representations")
        print("ladder from manifest:", pkg["ladder"]["ladder"])

    vmaf = {} if args.no_vmaf else pipe.vmaf_for(results, tag="demo_")
    total = round(time.perf_counter() - t0, 2)

    rep = report.build_report(
        job_id="demo",
        meta={"ffmpeg": ff.version(), "encoder": args.encoder, "preset": args.preset,
              "chunks": args.chunks, "source": pipe.src.summary(), "gpu": gpurep,
              "watermark": wm_spec, "rungs": [r.label for r in rungs]},
        encode=results, ladder_wall_s=wall, packaging=pkg, vmaf=vmaf, verify=verify,
        total_wall_s=total)
    path = report.dump(os.path.join(BENCH, "demo.json"), report.sanitize(rep, ROOT))
    print("report ->", report.to_repo_relative(path, ROOT))
    return 0




# ------------------------------------------------------------------ bench


def cmd_bench(args: argparse.Namespace) -> int:
    sample.require_space(WORK, args.min_free_gb)
    src = probe.probe_source(args.src)
    print("ffmpeg:", ff.version())
    print("source:", src.summary())
    gpurep = gpu.pick("h264_nvenc")
    print(gpu.summary(gpurep))

    sel = bench_mod.Selection.parse(args.rungs, args.configs)
    work = os.path.join(WORK, "m2")
    os.makedirs(work, exist_ok=True)
    t0 = time.perf_counter()
    rows = bench_mod.run_matrix(src, work, sel, do_vmaf=args.vmaf)
    rep = {
        "meta": {"ffmpeg": ff.version(), "source": src.summary(), "gpu": gpurep,
                 "configs": sel.configs, "rungs": sel.rungs, "vmaf": args.vmaf,
                 "total_wall_s": round(time.perf_counter() - t0, 1)},
        "rows": rows,
    }
    path = report.dump(os.path.join(BENCH, "m2_bench.json"), report.sanitize(rep, ROOT))
    print("\nspeedup vs x264 on the same rung:")
    for r in rows:
        if "vs_x264" in r:
            print(f"  [{r['rung']:>5}] {r['tag']:<18} x{r['vs_x264']}")
    print("report ->", report.to_repo_relative(path, ROOT))
    return 0


def cmd_watermark(args: argparse.Namespace) -> int:
    sample.require_space(WORK, args.min_free_gb)
    src = probe.probe_source(args.src)
    gpurep = gpu.pick("h264_nvenc")
    print(gpu.summary(gpurep))
    rung = bench_mod.pick_rungs([args.rung])[0]
    work = os.path.join(WORK, "m3")
    os.makedirs(work, exist_ok=True)
    print("ffmpeg:", ff.version(), "| source:", src.summary())
    rows = bench_mod.watermark_overhead(src, rung, work, reps=args.reps,
                                        encoder=args.encoder)
    rep = {"meta": {"ffmpeg": ff.version(), "source": src.summary(), "gpu": gpurep,
                    "rung": rung.label, "encoder": args.encoder, "reps": args.reps},
           "rows": rows}
    path = report.dump(os.path.join(BENCH, "m3_watermark.json"), report.sanitize(rep, ROOT))
    print("\noverhead vs clean transcode:")
    for r in rows:
        print(f"  {r['tag']:<10} {r['overhead_pct']:>+6.1f}% wall   "
              f"VMAF -{r['vmaf_drop']:>5}   {r['kbps']} kbps")
    print("report ->", report.to_repo_relative(path, ROOT))
    return 0


# ---------------------------------------------------------------- figures


def ladder_figures(rep: dict, out_dir: str) -> list[str]:
    """Charts for one ladder report.

    The bitrate comes from the packaging block, not from the file size:
    BANDWIDTH in the master playlist is audio+video combined and is the number
    a CDN and a player actually see, while file size also carries container
    overhead. `encode` rows carry the resolution, so rung -> BANDWIDTH is a
    lookup, not a guess.
    """
    pkg = rep.get("packaging") or {}
    by_resolution = {v["resolution"]: v["bandwidth"] // 1000
                     for v in (pkg.get("hls") or {}).get("variants", [])}
    vmaf = rep.get("vmaf") or {}
    rows = []
    for e in rep.get("encode") or []:
        if e.get("cancelled"):
            continue
        m = vmaf.get(e["rung"]) or {}
        rows.append({
            "rung": e["rung"],
            "wall_s": e.get("wall_s"),
            "total_kbps": by_resolution.get(e.get("rung_res"), 0),
            "vmaf_mean": m.get("vmaf_mean", 0),
            "vmaf_min": m.get("vmaf_min", 0),
        })
    if not rows:
        return []
    return [
        figures.ladder_chart(rows, os.path.join(out_dir, "m1_time_bitrate.svg")),
        figures.vmaf_chart(rows, os.path.join(out_dir, "m1_vmaf.svg")),
    ]


def cmd_figures(_: argparse.Namespace) -> int:
    out_dir = os.path.join(ROOT, "docs", "figures")
    os.makedirs(out_dir, exist_ok=True)
    made = []

    demo = os.path.join(BENCH, "demo.json")
    if os.path.exists(demo):
        rep = report.load(demo)
        made += ladder_figures(rep, out_dir)

    m2 = os.path.join(BENCH, "m2_bench.json")
    if os.path.exists(m2):
        rep = report.load(m2)
        rows = rep.get("rows", [])
        rungs = sorted({r["rung"] for r in rows}, key=lambda s: -int(s.rstrip("p")))
        tags = sorted({r["tag"] for r in rows})
        series = [(t, [next((r["wall_s"] for r in rows if r["rung"] == g and r["tag"] == t), 0)
                       for g in rungs]) for t in tags]
        made.append(figures.scaling_chart(rungs, series,
                                          os.path.join(out_dir, "m2_scaling.svg")))

    m3 = os.path.join(BENCH, "m3_watermark.json")
    if os.path.exists(m3):
        rows = report.load(m3).get("rows", [])
        made.append(figures.watermark_chart(rows, os.path.join(out_dir, "m3_watermark.svg")))

    if not made:
        print("no reports in bench/ yet - run: vodpipe demo / bench / watermark")
        return 1
    for path in made:
        print("wrote", report.to_repo_relative(path, ROOT))
    return 0


# ------------------------------------------------------------------- job


def cmd_job(args: argparse.Namespace) -> int:
    from .client import Client

    c = Client(args.api)
    if args.cancel:
        _print_json(c.cancel(args.cancel))
        return 0
    if args.status:
        _print_json(c.status(args.status))
        return 0

    body = {
        "source": args.source,
        "encoder": args.encoder,
        "rungs": args.rungs.split(","),
        "watermark": ({"dynamic": args.watermark} if args.watermark else None),
        "metrics": not args.no_vmaf,
    }
    job = c.submit(body)
    if "job_id" not in job:
        _print_json(job)
        return 1
    jid = job["job_id"]
    print("job:", jid)
    cancelled = False

    def show(st: dict) -> None:
        nonlocal cancelled
        # `stage` is present but null before the worker starts, so a default of
        # "" in get() does not help: only None means "unknown".
        stage = st.get("stage") or ""
        print(f"  {st.get('state')} p={st.get('progress')}: {stage or '-'}")
        # Fire once. The worker clears the flag semantics only between rungs, so
        # re-issuing cancel on every poll is noise (and would mask a resume).
        if not cancelled and args.cancel_at and stage.startswith("encode") \
                and args.cancel_at in stage:
            print(f"  -> cancel at {stage}")
            c.cancel(jid)
            cancelled = True

    # `wait` owns the terminal-state loop and, unlike an inline `while True`,
    # gives up on its own deadline: a worker that died with the job in flight
    # used to leave this command polling forever.
    try:
        st = c.wait(jid, timeout=args.timeout, on_update=show)
    except TimeoutError as exc:
        print(f"  {exc}")
        return 1
    _print_json(st)
    return 0 if st.get("state") != "FAILURE" else 1


# ------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vodpipe", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="report ffmpeg/GPU/encoder availability").set_defaults(
        func=cmd_doctor)

    m = sub.add_parser("master", help="generate (or fetch) the source clip")
    m.add_argument("--src", default=DEFAULT_SRC)
    m.add_argument("--url", help="fetch this instead of synthesizing")
    m.add_argument("--duration", type=float, default=24.0)
    m.add_argument("--size", default="3840x2160")
    m.add_argument("--seed", type=int, default=42)
    m.set_defaults(func=cmd_master)

    d = sub.add_parser("demo", help="end-to-end pipeline, no Docker needed")
    d.add_argument("--src", default=DEFAULT_SRC)
    d.add_argument("--encoder", default="h264_nvenc", choices=sorted(BACKENDS))
    d.add_argument("--preset", default=None)
    d.add_argument("--rungs", default="2160p,1440p,1080p,720p,432p")
    d.add_argument("--chunks", type=int, default=1,
                   help="parallel chunks (1 = whole file; >1 only helps across GPUs)")
    d.add_argument("--watermark", default=None, help="viewer id to burn in")
    d.add_argument("--ghost", action="store_true", help="add the near-invisible layer")
    d.add_argument("--no-vmaf", action="store_true")
    d.add_argument("--min-free-gb", type=float, default=4.0)
    d.set_defaults(func=cmd_demo)

    b = sub.add_parser("bench", help="encoder/parallelism matrix")
    b.add_argument("--src", default=DEFAULT_SRC)
    b.add_argument("--rungs", default="2160p,1080p,720p")
    b.add_argument("--configs", default="all")
    b.add_argument("--vmaf", action="store_true")
    b.add_argument("--min-free-gb", type=float, default=6.0)
    b.set_defaults(func=cmd_bench)

    w = sub.add_parser("watermark", help="watermark overhead matrix")
    w.add_argument("--src", default=DEFAULT_SRC)
    w.add_argument("--rung", default="1080p")
    w.add_argument("--encoder", default="h264_nvenc")
    w.add_argument("--reps", type=int, default=2)
    w.add_argument("--min-free-gb", type=float, default=4.0)
    w.set_defaults(func=cmd_watermark)

    sub.add_parser("figures", help="render SVG charts from bench/*.json").set_defaults(
        func=cmd_figures)

    j = sub.add_parser("job", help="drive the HTTP API")
    j.add_argument("--api", default="http://127.0.0.1:8000")
    j.add_argument("--source", default=DEFAULT_SRC)
    j.add_argument("--encoder", default="h264_nvenc")
    j.add_argument("--rungs", default="1080p,720p")
    j.add_argument("--watermark", default=None)
    j.add_argument("--no-vmaf", action="store_true")
    j.add_argument("--status", default=None, help="poll this job instead of submitting")
    j.add_argument("--cancel", default=None, help="cancel this job id")
    j.add_argument("--cancel-at", default=None,
                   help="cancel when the stage matches, e.g. '1/3'")
    j.add_argument("--timeout", type=float, default=1800.0,
                   help="give up waiting after this many seconds (default 1800)")
    j.set_defaults(func=cmd_job)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ff.FFmpegNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
