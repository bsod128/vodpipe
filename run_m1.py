# -*- coding: utf-8 -*-
"""M1: полный прогон лестницы 2160p->480p на 4K-источнике + упаковка HLS/DASH.

Использование:
    python run_m1.py [--encoder h264_nvenc|hevc_nvenc|libx264] [--chunked N W]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vodpipe import encode as E, ff, gpu, pipeline, probe
from vodpipe.encoders import ENCODERS, ladder_4k

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src_clip", "4k_master.mp4")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--encoder", default="h264_nvenc")
    ap.add_argument("--chunked", nargs=2, type=int, metavar=("NCHUNKS", "WORKERS"))
    ap.add_argument("--no-metrics", action="store_true")
    ap.add_argument("--work", default=ff.work_root())
    ap.add_argument("--out", default=os.path.join(ROOT, "out"))
    args = ap.parse_args()

    print("ffmpeg:", ff.version())
    if not os.path.exists(args.src):
        sys.exit(f"нет источника {args.src}")

    enc = ENCODERS[args.encoder]()
    print(f"encoder available: {enc.codec} -> {ff.has_encoder(enc.codec)}")
    if enc.hw and not ff.has_encoder(enc.codec):
        sys.exit("энкодер недоступен в этой сборке")

    # Индекс GPU не хардкодим: он зависит от конфигурации железа (RTX 3060
    # сдвинулась с 3 на 4, когда появился четвёртый V100).
    gpurep = None
    if enc.hw:
        gpurep = gpu.pick_nvenc_gpu(enc.codec)
        print(gpu.summary(gpurep))
        if gpurep["picked"] is None:
            sys.exit("нет GPU с рабочим NVENC — нужен --encoder libx264")

    work = os.path.join(args.work, "m1")
    os.makedirs(work, exist_ok=True)
    p = pipeline.Pipeline(args.src, work)
    print("source:", p.src.summary())
    if not p.src.is_4k():
        print("!! источник не 4K")

    tag = args.encoder + ("_chunk" + str(args.chunked[0]) if args.chunked else "")
    results, wall = p.run_ladder(
        encoder_name=args.encoder,
        chunk_mode="chunked" if args.chunked else "whole",
        nchunks=args.chunked[0] if args.chunked else 1,
        workers=args.chunked[1] if args.chunked else 1,
        tag=tag,
        do_metrics=not args.no_metrics,
    )
    print(f"ladder wall = {wall:.1f} s")

    # проверка склейки: на склеенном TS метаданные о длительности врут,
    # поэтому сверяем число видео-пакетов с ожидаемым числом кадров
    if args.chunked:
        expect_frames = round(p.src.duration * p.src.fps)
        for r in results:
            v = E.verify_concat(r["out"], p.src.duration, expect_frames)
            print(f"  concat check {r['rung']}: ok={v['ok']} "
                  f"vpkts={v['vpkts']}/{expect_frames}")
        rep_concat = {"expected_frames": expect_frames}
    else:
        rep_concat = {}

    pkg = p.package(results, args.out)
    print("packaged:", pkg["hls"]["variants"], "variants;",
          "segments:", pkg["hls"]["segments"])
    print("ladder from manifest:", pkg["ladder"]["ladder"])

    if not args.no_metrics:
        try:
            vm = p.vmaf_for(results, tag=tag + "_")
        except Exception as e:
            print("VMAF failed:", e)
            vm = {"error": str(e)}
    else:
        vm = {}

    rep = {
        "meta": {"ffmpeg": ff.version(), "encoder": args.encoder,
                 "chunked": args.chunked, "source": p.src.summary(),
                 "source_path": p.src.path, "duration": p.src.duration,
                 "gpu": gpurep, "concat": rep_concat,
                 "keyframes": len(p.src.keyframes)},
        "encode": results,
        "ladder_wall_s": wall,
        "packaging": pkg,
        "vmaf": vm,
    }
    out_json = pipeline.dump(os.path.join(ROOT, "bench", "m1_" + tag + ".json"), rep)
    print("report ->", out_json)


if __name__ == "__main__":
    main()
