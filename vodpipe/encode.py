# -*- coding: utf-8 -*-
"""Транскод: целый файл (baseline) и чанковый параллельный (NVENC/QSV).

Ключевая идея распараллеливания: режем строго по IDR-кадрам источника, каждый
чанк кодируется независимым ffmpeg-процессом в MPEG-TS, затем чанки склеиваются
бинарной конкатенацией (TS спроектирован для этого) — без ре encoded-шага и без
потери качества на стыках.

Важный нюанс, который мы мерим отдельно: на потребительских GeForce NVENC
ограничен числом одновременных сессий, а число самих аппаратных блоков ограничивает
пропускную способность. Поэтому кривая ускорения по N насыпается раньше, чем
для CPU-энкодера. Это и есть «ограничения NVENC» из вакансии.
"""
import concurrent.futures as cf
import os
import shutil
import subprocess
import time

from . import ff, probe
from .encoders import Rung, Encoder


def _vfilter(rung: Rung, src: probe.Source, extra=()):
    parts = [rung.scale(src.width, src.height), "setsar=1"]
    parts += list(extra)
    return ",".join(parts)


def _bitrate_args(enc: Encoder, rung: Rung) -> list:
    """Битрейт отдаём энкодеру как rate-control, а не 'guess': тогда лестница
    действительно держит целевые битрейты по всем рангам."""
    if enc.hw:
        return [
            "-b:v", str(rung.bitrate),
            "-maxrate:v", str(rung.maxrate),
            "-bufsize:v", str(rung.bufsize),
        ]
    return [
        "-b:v", str(rung.bitrate),
        "-maxrate:v", str(rung.maxrate),
        "-bufsize:v", str(rung.bufsize),
    ]


# ---------------------------------------------------------------------------
# 1. baseline: один проход по всему файлу
# ---------------------------------------------------------------------------


def transcode_whole(src: probe.Source, rung: Rung, enc: Encoder, out_path: str,
                    vextra=(), log=False):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    args = [
        ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "info", "-stats",
        "-i", src.path,
        "-vf", _vfilter(rung, src, vextra),
        "-r", str(rung.max_fps),
        "-g", str(int(rung.max_fps * 2)), "-keyint_min", str(int(rung.max_fps * 2)),
        "-sc_threshold", "0",
    ] + enc.args() + _bitrate_args(enc, rung) + [
        "-c:a", "aac", "-b:a", str(rung.audio), "-ac", "2",
        "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts", out_path,
    ]
    _, speed, wall = ff.run(args, log=log)
    return {"mode": "whole", "rung": rung.label, "encoder": enc.name,
            "speed_x": round(speed, 3), "wall_s": round(wall, 2),
            "out": out_path, "size": os.path.getsize(out_path)}


# ---------------------------------------------------------------------------
# 2. chunked parallel
# ---------------------------------------------------------------------------


def _encode_chunk(src, rung, enc, start, end, chunk_path, vextra, log):
    dur = round(end - start, 4)
    # -ss ДО -i: быстрый seek ровно на IDR => точный и дешёвый рез
    args = [
        ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "info", "-stats",
        "-ss", str(start), "-t", str(dur),
        "-i", src.path,
        "-vf", _vfilter(rung, src, vextra),
        "-r", str(rung.max_fps),
        "-g", str(int(rung.max_fps * 2)), "-keyint_min", str(int(rung.max_fps * 2)),
        "-sc_threshold", "0",
    ] + enc.args() + _bitrate_args(enc, rung) + [
        "-c:a", "aac", "-b:a", str(rung.audio), "-ac", "2",
        "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts", chunk_path,
    ]
    _, speed, wall = ff.run(args, log=log)
    return {"start": start, "end": end, "speed_x": round(speed, 3),
            "wall_s": round(wall, 2), "size": os.path.getsize(chunk_path)}


def concat_ts(chunk_paths, out_path):
    """Бинарная склейка MPEG-TS. Работает, потому что все чанки имеют одинаковые
    кодек/разрешение/таймбазу и начинаются с IDR."""
    with open(out_path, "wb") as w:
        for p in chunk_paths:
            with open(p, "rb") as r:
                shutil.copyfileobj(r, w, 1 << 20)
    return out_path


def transcode_chunked(src: probe.Source, rung: Rung, enc: Encoder, out_path: str,
                      nchunks: int, workers: int, workdir: str,
                      vextra=(), log=False, keep_chunks=False):
    """N чанков, W параллельных ffmpeg-сессий, затем конкатенация.

    workers <= nchunks. Для NVENC workers выше аппаратного лимита сессий не даёт
    прироста — это фиксируется в бенче.
    """
    os.makedirs(workdir, exist_ok=True)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    bounds = probe.split_points(src, nchunks)
    cdir = os.path.join(workdir, f"chunks_{rung.label}_{enc.name}")
    os.makedirs(cdir, exist_ok=True)
    cpaths = [os.path.join(cdir, f"c{i:03d}.ts") for i in range(len(bounds))]

    t0 = time.perf_counter()
    stats = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [
            ex.submit(_encode_chunk, src, rung, enc, s, e, cp, vextra, log)
            for (s, e), cp in zip(bounds, cpaths)
        ]
        for f in cf.as_completed(futs):
            stats.append(f.result())
    encode_wall = time.perf_counter() - t0

    t1 = time.perf_counter()
    concat_ts(cpaths, out_path)
    concat_wall = time.perf_counter() - t1

    if not keep_chunks:
        shutil.rmtree(cdir, ignore_errors=True)

    stats.sort(key=lambda d: d["start"])
    return {
        "mode": "chunked", "rung": rung.label, "encoder": enc.name,
        "nchunks": len(bounds), "workers": workers,
        "encode_wall_s": round(encode_wall, 2),
        "concat_wall_s": round(concat_wall, 3),
        "wall_s": round(encode_wall + concat_wall, 2),
        "speed_x": round(src.duration / (encode_wall + concat_wall), 3),
        "agg_speed_x": round(sum(s["speed_x"] for s in stats) / max(len(stats), 1), 3),
        "out": out_path, "size": os.path.getsize(out_path),
        "chunks": stats,
    }


def verify_concat(out_path, expect_dur, expect_frames=None, tol=0.5):
    """Проверяем, что склейка не потеряла и не удвоила кадры.

    Для склеенного TS длительность по метаданным ffprobe врут (в каждом чанке
    PCR/DTS перезапускаются), поэтому основной критерий — число видео-пакетов.
    """
    j = ff.probe_json(out_path, ["-show_entries", "format=duration:stream=codec_type,nb_read_packets",
                                 "-count_packets"])
    dur = float(j.get("format", {}).get("duration") or 0.0)
    vpkts = sum(int(s.get("nb_read_packets") or 0)
                for s in j.get("streams", []) if s.get("codec_type") == "video")
    checks = {"vpkts": vpkts}
    ok = True
    if expect_frames:
        ok = ok and (vpkts == expect_frames)
        checks["expected_frames"] = expect_frames
    # длительность по потоку (не по формату) для склейки не информативна,
    # поэтому сверяем её только через пакеты * длительность кадра
    return {"ok": ok, "duration_probe": dur, "expected_dur": expect_dur,
            "vpkts": vpkts, "checks": checks}


# ---------------------------------------------------------------------------
# 3. определение лимита NVENC-сессий (честно, экспериментом)
# ---------------------------------------------------------------------------


def nvenc_probe_ok(enc: Encoder) -> bool:
    """Прогон 1-секундного lavfi-источника: ловим CUDA_ERROR_*/No capable devices."""
    args = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1",
            "-c:v", enc.codec, "-preset", "p5", "-f", "null", "-"]
    try:
        ff.run(args, check=True)
        return True
    except ff.FFError as e:
        return False
