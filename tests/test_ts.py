"""TS -> MP4 normalisation argv.

The one that is invisible until a client rejects the stream: the video fourcc
cannot travel through MPEG-TS, so the remux decides it, and for H.265 it decides
`hev1` no matter what the encoder was told.
"""

from __future__ import annotations

import pytest
from conftest import ffmpeg_scratch_writable, make_fake_runner, requires_ffmpeg

from vodpipe import ff, ts

_NO_STREAMS = (0, '{"streams":[],"format":{}}', "")
_EMPTY = {"a.ts": _NO_STREAMS, "a.mp4": _NO_STREAMS}


def _normalize(tmp_path, tag: str = "") -> list[list[str]]:
    runner, calls = make_fake_runner(_EMPTY)
    ts.normalize(str(tmp_path / "a.ts"), str(tmp_path / "out" / "a.mp4"),
                 runner=runner, tag=tag)
    return calls


def test_normalize_args_copies_streams_and_heals_aac():
    args = ts.normalize_args("in.ts", "out.mp4")
    assert args[args.index("-c") + 1] == "copy"
    assert "aac_adtstoasc" in args
    assert "+faststart" in args
    assert args[-1] == "out.mp4"


def test_normalize_args_puts_the_fourcc_as_an_output_option():
    """`-tag:v` is an output option: it must land after the input and before the
    output path, or it does not apply to the MP4 being written."""
    args = ts.normalize_args("in.ts", "out.mp4", tag="hvc1")
    assert args[args.index("-tag:v") + 1] == "hvc1"
    assert args.index("-i") < args.index("-tag:v") < len(args) - 1
    assert args[-1] == "out.mp4"


def test_normalize_args_without_a_tag_writes_no_tag_flag():
    """An empty tag must not reach ffmpeg as `-tag:v ""`, which is an error."""
    assert "-tag:v" not in ts.normalize_args("in.ts", "out.mp4", tag="")


def test_normalize_reasserts_the_fourcc_because_ts_cannot_carry_it(tmp_path):
    """The tag has to be asserted where the MP4 is written - the real contract is
    pinned by `test_the_fourcc_actually_reaches_the_output` below."""
    remux = _normalize(tmp_path, tag="hvc1")[0]
    assert remux[remux.index("-tag:v") + 1] == "hvc1"


def test_normalize_without_a_tag_leaves_the_fourcc_to_the_muxer(tmp_path):
    assert "-tag:v" not in _normalize(tmp_path)[0]


@requires_ffmpeg
def test_the_fourcc_actually_reaches_the_output(tmp_path):
    """The measurement this whole feature rests on, run against real ffmpeg.

    H.265 encoded into MPEG-TS with `-tag:v hvc1` comes out of the TS->MP4 remux
    as `hev1`, because the MP4 handler rebuilds the stream description from the
    elementary stream. Re-asserting the tag at the remux yields `hvc1`. Not a
    cosmetic difference: HLS writes no `CODECS=` for a rendition whose video is
    `hev1`, so a client cannot tell it from an H.264 rendition and never selects
    the fallback.
    """
    if not ffmpeg_scratch_writable(str(tmp_path)):
        pytest.skip("scratch dir not writable by ffmpeg (rerun with --basetemp on a "
                    "writable volume)")
    h265 = str(tmp_path / "h.ts")
    ff.Runner().run([ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                     "-f", "lavfi", "-i", "testsrc=size=128x96:rate=25:duration=1",
                     "-c:v", "libx265", "-tag:v", "hvc1", "-x265-params",
                     "log-level=none", h265])

    # Without the tag at the remux, the muxer decides - and decides `hev1`.
    plain = str(tmp_path / "plain.mp4")
    ts.normalize(h265, plain, runner=ff.Runner())
    assert ts.stream_tags(plain)[0] == "hev1"

    # With it, the output says what the ladder needs it to say.
    tagged = str(tmp_path / "tagged.mp4")
    res = ts.normalize(h265, tagged, runner=ff.Runner(), tag="hvc1")
    assert res["video_tag"] == "hvc1", "the tag must survive to the finished file"
    assert res["has_audio"] is False


def test_normalize_reads_back_the_facts_packaging_is_built_from(tmp_path):
    runner, _ = make_fake_runner({
        "a.ts": _NO_STREAMS,
        "a.mp4": (0, '{"streams":[{"codec_type":"video","codec_tag_string":"hev1"},'
                     '{"codec_type":"audio","codec_tag_string":"mp4a"}],"format":{}}', ""),
    })
    mp4 = str(tmp_path / "out" / "a.mp4")
    res = ts.normalize(str(tmp_path / "a.ts"), mp4, runner=runner)
    assert res["mp4"] == mp4
    assert (res["video_tag"], res["has_audio"]) == ("hev1", True)


def test_normalize_reports_a_silent_rung(tmp_path):
    runner, _ = make_fake_runner({
        "a.ts": _NO_STREAMS,
        "a.mp4": (0, '{"streams":[{"codec_type":"video","codec_tag_string":"hvc1"}],'
                     '"format":{}}', ""),
    })
    res = ts.normalize(str(tmp_path / "a.ts"), str(tmp_path / "a.mp4"), runner=runner)
    assert res["has_audio"] is False
    assert res["video_tag"] == "hvc1"


def test_stream_tags_reads_fourcc_and_audio_presence_in_one_probe(tmp_path):
    """One ffprobe call, because packaging needs both facts about the same file.

    `hev1` and `hvc1` are deliberately both asserted: they are the same codec in
    different container orders, and a caller that folded them together would
    break DASH grouping while passing a test written only for `hvc1`.
    """
    import json

    for fourcc in ("hev1", "hvc1", "avc1"):
        payload = json.dumps({"streams": [
            {"codec_type": "video", "codec_tag_string": fourcc},
            {"codec_type": "audio", "codec_tag_string": "mp4a"},
        ]})
        runner, calls = make_fake_runner({"x.mp4": (0, payload, "")})
        assert ts.stream_tags(str(tmp_path / "x.mp4"), runner=runner) == (fourcc, True)
        assert len(calls) == 1, "both facts must come from one probe"


def test_stream_tags_is_empty_and_false_for_a_file_with_no_streams():
    """A missing fourcc or audio stream is a fact to report, not an error: the
    packager degrades to its own AdaptationSet instead of taking the run down."""
    runner, _ = make_fake_runner({})
    assert ts.stream_tags("x.mp4", runner=runner) == ("", False)
