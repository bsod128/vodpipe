# -*- coding: utf-8 -*-
"""Профили кодеков/энкодеров и ABR-лестница.

Один источник фактов для всех рангов: битрейты, разрешения, GOP, профили.
"""
from dataclasses import dataclass, field

from . import ff

# ---------------------------------------------------------------------------
# Энкодер-бэкенды
# ---------------------------------------------------------------------------


@dataclass
class Encoder:
    name: str            # имя для отчётов
    codec: str           # ffmpeg -c:v
    tag: str             # fourcc для mp4
    ext: str             # расширение промежуточного контейнера
    extra: list = field(default_factory=list)
    hw: bool = False

    def args(self) -> list:
        return ["-c:v", self.codec] + self.extra + ["-tag:v", self.tag]


def h264_nvenc(preset: str = "p5") -> Encoder:
    """NVENC H.264. p4/p5 — баланс скорость/качество; p1 — максимум скорости.
    spatial_aq + temporal_aq критичны: без них NVENC заметно хуже держит
    движение на тёмных сценах, чем x264.

    Написание опций (-spatial_aq против -spatial-aq) различается между сборками
    ffmpeg, поэтому имена подбираются по -h encoder на рантайме.
    """
    codec = "h264_nvenc"
    return Encoder(
        name="h264_nvenc",
        codec=codec,
        tag="avc1",
        ext="ts",
        hw=True,
        extra=[
            "-preset", preset,
            "-rc", "vbr",
            *ff.opt(codec, "spatial_aq", "spatial-aq"), "1",
            *ff.opt(codec, "temporal_aq", "temporal-aq"), "1",
            "-profile:v", "high",
            "-level", "5.1",
        ],
    )


def hevc_nvenc(preset: str = "p5") -> Encoder:
    """NVENC H.265 — для 4K-ранга (Smart TV), где H.264 на 4K уже нерентабелен."""
    codec = "hevc_nvenc"
    return Encoder(
        name="hevc_nvenc",
        codec=codec,
        tag="hvc1",
        ext="ts",
        hw=True,
        extra=[
            "-preset", preset,
            "-rc", "vbr",
            *ff.opt(codec, "spatial_aq", "spatial-aq"), "1",
            "-profile:v", "main",
        ],
    )


def h264_qsv(preset: str = "veryfast") -> Encoder:
    """Intel Quick Sync — на этой машине недоступен (нет Intel iGPU), оставлен
    для полноты сравнения и переносимости отчёта на другую железо-конфигурацию."""
    codec = "h264_qsv"
    return Encoder(
        name="h264_qsv",
        codec=codec,
        tag="avc1",
        ext="ts",
        hw=True,
        extra=["-preset", preset, "-profile:v", "high"],
    )


def libx264(preset: str = "medium") -> Encoder:
    """CPU baseline — точка отсчёта для замера ускорения."""
    return Encoder(
        name="libx264",
        codec="libx264",
        tag="avc1",
        ext="ts",
        extra=[
            "-preset", preset,
            "-profile:v", "high",
            "-level", "5.1",
            "-tune", "film",
        ],
    )


ENCODERS = {
    "h264_nvenc": h264_nvenc,
    "hevc_nvenc": hevc_nvenc,
    "h264_qsv": h264_qsv,
    "libx264": libx264,
}


# ---------------------------------------------------------------------------
# ABR-лестница
# ---------------------------------------------------------------------------


@dataclass
class Rung:
    label: str
    height: int
    width: int
    bitrate: int          # целевой видеобитрейт, бит/с
    maxrate: int
    bufsize: int
    codecs: list          # ранги могут быть в нескольких кодеках (H.265 + fallback H.264)
    devices: str          # для кого этот ранг в лестнице
    max_fps: int = 30
    audio: int = 128_000

    def scale(self, src_w: int, src_h: int) -> str:
        """Даунскейл с сохранением SAR/чотности (нечётные размеры ломают h264)."""
        h = self.height
        w = self.width if self.width else int(round(src_w * h / src_h / 2) * 2)
        return f"scale={w}:{h}:flags=lanczos"


# Классическая лестница под Smart TV / Mobile / Web.
# Верхний ранг — HEVC (4K), остальные — H.264, чтобы старые декодеры не отвалились.
#
# Все размеры — ровно 16:9 и кратные 8. Это не косметика: DASH-муxer отказывается
# писать манифест, если в одном Adaptation Set разные aspect ratio ("Conflicting
# stream aspect ratios values in Adaptation Set"). Классическое «480p = 854x480»
# даёт DAR 427:240, то есть не 16:9, и лестница из таких рангов не упаковывается.
# Поэтому нижние ранги — 432p (768x432) и 360p (640x360).
def ladder_4k(hevc_top: bool = True) -> list:
    rungs = [
        Rung("2160p", 2160, 3840, 28_000_000, 34_000_000, 42_000_000,
             ["hevc_nvenc"] if hevc_top else ["h264_nvenc"],
             "Smart TV 4K (Tizen/webOS, HEVC decode)"),
        Rung("1440p", 1440, 2560, 11_000_000, 14_000_000, 17_000_000,
             ["h264_nvenc"], "4K->1440 fallback, десктоп"),
        Rung("1080p", 1080, 1920, 6_500_000, 8_000_000, 10_000_000,
             ["h264_nvenc"], "Smart TV 1080, десктоп, планшет Wi-Fi"),
        Rung("720p", 720, 1280, 3_200_000, 4_000_000, 5_000_000,
             ["h264_nvenc"], "mobile 4G, старые Smart TV"),
        Rung("432p", 432, 768, 1_400_000, 1_800_000, 2_200_000,
             ["h264_nvenc"], "mobile 3G, слабый интернет"),
    ]
    return rungs
