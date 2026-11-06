"""The argv the pipeline actually composes.

These are the tests that were missing when two real bugs shipped:

* a global NVENC preset ("p5") reaching libx264, which makes ffmpeg exit
  non-zero after a job has already been queued;
* `-r` forced to the rung ceiling while the frame-count check and VMAF used the
  source frame rate, which mis-measures any master that is not exactly 30 fps.

Both are pure argv decisions, so they are checked here without ffmpeg.
"""

from __future__ import annotations

import pytest
from conftest import requires_ffmpeg

from vodpipe import encode, encoders
from vodpipe.ladder import ladder
from vodpipe.probe import Source


def make_source(fps: float, duration: float = 24.0) -> Source:
    return Source(path="master.mp4", duration=duration, width=3840, height=2160,
                  fps=fps, vcodec="h264", acodec="aac", bitrate=200_000_000,
                  keyframes=[])


def rung(label: str):
    return next(r for r in ladder() if r.label == label)


def flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


# --------------------------------------------------------------------------
# presets are per backend, not global
# --------------------------------------------------------------------------


def test_build_fills_each_backends_own_default_preset():
    assert flag_value(encoders.build("h264_nvenc").args(), "-preset") == "p5"
    assert flag_value(encoders.build("libx264").args(), "-preset") == "medium"
    assert flag_value(encoders.build("h264_qsv").args(), "-preset") == "veryfast"


def test_nvenc_preset_never_reaches_libx264():
    """The GPU-absent fallback used to inherit the NVENC default and die."""
    with pytest.raises(ValueError, match="libx264"):
        encoders.build("libx264", "p5")


def test_nvenc_rejects_a_foreign_preset():
    with pytest.raises(ValueError, match="h264_nvenc"):
        encoders.build("h264_nvenc", "medium")


@pytest.mark.parametrize("preset", ["p1", "p4", "p7"])
def test_nvenc_accepts_its_own_preset_range(preset: str):
    assert flag_value(encoders.build("h264_nvenc", preset).args(), "-preset") == preset


def test_validate_preset_is_the_single_owner_of_the_default():
    assert encoders.validate_preset("libx264", None) == "medium"
    assert encoders.validate_preset("hevc_nvenc", None) == "p5"
    with pytest.raises(ValueError, match="unknown encoder"):
        encoders.validate_preset("nope", None)


# --------------------------------------------------------------------------
# frame rate: one basis for encode, the frame-count check, and VMAF
# --------------------------------------------------------------------------


@pytest.mark.parametrize("src_fps,expected", [(30.0, 30), (24.0, 24), (25.0, 25),
                                              (60.0, 30), (0.0, 30)])
def test_out_fps_caps_source_rate_at_the_rung_ceiling(src_fps: float, expected: int):
    assert encode.out_fps(make_source(src_fps), rung("1080p")) == expected


def test_encode_and_verification_agree_on_frame_count():
    """The regression: -r 30 on a 24 fps master while the check expected 24 fps."""
    from vodpipe import ts

    src = make_source(24.0, duration=24.0)
    r = rung("720p")
    argv = encode.whole_args(src, r, encoders.build("libx264"), "out.ts")
    commanded = int(flag_value(argv, "-r"))
    expected = ts.expected_video_frames(src.duration, commanded)
    assert expected == 576, "24 s at the commanded rate, not at the rung ceiling"


def test_gop_stays_two_seconds_at_the_commanded_rate():
    argv = encode.whole_args(make_source(24.0), rung("1080p"),
                             encoders.build("libx264"), "out.ts")
    gop = int(flag_value(argv, "-g"))
    assert gop == 2 * int(flag_value(argv, "-r")) == 48


def test_chunked_encode_uses_the_same_rate():
    argv = encode.chunk_args(make_source(24.0), rung("720p"), encoders.build("libx264"),
                             0.0, 6.0, "c0.ts")
    assert flag_value(argv, "-r") == "24"
    assert flag_value(argv, "-ss") == "0.0"


def test_chunk_pins_video_to_an_exact_frame_count():
    # A float `-t` trim keeps a frame only if it lands strictly inside a
    # half-open time interval, so a sub-frame cut silently drops one. The exact
    # `-frames:v` is what the concat check is accountable to.
    src, r, enc = make_source(24.0, 24.0), rung("1080p"), encoders.build("libx264")
    argv = encode.chunk_args(src, r, enc, 3.4286, 6.8571, "c1.ts")
    assert flag_value(argv, "-frames:v") == str(encode.chunk_frames(src, r, 3.4286, 6.8571))
    assert encode.chunk_frames(src, r, 3.4286, 6.8571) == round(6.8571 * 24) - round(3.4286 * 24)


def test_chunk_frame_counts_telescope_to_the_whole_file():
    # Whatever the borders, summing the per-chunk frame counts must equal the
    # expectation verify() compares against - the boundaries have to cancel.
    from vodpipe import ts
    src, r = make_source(30.0, 24.0), rung("720p")
    n = 7
    edges = [round(i * 24 * 30 / n) / 30 for i in range(n + 1)]
    total = sum(encode.chunk_frames(src, r, edges[i], edges[i + 1]) for i in range(n))
    assert total == ts.expected_video_frames(24.0, encode.out_fps(src, r))


def test_whole_and_chunked_share_rate_control_and_gop():
    src, r, enc = make_source(24.0), rung("1080p"), encoders.build("h264_nvenc")
    whole = encode.whole_args(src, r, enc, "a.ts")
    chunk = encode.chunk_args(src, r, enc, 0.0, 6.0, "b.ts")
    for flag in ("-r", "-g", "-keyint_min", "-sc_threshold", "-b:v", "-maxrate:v",
                 "-bufsize:v", "-preset"):
        assert flag_value(whole, flag) == flag_value(chunk, flag), flag


def test_rate_control_bounds_the_peak_not_just_the_average():
    argv = encode.whole_args(make_source(30.0), rung("1080p"),
                             encoders.build("h264_nvenc"), "out.ts")
    assert int(flag_value(argv, "-maxrate:v")) >= int(flag_value(argv, "-b:v"))
    assert int(flag_value(argv, "-bufsize:v")) >= int(flag_value(argv, "-maxrate:v"))


def test_watermark_filters_are_appended_after_the_scale():
    argv = encode.whole_args(make_source(30.0), rung("1080p"),
                             encoders.build("libx264"), "out.ts", wm="drawtext=x=1")
    vf = flag_value(argv, "-vf")
    assert vf.index("drawtext") > vf.index("scale="), "watermark must ride the scale pass"


# --------------------------------------------------------------------------
# real binary: the escaping that protects the graph
#
# A unit test cannot prove that ffmpeg's *parser* agrees with our escaping, and
# that is exactly the assumption that was wrong before. This one runs the real
# ffmpeg with an injection payload; if the value escapes its quoted region the
# parser reports the smuggled filter instead of rendering it as text.
# --------------------------------------------------------------------------

INJECTION = "x',nosuchfilter_vodpipe_probe=1"


@requires_ffmpeg
def test_injected_filtergraph_is_not_reachable_through_a_watermark():
    from vodpipe import ff, watermark

    if "drawtext" not in ff.filter_names():
        pytest.skip("this ffmpeg build has no drawtext")

    argv = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.04",
            "-vf", watermark.static_text(INJECTION),
            "-frames:v", "1", "-f", "null", "-"]
    res = ff.Runner(timeout_s=60).run(argv, check=False)
    assert "No such filter" not in res.stderr, (
        "watermark text escaped its quoted region and reached the filter graph")
