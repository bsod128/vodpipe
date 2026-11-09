"""The dual-codec wiring: two ladders, one manifest, per-rendition facts honoured.

Nothing else in the suite touches `package_dual` / `_package_rungs` / `vmaf_for`,
which is exactly where the two real bugs in this feature lived: HLS needs audio
presence per rendition (a silent rung must not be given an `a:` selector) and
DASH needs the codec family (two families in one AdaptationSet is a manifest no
client can use). Both facts are read back off the encoded file by
`ts.normalize`, so these tests fake ffmpeg and assert on the argv that results.
"""

from __future__ import annotations

import json
import os

from conftest import make_fake_runner

from vodpipe import pipeline as pl
from vodpipe import probe

_NO_STREAMS = (0, '{"streams":[],"format":{}}', "")


def _row(base: str, rung: str, encoder: str, fourcc: str, audio: bool) -> dict:
    """An encode row as `run_ladder` leaves it, with `normalize` already applied.

    Paths are absolute under tmp_path because `ts.normalize` creates its output
    directory: a relative path there would make the suite write into the repo.
    """
    stem = os.path.join(base, f"{rung}_{encoder}")
    return {
        "rung": rung,
        "encoder": encoder,
        "encoder_tag": fourcc,
        "mode": "whole",
        "out": stem + ".ts",
        "wall_s": 1.0,
        "speed_x": 2.0,
        "normalize": {"ok": True, "mp4": stem + ".mp4", "video_tag": fourcc,
                      "has_audio": audio},
    }


def _payloads(rows: list[dict]) -> dict[str, tuple[int, str, str]]:
    """What each rendition file *really* contains, keyed by its file name.

    The fourcc and audio presence are answered per file, because the point of
    these tests is that packaging reads them back from the file rather than
    taking them from the encoder config. Specific names come first: the fake
    runner matches argv substrings in insertion order, and the generic `.mp4`
    / `.ts` keys below only have to satisfy the packet-count probes.
    """
    payloads: dict[str, tuple[int, str, str]] = {}
    for r in rows:
        norm = r["normalize"]
        streams = [{"codec_type": "video", "codec_tag_string": norm["video_tag"]}]
        if norm["has_audio"]:
            streams.append({"codec_type": "audio", "codec_tag_string": "mp4a"})
        payloads[os.path.basename(norm["mp4"])] = (
            0, json.dumps({"streams": streams, "format": {"duration": "24.000000"}}), "")
    payloads[".ts"] = _NO_STREAMS
    payloads[".mp4"] = _NO_STREAMS
    return payloads


def _pipeline(base: str, rows: list[dict] | None = None):
    """A Pipeline whose probe is a stub, so packaging is testable without ffmpeg.

    Packaging decisions depend only on what `normalize` read back from each
    rendition, never on the master, so the probe is replaced rather than emulated.
    """
    runner, calls = make_fake_runner(_payloads(rows or []))
    pipe = pl.Pipeline.__new__(pl.Pipeline)
    pipe.runner = runner
    pipe.src = probe.Source(path="src.mp4", duration=24.0, width=3840, height=2160,
                            fps=30.0, vcodec="h264", acodec="aac", bitrate=217704,
                            keyframes=[0.0, 8.0, 16.0])
    pipe.work = os.path.join(base, "work")
    pipe.wm_spec = {}
    pipe.wm = None
    os.makedirs(pipe.work, exist_ok=True)
    return pipe, calls


def _package(base: str, rows: list[dict]):
    pipe, calls = _pipeline(base, rows)
    pkg = pipe.package_dual(rows[:1], rows[1:], os.path.join(base, "out"))
    return pkg, calls


def _flag(calls: list[list[str]], flag: str) -> str:
    for call in calls:
        if flag in call:
            return call[call.index(flag) + 1]
    raise AssertionError(f"{flag} never reached ffmpeg; calls={[c[:3] for c in calls]}")


def test_normalize_reasserts_the_tag_and_reports_what_packaging_needs(tmp_path):
    """The two decisions below come from `normalize`, not from the encoder config
    - which is the point of reading the fourcc back off the file."""
    base = str(tmp_path)
    rows = [_row(base, "1080p", "hevc_nvenc", "hvc1", True)]
    pipe, calls = _pipeline(base, rows)
    encoded = pipe.normalize(rows)
    remux = calls[0]
    assert remux[remux.index("-tag:v") + 1] == "hvc1"
    assert encoded[0]["normalize"]["video_tag"] == "hvc1"


def test_package_dual_labels_renditions_by_encoder(tmp_path):
    """Both ladders deliver 1080p, so the variant name must carry the encoder."""
    base = str(tmp_path)
    rows = [_row(base, "1080p", "hevc_nvenc", "hvc1", True),
            _row(base, "1080p", "h264_nvenc", "avc1", True)]
    _, calls = _package(base, rows)
    names = [part.split("name:")[1] for part in _flag(calls, "-var_stream_map").split(" ")]
    assert names == ["1080p_hevc_nvenc", "1080p_h264_nvenc"]


def test_package_dual_splits_codecs_into_two_video_adaptation_sets(tmp_path):
    base = str(tmp_path)
    rows = [_row(base, "1080p", "hevc_nvenc", "hvc1", True),
            _row(base, "1080p", "h264_nvenc", "avc1", True)]
    _, calls = _package(base, rows)
    assert _flag(calls, "-adaptation_sets") == "id=0,streams=0 id=1,streams=2 id=2,streams=1,3"


def test_package_dual_gives_a_silent_rung_no_audio_selector_in_hls(tmp_path):
    """The bug: `v:1,a:1` asks for the 2nd audio stream, and with one silent rung
    there is no 2nd audio stream - ffmpeg aborts with "Unable to map stream at
    a:1" then "Could not write header". HLS audio ordinals count audio streams
    only, unlike the output stream indices DASH addresses renditions by."""
    base = str(tmp_path)
    rows = [_row(base, "2160p", "hevc_nvenc", "hvc1", True),
            _row(base, "1080p", "hevc_nvenc", "hvc1", False),
            _row(base, "720p", "hevc_nvenc", "hvc1", True)]
    _, calls = _package(base, rows)
    assert _flag(calls, "-var_stream_map") == (
        "v:0,a:0,name:2160p_hevc_nvenc "
        "v:1,name:1080p_hevc_nvenc "
        "v:2,a:1,name:720p_hevc_nvenc"
    )


def test_package_dual_shifts_dash_indices_around_a_silent_rung(tmp_path):
    """Same fact, DASH side: a silent rendition contributes one output stream.

    The silent rung has to sit in the *first* family for this to bite: the
    second family's renditions are the ones whose indices shift, and addressing
    them by the naive `2*i` would point at the audio stream next door - a
    manifest that parses and plays the wrong rendition.
    """
    base = str(tmp_path)
    rows = [_row(base, "2160p", "hevc_nvenc", "hvc1", True),
            _row(base, "1080p", "hevc_nvenc", "hvc1", False),
            _row(base, "720p", "h264_nvenc", "avc1", True)]
    _, calls = _package(base, rows)
    # 2160p -> v0/a1 | 1080p -> v2 (silent) | 720p -> v3/a4
    assert _flag(calls, "-adaptation_sets") == "id=0,streams=0,2 id=1,streams=3 id=2,streams=1,4"


def test_one_codec_family_keeps_the_committed_adaptation_set_shape(tmp_path):
    """Two rungs of one family are not a mixed ladder: the value must stay the
    `streams=v`/`streams=a` shorthand every committed artifact has."""
    base = str(tmp_path)
    rows = [_row(base, "2160p", "hevc_nvenc", "hvc1", True),
            _row(base, "720p", "hevc_nvenc", "hvc1", True)]
    _, calls = _package(base, rows)
    assert _flag(calls, "-adaptation_sets") == "id=0,streams=v id=1,streams=a"


def test_package_dual_rejects_colliding_labels(tmp_path):
    """Two renditions sharing a name overwrite each other's playlists, and the
    manifest still parses, so nothing downstream would notice."""
    base = str(tmp_path)
    rows = [_row(base, "1080p", "hevc_nvenc", "hvc1", True),
            _row(base, "1080p", "hevc_nvenc", "hvc1", True)]
    try:
        _package(base, rows)
    except ValueError as exc:
        assert "duplicate rendition label" in str(exc)
    else:
        raise AssertionError("duplicate labels were accepted")


def test_vmaf_for_keying_survives_two_codecs_delivering_one_resolution(tmp_path):
    """Keyed by rung alone, the fallback's number silently overwrites the primary's."""
    base = str(tmp_path)
    pipe, _ = _pipeline(base)
    rows = [_row(base, "1080p", "hevc_nvenc", "hvc1", True),
            _row(base, "1080p", "h264_nvenc", "avc1", True)]
    out = pipe.vmaf_for(rows, key=lambda r: f"{r['rung']}_{r['encoder']}")
    assert sorted(out) == ["1080p_h264_nvenc", "1080p_hevc_nvenc"]
