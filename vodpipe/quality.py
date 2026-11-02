# -*- coding: utf-8 -*-
"""Качество: VMAF (перцептуальная метрика, та же, что используют стриминговые
компании при тюнинге лестницы) + SSIM/PSNR как дешёвые контрольные точки.

VMAF считаем против того же ранга источника (scaled reference), иначе метрика
штрафует за разрешение, а не за кодирование.
"""
import json
import os
import re
import subprocess

from . import ff


def _fq(p: str) -> str:
    """Путь для log_path внутри filtergraph.

    Двоеточие в Windows-пути парсер графа читает как разделитель опций, поэтому
    его экранируем удвоенным обратным слэшем (граф съедает один слэш, второй
    экранирует двоеточие). Кавычки вокруг пути с двоеточием не помогают.
    """
    return p.replace("\\", "/").replace(":", "\\\\:")


def _run_metric(ref: str, dist: str, out_json: str, filter_name: str, extra=()):
    os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
    args = [
        ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", dist, "-i", ref,
        "-lavfi", f"[0:v][1:v]{filter_name}={':'.join(extra)}" if extra
        else f"[0:v][1:v]{filter_name}",
        "-f", "null", "-",
    ]
    # у libvmaf вывод в файл, у ssim/psnr — в stderr
    stdout, _, wall = ff.run(args)
    return wall


def vmaf(ref: str, dist: str, log_json: str, threads: int | None = None,
         ref_size: str | None = None) -> dict:
    """VMAF с upscale-методологией: дисторшн-поток апскейлится до размера
    референса, оба потока переводятся в один pix_fmt. Иначе libvmaf просто
    отвергает разные разрешения, а «сырой» downscale-референс штрафовал бы
    только даунскейл, а не качество кодирования.

    ref_size: "WxH" референса (обычно родное разрешение источника).
    threads: n_threads для libvmaf. По умолчанию 8: без явного значения метрика
    идёт в один поток (~1.8 fps на 4K), с 8 потоками — ~13.7 fps (замерено).
    Возвращает pooled VMAF (mean / harmonic mean — стандарт индустрии).
    """
    if threads is None:
        threads = max(1, min(8, os.cpu_count() or 1))
    w, h = (ref_size or "").lower().split("x") if ref_size else (None, None)
    if w and h:
        graph = (
            f"[0:v]scale={w}:{h}:flags=lanczos:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[d];"
            f"[1:v]format=yuv420p,setsar=1[r];"
        )
    else:
        graph = "[0:v]format=yuv420p[d];[1:v]format=yuv420p[r];"
    opts = [f"log_path={_fq(log_json)}", "log_fmt=json"]
    if threads:
        opts.append(f"n_threads={threads}")
    args = [
        ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-i", dist, "-i", ref,
        "-lavfi", graph + "[d][r]libvmaf=" + ":".join(opts),
        "-f", "null", "-",
    ]
    _, _, wall = ff.run(args)
    if not os.path.exists(log_json):
        return {"error": "no vmaf log"}
    j = json.load(open(log_json, encoding="utf-8", errors="replace"))
    # libvmaf в json пишет агрегаты в `pooled_metrics` (в старых версиях —
    # `pooled`), а метрика называется `vmaf`. Берём оба варианта, а если
    # агрегатов нет — считаем из пофреймового списка.
    pooled = (j.get("pooled_metrics") or j.get("pooled") or {}).get("vmaf") or {}
    frames = j.get("frames", [])
    vals = [f["metrics"].get("vmaf") for f in frames if "vmaf" in f.get("metrics", {})]
    vals = [v for v in vals if v is not None]
    mean = pooled.get("mean") or (sum(vals) / len(vals) if vals else 0)
    harm = pooled.get("harmonic_mean") or 0
    return {
        "vmaf_mean": round(mean, 2),
        "vmaf_harmonic": round(harm, 2),
        "vmaf_min": round(min(vals, default=0), 2),
        "vmaf_p99": round(sorted(vals)[int(len(vals) * 0.99) - 1], 2) if vals else 0,
        "frames": len(frames),
        "wall_s": round(wall, 2),
        "fps_metric": j.get("fps"),
    }


def ssim_psnr(ref: str, dist: str) -> dict:
    """Дёшево и без модели: полезно как sanity-check для бенча."""
    res = {}
    for name in ("ssim", "psnr"):
        args = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "info",
                "-i", dist, "-i", ref, "-lavfi", f"[0:v][1:v]{name}", "-f", "null", "-"]
        r = subprocess.run(args, capture_output=True, text=True, errors="replace")
        txt = r.stderr or ""
        if name == "ssim":
            m = re.search(r"All:([0-9.]+)", txt)
            if m:
                res["ssim"] = float(m.group(1))
        else:
            m = re.search(r"average ([0-9.]+) dB", txt)
            if m:
                res["psnr_db"] = float(m.group(1))
        res[name + "_wall_s"] = round(_last_wall(txt), 2)
    return res


def _last_wall(stderr: str) -> float:
    m = re.findall(r"speed=\s*([0-9.]+)x", stderr or "")
    return float(m[-1]) if m else 0.0


def bitrate(path: str) -> dict:
    j = ff.probe_json(path, ["-show_entries", "stream=codec_type,codec_name,bit_rate,width,height:format=bit_rate,duration"])
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), {})
    return {
        "video_kbps": int(v.get("bit_rate") or 0) // 1000,
        "audio_kbps": int(a.get("bit_rate") or 0) // 1000,
        "codec": v.get("codec_name"),
        "resolution": f"{v.get('width')}x{v.get('height')}",
        "total_kbps": int(j.get("format", {}).get("bit_rate") or 0) // 1000,
    }
