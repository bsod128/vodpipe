"""Source metadata and the only safe chunk boundaries: IDR frames.

`plan_chunks()` is pure: it turns a keyframe list into N ranges. That is the
logic that decides whether a parallel transcode is correct, and it is tested
without ffmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ff

_PROBE_ENTRIES = (
    "format=duration,bit_rate:stream=index,codec_type,codec_name,width,height,avg_frame_rate"
)


@dataclass(frozen=True)
class Source:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str
    acodec: str | None
    bitrate: int
    keyframes: list[float] = field(default_factory=list)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def is_4k(self) -> bool:
        return self.height >= 2160 or self.pixels >= 8_000_000

    def summary(self) -> str:
        audio = f"+{self.acodec}" if self.acodec else ""
        return (f"{self.resolution}@{self.fps} {self.vcodec}{audio}, "
                f"{self.duration:.2f} s, {self.bitrate // 1000} kbps, "
                f"IDR: {len(self.keyframes)}")

    def as_dict(self) -> dict:
        return {
            "path": self.path, "duration": self.duration, "resolution": self.resolution,
            "fps": self.fps, "vcodec": self.vcodec, "acodec": self.acodec,
            "bitrate": self.bitrate, "keyframes": len(self.keyframes),
        }


def parse_frame_rate(raw: str | None) -> float:
    """`avg_frame_rate` is a rational string like "30000/1001"; "0/0" means unset."""
    num, _, den = (raw or "0/1").partition("/")
    try:
        n, d = float(num), float(den or 0)
    except ValueError:
        return 0.0
    return round(n / d, 3) if d else 0.0


def source_from_probe(path: str, probe: dict, keyframes: list[float] | None = None) -> Source:
    """Build Source from an ffprobe dict (pure half of `probe_source`)."""
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"no video stream in {path}")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = probe.get("format", {})
    return Source(
        path=path,
        duration=ff.as_float(fmt.get("duration")),
        width=ff.as_int(video.get("width")),
        height=ff.as_int(video.get("height")),
        fps=parse_frame_rate(video.get("avg_frame_rate")),
        vcodec=video.get("codec_name") or "?",
        acodec=audio.get("codec_name") if audio else None,
        bitrate=ff.as_int(fmt.get("bit_rate")),
        keyframes=sorted(keyframes or []),
    )


def probe_source(path: str, with_keyframes: bool = True,
                 runner: ff.Runner | None = None) -> Source:
    probe = ff.probe_json(path, [_PROBE_ENTRIES], runner=runner)
    keys = keyframes(path, runner=runner) if with_keyframes else []
    return source_from_probe(path, probe, keys)


def keyframes(path: str, runner: ff.Runner | None = None) -> list[float]:
    """pts_time of every IDR frame - the only cut points that keep a chunk decodable."""
    probe = ff.probe_json(path, ["packet=pts_time,flags"], select="v:0", runner=runner)
    out = []
    for pkt in probe.get("packets", []):
        if "K" not in (pkt.get("flags") or ""):
            continue
        try:
            out.append(round(float(pkt["pts_time"]), 4))
        except (TypeError, KeyError, ValueError):
            continue
    return sorted(out)


def plan_chunks(duration: float, keyframes: list[float], n: int) -> list[tuple[float, float]]:
    """Split [0, duration] into n ranges whose borders are IDR frames.

    Cutting mid-GOP is the classic way to get corrupt frames at chunk seams: a
    chunk that starts on a P-frame cannot be decoded on its own. So borders are
    chosen from the keyframe list. When the source has too few keyframes (a
    long-GOP master), we fall back to a GOP-aligned uniform grid and accept that
    chunks start at non-keyframes: ffmpeg's `-ss` before `-i` still snaps to the
    nearest IDR, which is why the resulting total stays frame-accurate - the
    frame count check in `vodpipe.concat` is what proves it.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if duration <= 0:
        raise ValueError("duration must be positive")

    usable = [k for k in keyframes if 0.0 <= k <= duration]
    if len(usable) < n + 1:
        step = duration / n
        usable = [round(i * step, 4) for i in range(n + 1)]

    # Pick n+1 borders as evenly as possible over the available keyframes.
    last = len(usable) - 1
    idx = [round(i * last / n) for i in range(n + 1)]
    borders = [usable[i] for i in idx]
    borders[0] = 0.0
    borders[-1] = round(duration, 4)

    ranges = [(borders[i], borders[i + 1]) for i in range(n)]
    return [(s, e) for (s, e) in ranges if e > s]
