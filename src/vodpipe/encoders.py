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
    h = help_ or ff.EncoderHelp()
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
    h = help_ or ff.EncoderHelp()
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


def build(name: str, preset: str | None = None) -> Encoder:
    try:
        factory = BACKENDS[name]
    except KeyError:
        raise ValueError(f"unknown encoder {name!r}; choose from {sorted(BACKENDS)}") from None
    import inspect

    if "preset" in inspect.signature(factory).parameters:
        return factory(preset) if preset else factory()
    return factory()
