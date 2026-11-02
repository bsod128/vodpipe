# -*- coding: utf-8 -*-
"""M3: overhead вотермарков — сколько стоит защита контента в пайплайне.

Сравнивает (один и тот же ранг, один и тот же энкодер):
  * none        — чистый транскод (база)
  * static      — статичный логотип + текст в углу
  * dynamic     — дрейфующий персональный текст
  * dyn+ghost   — дрейфующий текст + почти невидимый второй слой
  * twopass     — вотермарк ОТДЕЛЬНЫМ проходом после транскода (для сравнения:
                  это то, чего избегают, overhead ~2x)

Все «fused» режимы — вотермарк в том же filtergraph, что и scale, то есть один
проход. Замеряем дельту wall/throughput и дельту VMAF (вотермарк меняет пиксели,
поэтому метрика против исходника падает — важно знать насколько).

Использование:
    python run_m3.py [--rung 1080p] [--configs none,static,dynamic,dyn_ghost]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vodpipe import encode as E, ff, gpu, pipeline, probe, quality, watermark as WM
from vodpipe.encoders import ENCODERS, ladder_4k

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src_clip", "4k_master.mp4")
VIEWER = "viewer-42f7a9"     # имитация персонального ID подписчика


def variants():
    """Каждый вариант: (tag, wm_kwargs, two_pass).

    wm-значения здесь сырые (текст/bool), как их присылает API: Pipeline.wm_chain
    сам превращает их в фильтры. Так бенч мерит ровно тот же код, что и прод.
    """
    return [
        ("none",      {},                                        False),
        ("static",    {"static": "MAXIMA VOD"},                  False),
        ("dyn",       {"dynamic": VIEWER},                       False),
        ("dyn_ghost", {"dynamic": VIEWER, "ghost": True},        False),
        ("twopass",   {"static": "MAXIMA VOD"},                  True),
    ]


def twopass(src, rung, enc, out, work, wm):
    """База «плохого» подхода: сначала чистый транскод, потом второй проход с вотермарком."""
    base = os.path.join(work, "twopass_base.ts")
    r1 = E.transcode_whole(src, rung, enc, base)
    chain = pipeline.Pipeline(src.path, work, wm=wm).wm_chain()
    args = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "info", "-stats",
            "-i", base, "-vf", chain] + enc.args() + [
            "-b:v", str(rung.bitrate), "-maxrate:v", str(rung.maxrate),
            "-bufsize:v", str(rung.bufsize),
            "-c:a", "copy", "-f", "mpegts", out]
    import time
    t0 = time.perf_counter()
    _, speed, wall = ff.run(args)
    r2 = {"wall_s": round(time.perf_counter() - t0, 2)}
    return {
        "mode": "twopass", "rung": rung.label, "encoder": enc.name,
        "wall_s": round(r1["wall_s"] + r2["wall_s"], 2),
        "speed_x": round(src.duration / (r1["wall_s"] + r2["wall_s"]), 3),
        "pass1_wall_s": r1["wall_s"], "pass2_wall_s": r2["wall_s"],
        "out": out, "size": os.path.getsize(out),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--rung", default="1080p")
    ap.add_argument("--encoder", default="h264_nvenc")
    ap.add_argument("--configs", default="none,static,dyn,dyn_ghost,logo,twopass")
    ap.add_argument("--reps", type=int, default=2, help="повторов на вариант (шум замеров)")
    ap.add_argument("--work", default=ff.work_root())
    args = ap.parse_args()

    work = os.path.join(args.work, "m3")
    os.makedirs(work, exist_ok=True)
    print("ffmpeg:", ff.version())
    gpurep = gpu.pick_nvenc_gpu("h264_nvenc")
    print(gpu.summary(gpurep))
    if gpurep["picked"] is None:
        sys.exit("NVENC недоступен")

    src = probe.source_info(args.src)
    print("source:", src.summary())
    ref_size = f"{src.width}x{src.height}"

    enc = ENCODERS[args.encoder]()
    rung = next(r for r in ladder_4k(hevc_top=False) if r.label == args.rung)

    want = set(args.configs.split(","))
    rows = []
    for (tag, wm, two) in variants():
        if tag not in want:
            continue
        walls, vmafs = [], []
        for rep in range(args.reps):
            out = os.path.join(work, f"m3_{tag}_r{rep}.ts")
            if two:
                r = twopass(src, rung, enc, out, work, wm)
            else:
                # whole chain одним элементом: дробить по запятой нельзя, внутри
                # drawtext свои экранированные запятые (mod(t\,...))
                chain = pipeline.Pipeline(args.src, work, wm=wm).wm_chain()
                r = E.transcode_whole(src, rung, enc, out,
                                      vextra=[chain] if chain else ())
            walls.append(r["wall_s"])
            vmafs.append(quality.vmaf(src.path, out,
                                      os.path.join(work, f"m3_{tag}_r{rep}_vmaf.json"),
                                      ref_size=ref_size))
            os.remove(out)
            if two:
                b = os.path.join(work, "twopass_base.ts")
                if os.path.exists(b):
                    os.remove(b)
        wall = round(sum(walls) / len(walls), 2)
        vmean = round(sum(v.get("vmaf_mean", 0) for v in vmafs) / len(vmafs), 2)
        rows.append({"tag": tag, "two_pass": two, "rung": rung.label,
                     "wall_s": wall,
                     "speed_x": round(src.duration / wall, 3),
                     "size": r["size"],
                     "kbps": round(r["size"] * 8 / src.duration / 1000),
                     "vmaf_mean": vmean,
                     "vmaf_min": round(sum(v.get("vmaf_min", 0) for v in vmafs) / len(vmafs), 2),
                     "reps": walls})
        print(f"  {tag:<10} wall={wall:>7}s speed={rows[-1]['speed_x']:>5}x "
              f"vmaf={vmean:>6} kbps={rows[-1]['kbps']:>6}")

    base = next((r for r in rows if r["tag"] == "none"), None)
    if base:
        for r in rows:
            r["overhead_pct"] = round((r["wall_s"] / base["wall_s"] - 1) * 100, 1)
            r["vmaf_drop"] = round(base["vmaf_mean"] - r["vmaf_mean"], 2)

    rep = {"meta": {"ffmpeg": ff.version(), "source": src.summary(),
                    "rung": rung.label, "encoder": enc.name, "reps": args.reps,
                    "gpu": gpurep},
           "rows": rows}
    out_json = os.path.join(ROOT, "bench", "m3_watermark.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)

    print("\noverhead к чистому транскоду:")
    for r in rows:
        print(f"  {r['tag']:<10} +{r['overhead_pct']:>5}% wall   "
              f"VMAF -{r['vmaf_drop']:>5}   {r['kbps']} kbps")
    print("report ->", out_json)


if __name__ == "__main__":
    main()
