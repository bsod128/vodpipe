"""Encoder backends.

An encoder is a value object: a name, a codec, and a list of extra argv. The
pipeline never branches on "is this hardware", except through `hw`, which only
decides whether a GPU has to be picked. Adding a new backend means adding a
factory here, not editing the encode path (open/closed).

Two facts shape the defaults:

* **Rate control must be explicit.** `-b:v` alone lets the encoder drift, so
  every rung passes `-b:v/-maxrate:v/-bufsize:v` (see `rate_args`).
* **Option spellings differ between builds.** `h264_nvenc` takes `-spatial_aq`
  in one build and `-spatial-aq` in another; the wrong guess does not degrade,
  it aborts with "Unrecognized option". `Encoder.args()` resolves them through
  `ff.EncoderHelp`, which is why constructing an encoder touches ffmpeg once.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ff
from .ladder import Rung

#: Shared help-text cache. `ffmpeg -h encoder=<codec>` is a subprocess, and the
#: benchmark builds many encoders; without one shared cache each `build()` pays
#: for it again. `ff.ffmpeg()` resolves once per process, so a module-level
#: instance cannot go stale against a different binary.
_HELP = ff.EncoderHelp()


@dataclass(frozen=True)
class Encoder:
    name: str
    codec: str          # -c:v
    tag: str            # fourcc for MP4 (hvc1 vs hev1 matters for Apple/Smart TV)
    hw: bool = False
    extra: tuple[str, ...] = field(default_factory=tuple)

    def args(self) -> list[str]:
        return ["-c:v", self.codec, *self.extra, "-tag:v", self.tag]


def rate_args(rung: Rung) -> list[str]:
    """Rate control for one rung.

    `-b:v` is the average target; `-maxrate:v`/`-bufsize:v` bound the peak and
    the window it may be spent over. Without the latter two a fast-motion scene
    spikes well above the rung's target and the client's ABR logic, which reads
    BANDWIDTH from the manifest, makes the wrong choice.
    """
    return [
        "-b:v", str(rung.bitrate),
        "-maxrate:v", str(rung.maxrate),
        "-bufsize:v", str(rung.bufsize),
    ]


def gop_args(fps: int) -> list[str]:
    """Fixed 2-second GOP with scene-cut keyframes disabled.

    `-sc_threshold 0` matters twice: it keeps segment boundaries predictable
    (a scene-cut keyframe in the middle of a segment breaks CMAF alignment), and
    it makes chunk cut points land where `plan_chunks` expects them.
    """
    g = int(fps * 2)
    return ["-r", str(fps), "-g", str(g), "-keyint_min", str(g), "-sc_threshold", "0"]


def h264_nvenc(preset: str = "p5", help_: ff.EncoderHelp | None = None) -> Encoder:
    """NVENC H.264. p5 is the speed/quality balance; p1 is max throughput.

    spatial/temporal AQ are load-bearing: without them NVENC loses motion detail
    in dark scenes compared with x264, which is exactly the kind of regression a
    benchmark at matched bitrate would otherwise hide.
    """
    h = help_ or _HELP
    codec = "h264_nvenc"
    return Encoder(
        name=codec, codec=codec, tag="avc1", hw=True,
        extra=(
            "-preset", preset, "-rc", "vbr",
            *h.flag(codec, "spatial_aq", "spatial-aq"), "1",
            *h.flag(codec, "temporal_aq", "temporal-aq"), "1",
            "-profile:v", "high", "-level", "5.1",
        ),
    )


def hevc_nvenc(preset: str = "p5", help_: ff.EncoderHelp | None = None) -> Encoder:
    """NVENC H.265 - the 4K rung, where H.264 at 4K bitrate stops paying."""
    h = help_ or _HELP
    codec = "hevc_nvenc"
    return Encoder(
        name=codec, codec=codec, tag="hvc1", hw=True,
        extra=("-preset", preset, "-rc", "vbr",
               *h.flag(codec, "spatial_aq", "spatial-aq"), "1",
               "-profile:v", "main"),
    )


def h264_qsv(preset: str = "veryfast") -> Encoder:
    """Intel Quick Sync. Present so the benchmark is portable to a machine with
    an Intel iGPU; unavailable on this one."""
    return Encoder(name="h264_qsv", codec="h264_qsv", tag="avc1", hw=True,
                   extra=("-preset", preset, "-profile:v", "high"))


def libx264(preset: str = "medium") -> Encoder:
    """CPU reference. This is the baseline every hardware speedup is divided by."""
    return Encoder(name="libx264", codec="libx264", tag="avc1",
                   extra=("-preset", preset, "-profile:v", "high", "-level", "5.1",
                          "-tune", "film"))


#: name -> factory. `cli`/API resolve encoders through this map only.
BACKENDS = {
    "h264_nvenc": h264_nvenc,
    "hevc_nvenc": hevc_nvenc,
    "h264_qsv": h264_qsv,
    "libx264": libx264,
}

#: Each backend has its own preset vocabulary. Preset names are NOT portable:
#: passing the NVENC default "p5" to libx264 is a hard ffmpeg error, so the
#: default has to be chosen per backend rather than globally.
PRESET_DEFAULTS = {
    "h264_nvenc": "p5",
    "hevc_nvenc": "p5",
    "h264_qsv": "veryfast",
    "libx264": "medium",
}

#: The NVENC family uses p1..p7; everything else uses its own names.
NVENC_PRESETS = frozenset(f"p{i}" for i in range(1, 8))


def is_nvenc(name: str) -> bool:
    return name in ("h264_nvenc", "hevc_nvenc")


def validate_preset(name: str, preset: str | None) -> str:
    """Return a preset valid for `name`, filling the backend's default for None.

    Rejects the cross-vocabulary mistake that silently breaks the GPU-absent
    fallback: an NVENC preset handed to libx264 (or vice versa) makes ffmpeg
    exit non-zero after the job has already been accepted and queued.
    """
    if name not in BACKENDS:
        raise ValueError(f"unknown encoder {name!r}; choose from {sorted(BACKENDS)}")
    preset = preset or PRESET_DEFAULTS[name]
    if is_nvenc(name):
        if preset not in NVENC_PRESETS:
            raise ValueError(
                f"{name} preset must be one of {sorted(NVENC_PRESETS)}, got {preset!r}")
    elif preset in NVENC_PRESETS:
        raise ValueError(
            f"{name} does not accept NVENC preset {preset!r}; use an {name} preset")
    return preset


def build(name: str, preset: str | None = None) -> Encoder:
    preset = validate_preset(name, preset)
    factory = BACKENDS[name]
    return factory(preset)
