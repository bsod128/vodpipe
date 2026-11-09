"""MPEG-TS concat, remux-normalise, and integrity verification.

This module exists because of one measurement: on a concatenated TS the
**duration reported by ffprobe is wrong**. Each chunk restarts its PCR/DTS base,
so the container-level duration reflects only part of the stream. Metadata
cannot be trusted there, so integrity is checked by **video packet count**,
which must equal `round(duration * fps)` - one packet per frame for CFR output
with `-sc_threshold 0`.

`normalize()` then remuxes TS -> MP4 (`-c copy`) to heal the seam discontinuities
before CMAF packaging: the muxer writes a single index over one continuous
timeline, and the packager stops seeing "Packet corrupt" at chunk borders.

The packet check has **no tolerance** on purpose - one packet short is a dropped
frame and should fail. It is sound only because `probe.plan_chunks` makes every
chunk span a whole number of frames: IDRs are already frame-aligned, and the
uniform-grid fallback cuts on frame boundaries too. Cut on even seconds instead
(3.4286 s = 102.857 frames) and the pieces overshoot the frame grid; that was a
real bug, pinned by `test_uniform_grid_is_frame_aligned_when_fps_given`.
"""

from __future__ import annotations

import os
import shutil

from . import ff

_PACKET_CHECK = 0.5  # seconds of tolerance on duration comparison


def concat(chunk_paths: list[str], out_path: str) -> str:
    """Binary concat of MPEG-TS chunks.

    Valid because every chunk shares codec, resolution and timebase and starts
    with an IDR (that is what `probe.plan_chunks` guarantees). No demux/mux, so
    it costs one file copy.
    """
    with open(out_path, "wb") as w:
        for p in chunk_paths:
            with open(p, "rb") as r:
                shutil.copyfileobj(r, w, 1 << 20)
    return out_path


def packet_stats(path: str, runner: ff.Runner | None = None) -> dict:
    """Duration plus per-stream packet counts.

    Video and audio are asked for in separate ffprobe calls: one call with
    `-count_packets` covering both stream types returns an empty
    `nb_read_packets` on concatenated TS. Verified behaviour, not a guess.
    """
    run = runner or ff.Runner()
    fmt = ff.probe_json(path, ["format=duration"], runner=run)
    duration = ff.as_float(fmt.get("format", {}).get("duration"))

    def count(kind: str) -> int:
        j = ff.probe_json(path, ["stream=nb_read_packets"], select=kind,
                          count_packets=True, runner=run)
        return sum(ff.as_int(s.get("nb_read_packets")) for s in j.get("streams", []))

    return {"duration": round(duration, 3), "vpkts": count("v"), "apkts": count("a")}


def expected_video_frames(duration: float, fps: float) -> int:
    """One packet per frame for CFR output. This is the number we hold the
    concat accountable to, independent of any container metadata."""
    return round(duration * fps)


def verify_concat(path: str, expect_frames: int, runner: ff.Runner | None = None) -> dict:
    stats = packet_stats(path, runner=runner)
    ok = stats["vpkts"] == expect_frames
    return {"ok": ok, "vpkts": stats["vpkts"], "expected_frames": expect_frames,
            "duration": stats["duration"]}


def normalize_args(ts_path: str, mp4_path: str, tag: str = "") -> list[str]:
    args = [
        ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "warning",
        # +genpts rebuilds missing PTS, which the seam gaps can leave behind.
        "-fflags", "+genpts", "-i", ff.clean_path(ts_path),
        "-c", "copy",
        # AAC inside TS is ADTS; fMP4/MP4 requires AudioSpecificConfig.
        "-bsf:a", "aac_adtstoasc",
    ]
    # The MP4 handler rewrites the stream description from what the elementary
    # stream says, and for H.265 in MPEG-TS that resolves to `hev1` - the
    # encoder's own `-tag:v hvc1` does not survive the container hop. Measured
    # on ffmpeg 7.1: without a tag here, an HEVC rendition is packaged with
    # `codecs="hev1"` in DASH and with **no CODECS= at all** in the HLS master,
    # so a client cannot tell HEVC from H.264 and cannot pick a fallback. The
    # fourcc has to be asserted where the MP4 is written.
    if tag:
        args += ["-tag:v", tag]
    args += ["-movflags", "+faststart", mp4_path]
    return args


def stream_tags(path: str, runner: ff.Runner | None = None) -> tuple[str, bool]:
    """Video fourcc and whether the file has an audio stream.

    One ffprobe call, because packaging needs both: the fourcc decides which
    AdaptationSet a rendition belongs to, and audio presence decides the output
    stream index (`-map N:a:0?` skips a silent rendition, shifting every later
    index). Both are facts about this file, so both are read from it.
    """
    streams = ff.probe_json(path, ["stream=codec_type,codec_tag_string"],
                            runner=runner).get("streams", [])
    video = next((s.get("codec_tag_string", "") for s in streams
                  if s.get("codec_type") == "video"), "")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return video, has_audio


def normalize(ts_path: str, mp4_path: str, expect_dur: float = 0.0,
              runner: ff.Runner | None = None, tag: str = "") -> dict:
    """Remux TS -> MP4 and prove nothing was lost.

    The check is packet-for-packet: `vpkts`/`apkts` before must equal after.
    Duration is only compared for the resulting MP4 (the TS number is the lying
    one), against the source duration. `tag` re-asserts the video fourcc, which
    MPEG-TS cannot carry - see `normalize_args`.
    """
    os.makedirs(os.path.dirname(mp4_path) or ".", exist_ok=True)
    run = runner or ff.Runner()
    run.run(normalize_args(ts_path, mp4_path, tag))
    before = packet_stats(ts_path, runner=run)
    after = packet_stats(mp4_path, runner=run)

    ok = before["vpkts"] == after["vpkts"] and before["apkts"] == after["apkts"]
    if expect_dur:
        ok = ok and abs(after["duration"] - expect_dur) <= _PACKET_CHECK
    fourcc, has_audio = stream_tags(mp4_path, runner=run)
    return {
        "ok": ok,
        "vpkts_ok": before["vpkts"] == after["vpkts"],
        "apkts_ok": before["apkts"] == after["apkts"],
        "mp4": mp4_path,
        # What the manifest will advertise, read back from this file: the fourcc
        # decides DASH AdaptationSet grouping and whether HLS may state CODECS=,
        # and audio presence decides the rendition's output stream index.
        "video_tag": fourcc,
        "has_audio": has_audio,
        "before": before,
        "after": after,
        "expected_duration": expect_dur,
    }
