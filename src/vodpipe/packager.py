"""CMAF packaging: one segment set that both HLS and DASH can serve.

CMAF means the media is cut once into fragmented-MP4 segments (one init segment
per rendition + short media segments) and only the *manifests* differ. That is
why `package_hls` and `package_dash` here share inputs: a player picks HLS or
DASH, the CDN stores one copy.

Packaging runs with `-c copy` (no re-encode) in a single ffmpeg invocation that
takes every rendition as a separate input. That single invocation is what keeps
segments time-aligned across renditions - the prerequisite for a client switching
rungs mid-playback without a visible glitch.

Two traps this module encodes:

* `-var_stream_map` uses **spaces between variants and commas between the streams
  of one variant**. Semicolons fail with "Could not write header (incorrect codec
  parameters ?): Invalid argument".
* `-c copy` requires direct stream mapping. Any `filter_complex` makes copy
  packaging illegal, so no filtering happens here (do it at encode time).
"""

from __future__ import annotations

import os
import re

from . import ff

# --------------------------------------------------------------------------
# argv builders (pure)
# --------------------------------------------------------------------------


def input_args(rungs: list[tuple[str, str]]) -> list[str]:
    """`-i` per rendition. `rungs` is [(label, media_path)]."""
    return [x for _, p in rungs for x in ("-i", ff.clean_path(p))]


def map_args(n: int) -> list[str]:
    """Direct maps, audio optional (`?`) so a silent master still packages."""
    return [x for i in range(n) for x in ("-map", f"{i}:v:0", "-map", f"{i}:a:0?")]


def has_ts_input(rungs: list[tuple[str, str]]) -> bool:
    return any(p.lower().endswith(".ts") for _, p in rungs)


def bsf_args(rungs: list[tuple[str, str]]) -> list[str]:
    """ADTS -> AudioSpecificConfig for any AAC coming out of MPEG-TS.

    Without it the fMP4 muxer aborts with "Malformed AAC bitstream detected".
    """
    return ["-bsf:a", "aac_adtstoasc"] if has_ts_input(rungs) else []


def safe_label(label: str) -> str:
    """Variant name used in `-var_stream_map`, in file names and in directory names.

    Any run of disallowed characters collapses to a single underscore, so a label
    like `1080p (mobile)` stays readable. Leading and trailing separators and dots
    are stripped, which makes it impossible for a label to contribute a `..` path
    component. Length is capped because the label ends up in file names served by
    the CDN.
    """
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", label).strip("._-")
    return cleaned[:48] or "v"


def var_stream_map(rungs: list[tuple[str, str]]) -> str:
    """One `v:N,a:N,name:label` per rendition, **space separated**.

    This string is the single most common packaging failure I hit: writing the
    separator as `;` (or as a comma between variants) produces a cryptic
    "Could not write header".
    """
    return " ".join(f"v:{i},a:{i},name:{safe_label(label)}"
                    for i, (label, _) in enumerate(rungs))


def hls_args(rungs: list[tuple[str, str]], hls_dir: str, seg_dur: float = 4.0) -> list[str]:
    args = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    args += input_args(rungs) + map_args(len(rungs))
    args += [
        "-c", "copy",
        *bsf_args(rungs),
        "-var_stream_map", var_stream_map(rungs),
        "-f", "hls",
        "-hls_time", str(seg_dur),
        "-hls_segment_type", "fmp4",
        "-hls_playlist_type", "vod",
        # independent_segments lets a client enter the stream at any segment;
        # omit_endlist keeps the VOD playlists cacheable while packaging runs.
        "-hls_flags", "independent_segments+omit_endlist",
        "-hls_segment_filename", ff.clean_path(os.path.join(hls_dir, "stream_%v", "seg_%05d.m4s")),
        "-master_pl_name", "master.m3u8",
        ff.clean_path(os.path.join(hls_dir, "stream_%v.m3u8")),
    ]
    return args


def dash_args(rungs: list[tuple[str, str]], dash_dir: str, seg_dur: float = 4.0) -> list[str]:
    args = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    args += input_args(rungs) + map_args(len(rungs))
    args += [
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-f", "dash",
        "-seg_duration", str(seg_dur),
        # template + timeline: one URL template with a segment timeline instead
        # of a literal list of URLs, which keeps the MPD small and cache friendly.
        "-use_template", "1",
        "-use_timeline", "1",
        "-adaptation_sets", "id=0,streams=v id=1,streams=a",
        ff.clean_path(os.path.join(dash_dir, "manifest.mpd")),
    ]
    return args


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def package_hls(rungs: list[tuple[str, str]], out_dir: str, seg_dur: float = 4.0,
                runner: ff.Runner | None = None) -> dict:
    hls_dir = os.path.join(out_dir, "hls")
    # The HLS muxer creates variant playlists but not these subdirectories; a
    # missing dir surfaces as a header write error on the very first segment.
    os.makedirs(hls_dir, exist_ok=True)
    for label, _ in rungs:
        os.makedirs(os.path.join(hls_dir, f"stream_{safe_label(label)}"), exist_ok=True)

    res = (runner or ff.Runner()).run(hls_args(rungs, hls_dir, seg_dur))
    master = os.path.join(hls_dir, "master.m3u8")
    return {
        "kind": "hls", "master": master, "wall_s": round(res.wall_s, 2),
        "variants": parse_hls_variants(read_text(master)),
        "segments": count_segments(hls_dir),
    }


def package_dash(rungs: list[tuple[str, str]], out_dir: str, seg_dur: float = 4.0,
                 runner: ff.Runner | None = None) -> dict:
    dash_dir = os.path.join(out_dir, "dash")
    os.makedirs(dash_dir, exist_ok=True)
    res = (runner or ff.Runner()).run(dash_args(rungs, dash_dir, seg_dur))
    mpd = os.path.join(dash_dir, "manifest.mpd")
    return {
        "kind": "dash", "manifest": mpd, "wall_s": round(res.wall_s, 2),
        "representations": parse_dash_representations(read_text(mpd)),
        "segments": count_segments(dash_dir),
    }


# --------------------------------------------------------------------------
# manifest parsing (pure) - what actually shipped, read back from the output
# --------------------------------------------------------------------------

_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n#]+)")


def attr(attribute_list: str, name: str) -> str | None:
    """One attribute from an HLS attribute list.

    The `(?:^|,)` anchor is not pedantry: matching a bare `BANDWIDTH=` also hits
    `AVERAGE-BANDWIDTH=`, and ffmpeg emits AVERAGE-BANDWIDTH **before** BANDWIDTH,
    so a loose match reports the wrong number for every rung.
    """
    m = re.search(rf"(?:^|,)\s*{name}=(\"[^\"]*\"|[^,\n]*)", attribute_list)
    if not m:
        return None
    return m.group(1).strip().strip('"')


def parse_hls_variants(master_text: str) -> list[dict]:
    """Video variants: bandwidth, resolution and the codec string.

    Entries with no RESOLUTION (audio-only renditions) are skipped: the ladder a
    client switches between is the set of video renditions, and mixing an audio
    row into it would misreport the ladder.
    """
    out = []
    for attributes, uri in _STREAM_INF_RE.findall(master_text or ""):
        bandwidth = attr(attributes, "BANDWIDTH")
        resolution = attr(attributes, "RESOLUTION")
        if not bandwidth or not resolution:
            continue
        out.append({
            "bandwidth": int(bandwidth),
            "resolution": resolution,
            "uri": uri.strip(),
            "codecs": attr(attributes, "CODECS"),
        })
    return out


_REP_RE = re.compile(r"<Representation\b[^>]*>", re.I)
_SET_RE = re.compile(r"<AdaptationSet\b[^>]*>(.*?)</AdaptationSet>", re.S | re.I)


def parse_dash_representations(mpd_text: str) -> list[dict]:
    """Representations with their media type.

    ffmpeg writes lower-case attributes (`bandwidth=`) while the DASH spec is
    case-insensitive, so matching is case-insensitive and per tag. The media type
    has to come from the parent AdaptationSet's `contenttype`: without it an
    audio Representation is indistinguishable from a video one.
    """
    reps: list[dict] = []
    for group in _SET_RE.finditer(mpd_text or ""):
        open_tag = re.match(r"<AdaptationSet\b[^>]*>", group.group(0), re.I)
        content_type = None
        if open_tag:
            ct = re.search(r'\bcontenttype="([^"]+)"', open_tag.group(0), re.I)
            content_type = ct.group(1) if ct else None
        media = content_type.split("/")[0].lower() if content_type else "unknown"
        for rep in _REP_RE.finditer(group.group(1)):
            tag = rep.group(0)
            reps.append({
                "media": media,
                "bandwidth": _int_attr(tag, "bandwidth"),
                "width": _int_attr(tag, "width"),
                "height": _int_attr(tag, "height"),
            })
    return reps


def _int_attr(tag: str, name: str) -> int | None:
    m = re.search(rf'\b{name}="(\d+)"', tag, re.I)
    return int(m.group(1)) if m else None


def ladder_from_hls(master_text: str) -> list[dict]:
    """The ladder as the client sees it: BANDWIDTH is audio+video combined, which
    is why it reads higher than the encoder's `-b:v` target."""
    variants = [{"resolution": v["resolution"], "total_kbps": v["bandwidth"] // 1000}
                for v in parse_hls_variants(master_text)]
    return sorted(variants, key=lambda d: -d["total_kbps"])


def manifest_stats(out_dir: str) -> dict:
    ladder = ladder_from_hls(read_text(os.path.join(out_dir, "hls", "master.m3u8")))
    return {
        "variants": len(ladder),
        "ladder": ladder,
        "top_kbps": ladder[0]["total_kbps"] if ladder else 0,
        "bottom_kbps": ladder[-1]["total_kbps"] if ladder else 0,
    }


def read_text(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def count_segments(root: str, ext: str = "m4s") -> int:
    n = 0
    for _, _, files in os.walk(root):
        n += sum(1 for f in files if f.endswith("." + ext))
    return n
