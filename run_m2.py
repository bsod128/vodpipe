# -*- coding: utf-8 -*-
"""M2: бенчмарк режимов кодирования на одном источнике.

Сравниваем:
  * libx264 whole            — CPU baseline (точка отсчёта ускорения)
  * h264_nvenc whole         — одна GPU-сессия
  * h264_nvenc chunked N/W   — параллельные GPU-сессии (N чанков, W воркеров)

Заодно фиксируем насыщение NVENC: пока прирост wall идёт почти линейно по W, а
потом упирается в число аппаратных сессий/пропускную способность памяти.

Использование:
    python run_m2.py [--rungs 2160p,1080p,720p] [--configs all] [--metrics]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vodpipe import encode as E, ff, gpu, probe, quality
from vodpipe.encoders import ENCODERS, ladder_4k

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src_clip", "4k_master.mp4")

# (tag, encoder, mode, nchunks, workers)
CONFIGS = [
    ("x264_medium_whole",  "libx264",     "whole",    1, 1),
    ("nvenc_p5_whole",     "h264_nvenc",  "whole",    1, 1),
    ("nvenc_p5_c2w2",      "h264_nvenc",  "chunked",  2, 2),
    ("nvenc_p5_c4w4",      "h264_nvenc",  "chunked",  4, 4),
    ("nvenc_p5_c8w8",      "h264_nvenc",  "chunked",  8, 8),
    ("nvenc_p5_c8w4",      "h264_nvenc",  "chunked",  8, 4),
    ("nvenc_p1_c8w8",      "h264_nvenc",  "chunked",  8, 8),
]


def free_gb(path: str) -> float:
    drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    du = __import__("shutil").disk_usage(drive)
    return du.free / 1e9


def run_one(tag, enc_name, mode, nchunks, workers, src, rung, work, do_vmaf, ref_size):
    enc = ENCODERS[enc_name]()
    if enc_name.startswith("hevc"):
        enc = ENCODERS[enc_name](preset="p5")
    elif enc_name == "h264_nvenc":
        enc = ENCODERS[enc_name](preset="p1" if "p1" in tag else "p5")
    else:
        enc = ENCODERS[enc_name](preset="medium")

    out = os.path.join(work, f"m2_{tag}_{rung.label}.ts")
    if mode == "whole":
        r = E.transcode_whole(src, rung, enc, out)
    else:
        r = E.transcode_chunked(src, rung, enc, out, nchunks, workers,
                                os.path.join(work, "m2_chunks"))
        v = E.verify_concat(out, src.duration, round(src.duration * src.fps))
        r["concat_ok"] = v["ok"]
        r["vpkts"] = v["vpkts"]
    r["tag"] = tag
    r["kbps"] = round(r["size"] * 8 / src.duration / 1000)
    if do_vmaf:
        r["vmaf"] = quality.vmaf(src.path, out, os.path.join(work, f"m2_{tag}_{rung.label}_vmaf.json"),
                                 ref_size=ref_size)
    print(f"  [{rung.label:>5}] {tag:<18} wall={r['wall_s']:>6}s "
          f"speed={r['speed_x']:>5}x kbps={r['kbps']:>6} "
          + (f"vmaf={r['vmaf'].get('vmaf_mean')} " if do_vmaf else "")
          + (f"concat_ok={r.get('concat_ok')}" if mode == "chunked" else ""))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--rungs", default="2160p,1080p,720p")
    ap.add_argument("--configs", default="all",
                    help="список тегов через запятую или all")
    ap.add_argument("--metrics", action="store_true", help="считать VMAF для каждого замера")
    ap.add_argument("--work", default=ff.work_root())
    ap.add_argument("--min-free-gb", type=float, default=4.0)
    args = ap.parse_args()

    work = os.path.join(args.work, "m2")
    os.makedirs(work, exist_ok=True)
    if free_gb(work) < args.min_free_gb:
        sys.exit(f"на диске {free_gb(work):.1f} ГБ свободно — мало для бенча "
                 f"(нужно {args.min_free_gb}); освободите место или дайте --work")

    print("ffmpeg:", ff.version())
    enc_names = {c[1] for c in CONFIGS}
    gpurep = gpu.pick_nvenc_gpu("h264_nvenc")
    print(gpu.summary(gpurep))
    if gpurep["picked"] is None:
        sys.exit("NVENC недоступен — бенч имеет смысл только с --configs x264")

    src = probe.source_info(args.src)
    print("source:", src.summary())
    ref_size = f"{src.width}x{src.height}"

    want = {c[0] for c in CONFIGS} if args.configs == "all" else set(args.configs.split(","))
    configs = [c for c in CONFIGS if c[0] in want]
    rungs = ladder_4k(hevc_top=False)
    sel = args.rungs.split(",")
    rungs = [r for r in rungs if r.label in sel]

    rows = []
    t0 = time.perf_counter()
    for rung in rungs:
        for (tag, enc_name, mode, nch, w) in configs:
            if enc_name.endswith("nvenc") and not ff.has_encoder(enc_name):
                print(f"  [{rung.label}] {tag}: пропуск — нет {enc_name}")
                continue
            rows.append(run_one(tag, enc_name, mode, nch, w, src, rung, work,
                                args.metrics, ref_size))
    total = time.perf_counter() - t0

    rep = {
        "meta": {"ffmpeg": ff.version(), "source": src.summary(),
                 "gpu": gpurep, "configs": [c[0] for c in configs],
                 "rungs": [r.label for r in rungs], "vmaf": args.metrics,
                 "total_wall_s": round(total, 1)},
        "rows": rows,
    }
    # ускорение относительно x264-baseline на том же ранге
    for r in rows:
        base = next((b for b in rows if b["rung"] == r["rung"]
                     and b["encoder"] == "libx264" and b["mode"] == "whole"), None)
        if base and base["wall_s"]:
            r["vs_x264"] = round(base["wall_s"] / r["wall_s"], 2)

    out_json = os.path.join(ROOT, "bench", "m2_bench.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print("\nускорение к x264 (same rung):")
    for r in rows:
        if "vs_x264" in r:
            print(f"  [{r['rung']:>5}] {r['tag']:<18} x{r['vs_x264']}")
    print("report ->", out_json)


if __name__ == "__main__":
    main()
