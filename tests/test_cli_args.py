"""`demo_arg_errors` - the guards that reject a self-contradictory demo command.

Each of these is a command that would otherwise spend ten minutes encoding and
then report something other than what was asked for.
"""

from __future__ import annotations

import argparse

from vodpipe import cli


def _args(**over) -> argparse.Namespace:
    base = {"encoder": "hevc_nvenc", "preset": "p5", "fallback": None,
            "fallback_preset": None, "watermark": None, "ghost": False,
            "rungs": "2160p", "report": "demo.json", "min_free_gb": 1.0,
            "src": "src_clip/4k_master.mp4", "chunks": 1, "workers": 0,
            "no_vmaf": False}
    return argparse.Namespace(**{**base, **over})


def test_same_codec_family_as_fallback_is_rejected():
    """Two H.264 ladders in one manifest is not a fallback - it is a duplicate."""
    assert "same codec family" in str(cli.demo_arg_errors(
        _args(encoder="h264_nvenc", fallback="libx264")))
    assert "same codec family" in str(cli.demo_arg_errors(
        _args(encoder="h264_nvenc", fallback="h264_qsv")))


def test_a_real_fallback_pair_is_accepted():
    assert cli.demo_arg_errors(_args(fallback="h264_nvenc")) is None
    assert cli.demo_arg_errors(_args(fallback="h264_nvenc", fallback_preset="p4")) is None


def test_fallback_preset_without_fallback_is_rejected():
    """Silently ignored flags are the worst kind: the run looks successful and
    measures the primary's preset."""
    msg = cli.demo_arg_errors(_args(fallback_preset="p7"))
    assert msg is not None and "needs --fallback" in msg


def test_an_unsafe_watermark_id_is_rejected():
    assert "refusing watermark id" in cli.demo_arg_errors(_args(watermark="a,b;c"))


def test_a_plain_single_codec_run_passes():
    assert cli.demo_arg_errors(_args()) is None
