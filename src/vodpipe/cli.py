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
from . import encoders, ff, figures, gpu, packager, probe, report, sample
from . import pipeline as pl
from .encoders import BACKENDS, validate_preset
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
    h = ff.health()
    # health() reports a missing binary as ffmpeg="missing" instead of raising,
    # so the encoders/filters maps come back empty. Printing them would read as
    # "no encoders" and "all filters present" - both wrong, and confusing on the
    # one command a fresh checkout is told to run first. Say the real problem and
    # stop.
    if h.ffmpeg == "missing":
        print("MISSING")
        print(f"  {h.detail}")
        print("  install ffmpeg 7+ or set VODPIPE_FFMPEG / VODPIPE_FFPROBE")
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


def demo_arg_errors(args: argparse.Namespace) -> str | None:
    """Reject a self-contradictory `demo` invocation before anything is touched.

    Pure and first, because every one of these is a command that would otherwise
    run for ten minutes and report something other than what was asked for.
    """
    # A fallback only means something if the codecs differ: two H.264 ladders in
    # one manifest is not a fallback, it is a duplicate that halves the ladder.
    # The family comes from the encoder's own fourcc, so this cannot drift from
    # what the encoder actually writes.
    if args.fallback and (packager.codec_family(encoders.build(args.fallback).tag)
                          == packager.codec_family(encoders.build(args.encoder).tag)):
        return (f"--fallback {args.fallback} is the same codec family as --encoder "
                f"{args.encoder}; nothing to fall back to")
    # Without this, `--encoder hevc_nvenc --fallback-preset p7` runs one ladder at
    # the primary's preset and never says the flag did nothing.
    if args.fallback_preset and not args.fallback:
        return "--fallback-preset needs --fallback; there is no second ladder to preset"
    if args.watermark and not is_safe_id(args.watermark):
        return f"refusing watermark id {args.watermark!r}: use [A-Za-z0-9_.:@#-]"
    return None


def cmd_demo(args: argparse.Namespace) -> int:
    """The one-button path: probe -> ladder -> verify -> package -> VMAF.

    With `--fallback`, two ladders are encoded into ONE manifest: clients that
    cannot decode the primary codec must still find a complete ladder, and two
    separate packager runs would produce two masters, where a client never sees
    the second one.
    """
    bad = demo_arg_errors(args)
    if bad:
        print(bad)
        return 2
    sample.require_space(WORK, args.min_free_gb)
    if not os.path.exists(args.src):
        print(f"no source at {args.src}; run: vodpipe master")
        return 2

    encoders_to_run = [args.encoder] + ([args.fallback] if args.fallback else [])
    gpurep = None
    for name in encoders_to_run:
        if not name.endswith("nvenc"):
            continue
        rep = gpu.pick(name)
        print(gpu.summary(rep))
        if rep["picked"] is None:
            print(f"NVENC unavailable for {name}; rerun with --encoder libx264")
            return 1
        gpurep = gpurep or rep

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

    # The id itself is rejected up front by `demo_arg_errors`; this only maps the
    # flags to the filter spec the encoder receives.
    wm_spec = None
    if args.watermark:
        wm_spec = {"dynamic": args.watermark, "ghost": bool(args.ghost)}

    # Everything is keyed by the report's basename, so one command shape writes
    # either the whole-file ladder (demo.json) or a chunked one
    # (m1_h264_nvenc_chunk8.json) without their work/ or out/ dirs colliding.
    stem = os.path.splitext(args.report)[0]
    work = os.path.join(WORK, stem)
    out = os.path.join(OUT, stem)
    pipe = pl.Pipeline(args.src, work, wm_spec=wm_spec)
    print("source:", pipe.src.summary())
    if not pipe.src.is_4k():
        print("note: source is not 4K; numbers will not match the README tables")

    def progress(i: int, n: int, results: list) -> None:
        done = sum(r.get("wall_s", 0) for r in results)
        print(f"    progress {i}/{n} rungs, {done:.1f}s of encode so far")

    workers = args.workers or args.chunks
    # The effective preset: what the encoder actually used, not the raw argument
    # (which is None when the caller let the backend choose). Reports must state
    # the command that was really run. Presets are per-backend: the NVENC "p5" is
    # a hard ffmpeg error on libx264, so each ladder resolves its own.
    ladders_to_run = [(args.encoder, validate_preset(args.encoder, args.preset))]
    if args.fallback:
        ladders_to_run.append(
            (args.fallback, validate_preset(args.fallback, args.fallback_preset)))

    t0 = time.perf_counter()
    ladders: dict[str, list[dict]] = {}
    wall = 0.0
    for name, preset in ladders_to_run:
        print(f"ladder: {name} ({preset})")
        results, run_wall = pipe.run_ladder(encoder=name, preset=preset,
                                           nchunks=args.chunks, workers=workers,
                                           tag=stem, rungs=rungs, on_rung_done=progress)
        ladders[name] = results
        wall += run_wall
        print(f"  {name} ladder wall = {run_wall:.1f}s")
    print(f"ladder wall = {wall:.1f}s"
          + (f" across {len(ladders)} codecs" if len(ladders) > 1 else ""))

    all_results = [r for results in ladders.values() for r in results]
    verify = pipe.verify(all_results)
    for v in verify:
        print(f"  concat check {v['rung']}: ok={v['ok']} vpkts={v['vpkts']}"
              f"/{v['expected_frames']}")

    if args.fallback:
        # Primary first: within a family the packager orders renditions by
        # bandwidth, but the ladder a client reads back should list the codec it
        # prefers first, and a mixed-codec manifest is only legal if each codec
        # lands in its own AdaptationSet.
        pkg = pipe.package_dual(ladders[args.encoder], ladders[args.fallback], out)
    else:
        pkg = pipe.package(all_results, out)
    if pkg["hls"]:
        print("packaged:", len(pkg["hls"]["variants"]), "HLS variants,",
              pkg["hls"]["segments"], "segments;",
              len(pkg["dash"]["representations"]), "DASH representations")
        print("ladder from manifest:", pkg["ladder"]["ladder"])
        print("codecs in manifest:", pkg["ladder"]["codecs"])

    # A dual run delivers the same resolution under two codecs, so VMAF has to be
    # keyed by codec too - keyed by rung alone, the fallback's number would
    # silently overwrite the primary's.
    dual = len(ladders) > 1
    vmaf = {} if args.no_vmaf else pipe.vmaf_for(
        all_results, tag=stem + "_",
        key=(lambda r: f"{r['rung']}_{r['encoder']}") if dual else None)
    total = round(time.perf_counter() - t0, 2)

    rep = report.build_report(
        job_id=stem,
        meta=report.job_meta(ffmpeg=ff.version(), encoder=args.encoder,
                             preset=ladders_to_run[0][1], nchunks=args.chunks, workers=workers,
                             source=pipe.src.summary(), duration=pipe.src.duration,
                             gpurep=gpurep, watermark=wm_spec,
                             rungs=[r.label for r in rungs], fallback=args.fallback,
                             fallback_preset=ladders_to_run[1][1] if args.fallback else None),
        encode=all_results, ladder_wall_s=wall, packaging=pkg, vmaf=vmaf,
        verify=verify, total_wall_s=total)
    path = report.dump(os.path.join(BENCH, args.report), report.sanitize(rep, ROOT))
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

    if args.repeats > 1:
        return _cmd_bench_noise(args, src, gpurep, sel, work)
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


def _cmd_bench_noise(args: argparse.Namespace, src, gpurep, sel, work) -> int:
    """Repeat the matrix and publish the run-to-run spread, not an average.

    Every README comparison against measurement noise points at this file, so it
    has to be a committed artifact like the rest, not a number quoted from a
    throwaway run.
    """
    t0 = time.perf_counter()
    rows = bench_mod.noise_runs(src, work, sel, repeats=args.repeats)
    rep = {
        "meta": {"ffmpeg": ff.version(), "source": src.summary(), "gpu": gpurep,
                 "configs": sel.configs, "rungs": sel.rungs, "repeats": args.repeats,
                 "total_wall_s": round(time.perf_counter() - t0, 1)},
        "rows": rows,
    }
    path = report.dump(os.path.join(BENCH, "m2_noise.json"), report.sanitize(rep, ROOT))
    print("\nrun-to-run wall spread:")
    for r in rows:
        print(f"  [{r['rung']:>5}] {r['tag']:<18} {r['min_s']:>6}-{r['max_s']} s "
              f"spread {r['spread_pct']}%")
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


def ladder_rows(rep: dict) -> list[dict]:
    """Join a report's encode rows to the manifest and the VMAF block.

    The join key is the **rendition label**, and it has to be: a dual-codec
    report delivers the same resolution twice, so keying by `rung_res` collapses
    both ladders onto one bitrate, and keying VMAF by `rung` misses every entry
    because `cmd_demo` keys a dual run by `<rung>_<encoder>`. The label is
    recovered from the variant playlist URI, which `package_hls` derives from the
    same label the encode rows carry, so the two sides agree by construction.
    Single-codec artifacts are unaffected - there the label *is* the rung.
    """
    pkg = rep.get("packaging") or {}
    by_label = {packager.variant_label(v.get("uri", "")): v["bandwidth"] // 1000
                for v in (pkg.get("hls") or {}).get("variants", [])}
    vmaf = rep.get("vmaf") or {}
    rows = []
    for e in rep.get("encode") or []:
        if e.get("cancelled"):
            continue
        candidates = [f"{e['rung']}_{e['encoder']}", e["rung"]]
        key = next((k for k in candidates if k in by_label or k in vmaf), candidates[-1])
        m = vmaf.get(key) or {}
        rows.append({
            "rung": key,
            "wall_s": e.get("wall_s"),
            "total_kbps": by_label.get(key, 0),
            "vmaf_mean": m.get("vmaf_mean", 0),
            "vmaf_min": m.get("vmaf_min", 0),
        })
    return rows


def ladder_figures(rep: dict, out_dir: str) -> list[str]:
    """Charts for one ladder report.

    The bitrate comes from the packaging block, not from the file size:
    BANDWIDTH in the master playlist is audio+video combined and is the number
    a CDN and a player actually see, while file size also carries container
    overhead. See `ladder_rows` for how a row is matched to its manifest entry.
    """
    rows = ladder_rows(rep)
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
    d.add_argument("--fallback", default=None, choices=sorted(BACKENDS),
                   help="encode a second ladder into the SAME manifest, for clients "
                        "that cannot decode --encoder (e.g. --encoder hevc_nvenc "
                        "--fallback h264_nvenc)")
    d.add_argument("--fallback-preset", default=None,
                   help="preset for --fallback (its vocabulary is per-backend)")
    d.add_argument("--rungs", default="2160p,1440p,1080p,720p,432p")
    d.add_argument("--chunks", type=int, default=1,
                   help="parallel chunks (1 = whole file; >1 only helps across GPUs)")
    d.add_argument("--workers", type=int, default=None,
                   help="parallel workers (defaults to --chunks)")
    d.add_argument("--report", default="demo.json",
                   help="report file name under bench/ (also keys the work/out dirs)")
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
    b.add_argument("--repeats", type=int, default=1,
                   help="run the matrix N times and write the run-to-run wall "
                        "spread to bench/m2_noise.json instead of m2_bench.json")
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
