"""Parsing of ffmpeg's own text output.

These parsers are the interface between our code and a tool whose output format
changes between builds, so each one is pinned by a fixture taken from real
output, including the formats that once silently matched nothing.
"""

from __future__ import annotations

from vodpipe import ff

ENCODER_DUMP = """
Encoders:
 V = Video encoder. A = Audio encoder. S = Encoder supported by HW acceleration.
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
 V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
 V....D h264_qsv             H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (Intel
 A....D aac                  AAC (Advanced Audio Coding)
"""

FILTER_DUMP = """
Filters:
 T.C anchor                 Insert anchors anywhere in filtergraph (graph)
 ... drawtext               Draw text above video (V->V)
 T.C scale                  Scale the input video size and/or convert the image format (V->V)
 ... libvmaf                Compute vmaf score (VV->V)
"""

HELP_TEXT = """
h264_nvenc encoders:
Threading options:
    -g             <int>        GOP size (from 0 to INT_MAX) (default 0)
    -spatial_aq    <boolean>    use spatial adaptive quantization (default false)
    -rc            <int>        ratecontrol method
"""


def test_encoder_names_including_hardware():
    names = ff.parse_encoder_names(ENCODER_DUMP)
    assert {"h264_nvenc", "hevc_nvenc", "libx264", "h264_qsv", "aac"} <= names


def test_encoder_names_ignore_help_legend():
    """The legend line starts with ` V = Video encoder.` - a loose pattern used
    to harvest `=` and single letters from it."""
    names = ff.parse_encoder_names(ENCODER_DUMP)
    assert all(len(n) > 1 and not n.startswith("=") for n in names)


def test_filter_names_use_the_short_flag_column():
    """ffmpeg 7.x prints a 3-char filter flag column (` T.C drawtext`), so a
    fixed 6-char pattern finds zero filters and every capability check fails."""
    names = ff.parse_filter_names(FILTER_DUMP)
    assert {"drawtext", "scale", "libvmaf", "anchor"} <= names


def test_parsers_tolerate_empty_output():
    assert ff.parse_encoder_names("") == set()
    assert ff.parse_filter_names("") == set()
    assert ff.parse_help_options("") == set()


def test_help_options_and_pick_option():
    assert {"g", "spatial_aq", "rc"} <= ff.parse_help_options(HELP_TEXT)
    assert ff.pick_option(HELP_TEXT, "spatial-aq", "spatial_aq") == "-spatial_aq"
    assert ff.pick_option(HELP_TEXT, "nonexistent") is None


def test_pick_option_prefers_the_first_supported_spelling():
    """Builds disagree on underscore vs hyphen. Picking the wrong one is not a
    quiet degradation: ffmpeg exits with 'Unrecognized option'."""
    hyphen = HELP_TEXT.replace("spatial_aq", "spatial-aq")
    assert ff.pick_option(hyphen, "spatial-aq", "spatial_aq") == "-spatial-aq"


def test_last_speed_takes_the_final_progress_line():
    stderr = (r"frame=  100 fps=120 speed=3.97x\r\n"
              r"frame=  600 fps=180 speed=6.10x\r\n")
    assert ff.last_speed(stderr) == 6.10


def test_last_speed_is_zero_when_ffmpeg_printed_no_stats():
    assert ff.last_speed("") == 0.0
    assert ff.last_speed("Unrecognized option") == 0.0


def test_clean_path_uses_forward_slashes():
    """ffmpeg's arg and filtergraph parsers treat backslash as an escape char."""
    assert ff.clean_path(r"D:\media\out\master.m3u8") == "D:/media/out/master.m3u8"


def test_scrub_hides_machine_specific_roots():
    """Reported error text is committed and shown to clients, so the host's
    directory layout must not survive it."""
    text = "source not found: D:\\media\\src_clip\\master.mp4"
    assert ff.scrub(text) == "source not found: <local-path>"
    assert ff.scrub("see /home/user/media/out.m3u8") == "see <local-path>"
    assert ff.scrub("see ~/media/out.m3u8") == "see <local-path>"
    assert "src_clip" not in ff.scrub(text)
    assert ff.scrub("relative/out.m3u8") == "relative/out.m3u8"
