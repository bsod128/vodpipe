# -*- coding: utf-8 -*-
"""Выбор GPU для NVENC без захардкоженного индекса.

Индекс карты в nvidia-smi зависит от конфигурации железа: на этой машине
появился четвёртый V100, и RTX 3060 сдвинулась с 3 на 4. Серверные Tesla V100
рабочего NVENC-контекста не дают, поэтому ffmpeg без CUDA_VISIBLE_DEVICES падает
то с "No capable devices found", то с CUDA_ERROR_OUT_OF_MEMORY.

Решение: индекс не запоминать, а перебирать карты и делать на каждой
полусекундный NVENC-прогон. Это же и диагностика: видно, почему карта отброшена.
"""
import os
import re
import subprocess

from . import ff

_SMI_ARGS = ["nvidia-smi",
             "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"]


def gpus() -> list:
    """Карты: [{index, name, used, total, util}] по nvidia-smi."""
    try:
        r = subprocess.run(_SMI_ARGS, capture_output=True, text=True, errors="replace")
    except FileNotFoundError:
        return []
    out = []
    for line in (r.stdout or "").strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 5:
            continue
        try:
            out.append({"index": int(p[0]), "name": p[1], "used": int(p[2]),
                        "total": int(p[3]), "util": int(p[4])})
        except ValueError:
            pass
    return out


def _rank(g: dict) -> tuple:
    """GeForce/RTX вперёд (у них гарантированно рабочий NVENC), затем по объёму
    свободной VRAM."""
    consumer = 0 if re.search(r"GeForce|RTX|GTX", g["name"], re.I) else 1
    free = g["total"] - g["used"]
    return (consumer, -free, g["util"])


def probe_index(idx: int, codec: str = "h264_nvenc") -> tuple:
    """Прогон 1 с @ 640x360 на конкретной карте -> (ok, причина отказа)."""
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(idx))
    args = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1",
            "-c:v", codec, "-preset", "p5", "-f", "null", "-"]
    r = subprocess.run(args, capture_output=True, text=True, errors="replace", env=env)
    if r.returncode == 0:
        return True, "ok"
    return False, " ".join((r.stderr or "").split())[:200]


_PICK_CACHE = {}


def pick_nvenc_gpu(codec: str = "h264_nvenc", set_env: bool = True,
                   refresh: bool = False) -> dict:
    """Найти карту, где NVENC реально кодирует, и зафиксировать её в окружении.

    Кэшируем по кодеку на процесс: прогон по всем картам стоит несколько секунд
    на каждую отказавшую, а в воркере это происходило бы на каждой задаче.
    refresh=True (или VODPIPE_GPU_REFRESH=1) — когда железо могло измениться.
    """
    if not refresh and not os.environ.get("VODPIPE_GPU_REFRESH"):
        hit = _PICK_CACHE.get(codec)
        if hit is not None:
            if set_env and hit["picked"] is not None:
                os.environ["CUDA_VISIBLE_DEVICES"] = str(hit["picked"])
            return hit

    report = {"codec": codec, "cards": [], "picked": None}
    for g in sorted(gpus(), key=_rank):
        ok, msg = probe_index(g["index"], codec)
        report["cards"].append(dict(g, nvenc=ok, reason=msg))
        if ok and report["picked"] is None:
            report["picked"] = g["index"]
            if set_env:
                os.environ["CUDA_VISIBLE_DEVICES"] = str(g["index"])
    # Кэшируем только успех: отрицательный результат мог быть вызван временной
    # занятостью VRAM, и запоминать его надолго нельзя. Если железо поменялось,
    # воркер перезапускают (или ставят VODPIPE_GPU_REFRESH=1).
    if report["picked"] is not None:
        _PICK_CACHE[codec] = report
    return report


def summary(rep: dict) -> str:
    lines = []
    for c in rep["cards"]:
        flag = "NVENC OK" if c["nvenc"] else "NVENC нет"
        note = "" if c["nvenc"] else f"  <- {c['reason'][:80]}"
        lines.append(f"  GPU {c['index']} {c['name']:<26} "
                     f"free={c['total'] - c['used']:>5}MiB util={c['util']:>3}% "
                     f"{flag}{note}")
    lines.append("  NVENC не работает ни на одной карте" if rep["picked"] is None
                 else f"  CUDA_VISIBLE_DEVICES={rep['picked']}")
    return "\n".join(lines)
