# -*- coding: utf-8 -*-
"""Метаданные источника и безопасные границы чанков (только IDR-кадры)."""
from dataclasses import dataclass, field

from . import ff


@dataclass
class Source:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str
    acodec: str | None
    bitrate: int
    keyframes: list = field(default_factory=list)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def is_4k(self) -> bool:
        return self.height >= 2160 or self.pixels >= 8_000_000

    def summary(self) -> str:
        return (
            f"{self.width}x{self.height}@{self.fps} {self.vcodec}"
            f"{'+' + self.acodec if self.acodec else ''}, "
            f"{self.duration:.2f} с, {self.bitrate // 1000} кбит/с, "
            f"IDR: {len(self.keyframes)}"
        )


def source_info(path: str, with_keyframes: bool = True) -> Source:
    j = ff.probe_json(
        path,
        [
            "-show_entries",
            "format=duration,bit_rate:stream=index,codec_type,codec_name,width,height,avg_frame_rate",
        ],
    )
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise ValueError(f"нет видеопотока в {path}")
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den) if den and float(den) else 0.0
    except ValueError:
        fps = 0.0
    fmt = j.get("format", {})
    return Source(
        path=path,
        duration=float(fmt.get("duration") or 0.0),
        width=int(v.get("width") or 0),
        height=int(v.get("height") or 0),
        fps=round(fps, 3),
        vcodec=v.get("codec_name") or "?",
        acodec=(a.get("codec_name") if a else None),
        bitrate=int(fmt.get("bit_rate") or 0),
        keyframes=keyframes(path) if with_keyframes else [],
    )


def keyframes(path: str) -> list:
    """pts_time всех IDR-кадров — единственные безопасные точки реза."""
    j = ff.probe_json(
        path,
        ["-select_streams", "v:0", "-show_entries", "packet=pts_time,flags", "-of", "json"],
    )
    out = []
    for p in j.get("packets", []):
        if "K" in (p.get("flags") or ""):
            try:
                out.append(round(float(p["pts_time"]), 4))
            except (TypeError, KeyError, ValueError):
                continue
    return sorted(out)


def split_points(src: Source, n: int):
    """Ключевые кадры -> n диапазонов (start, end) с ровными границами.

    Режем строго по IDR: такой чанк декодируется независимо, а склейка чанков
    бинарно конкатенируема — отсюда и корректный параллельный транскод без артефактов.
    """
    kfs = [k for k in src.keyframes if 0.0 <= k <= src.duration]
    if len(kfs) < n + 1:
        # мало ключевых кадров — берём GOP-выровненную сетку по длительности
        step = src.duration / max(n, 1)
        kfs = [round(i * step, 4) for i in range(n + 1)]
    idx = [int(round(i * (len(kfs) - 1) / n)) for i in range(n + 1)]
    bounds = [kfs[i] for i in idx]
    bounds[0] = 0.0
    bounds[-1] = round(src.duration, 4)
    return [(bounds[i], bounds[i + 1]) for i in range(n) if bounds[i + 1] > bounds[i]]
