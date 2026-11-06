"""The generated master must reproduce the shape every committed artifact has.

`bench/*.json` were measured on a 24 s / 30 fps master with 8 IDRs (a 3 s GOP).
If `sample.master_args` changes its GOP, a fresh `vodpipe master` no longer
rebuilds that source and the published numbers stop being reproducible, so the
GOP is pinned here rather than left to drift.
"""
from __future__ import annotations

from vodpipe import sample


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_master_gop_is_three_seconds():
    # The frame rate lives in the lavfi graph (rate=30), not in a -r flag.
    argv = sample.master_args("m.mp4")
    assert int(_flag(argv, "-g")) == 3 * 30 == 90


def test_master_keyint_matches_gop_and_scene_cuts_are_off():
    argv = sample.master_args("m.mp4")
    assert _flag(argv, "-keyint_min") == _flag(argv, "-g")
    assert _flag(argv, "-sc_threshold") == "0"


def test_master_yields_eight_idrs_over_the_default_24s():
    # 24 s at 30 fps = 720 frames; IDR every 90 frames -> frames 0..630 = 8.
    argv = sample.master_args("m.mp4")
    gop = int(_flag(argv, "-g"))
    assert 720 // gop == 8
