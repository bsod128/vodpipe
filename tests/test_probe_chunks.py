"""Chunk planning: the correctness core of parallel transcoding."""

from __future__ import annotations

from itertools import pairwise

import pytest

from vodpipe import probe


def test_cut_points_are_idr_frames_only():
    """Where the keyframes give a balanced split, every border is an IDR.

    (Keys span the whole file here. When they stop short of the duration, the
    tail chunk is unavoidably long and the balance guard below takes over - see
    `test_unbalanced_keyframes_fall_back_to_a_uniform_grid`.)
    """
    duration, keys, n = 24.0, [round(i * 3.0, 4) for i in range(9)], 3
    ranges = probe.plan_chunks(duration, keys, n)
    assert len(ranges) == n
    assert ranges[0][0] == 0.0
    assert ranges[-1][1] == duration
    for start, end in ranges:
        assert start in keys or start == 0.0
        assert end in keys or end == duration


def test_ranges_are_contiguous_and_cover_the_source():
    keys = [round(i * 2.0, 4) for i in range(13)]
    ranges = probe.plan_chunks(24.0, keys, 4)
    assert ranges[0][0] == 0.0
    assert ranges[-1][1] == 24.0
    for (_, prev_end), (next_start, _) in pairwise(ranges):
        assert prev_end == next_start, "a gap between chunks would drop frames"


def test_ranges_are_reasonably_even():
    keys = [round(i * 2.0, 4) for i in range(13)]
    sizes = [e - s for s, e in probe.plan_chunks(24.0, keys, 4)]
    assert all(abs(s - 6.0) < 0.01 for s in sizes)


def test_last_edge_is_pinned_to_duration():
    """Keyframes often stop just short of the end; the last chunk must still
    reach the true duration or the tail of the video is silently lost. This
    holds for both paths - the keyframe path pins the end, and so does the
    uniform grid."""
    keys = [0.0, 2.0, 4.0, 6.0]
    for result in (probe.plan_chunks(24.0, keys, 2),
                   probe.plan_chunks(24.0, keys, 3)):
        assert result[-1][1] == 24.0


def test_too_few_keyframes_falls_back_to_a_uniform_grid():
    """A long-GOP master has fewer keyframes than chunks. Falling back to a grid
    is correct because `-ss` before `-i` snaps to the nearest IDR anyway; the
    frame-count check is what proves nothing was lost."""
    ranges = probe.plan_chunks(10.0, [0.0], 5)
    assert len(ranges) == 5
    assert ranges[0][0] == 0.0
    assert ranges[-1][1] == 10.0


def test_unbalanced_keyframes_fall_back_to_a_uniform_grid():
    """Evenly spaced keyframes are not evenly sized chunks.

    This is the 4K master's real shape (GOP 3 s over 24 s): picking four ranges
    from the keyframe list gave 3 s / 6 s / 6 s / 9 s, so the rung's wall time was
    the 9 s chunk while three workers sat idle. Chunking is the multi-GPU tool,
    and a 3x skew is what wastes a card - the uniform grid is the better answer
    even though its borders are not all IDRs (`-ss` snaps to one, and the
    frame-count check is what proves nothing was lost).
    """
    keys = [0.0, 3.0, 6.0, 9.0, 12.0, 15.0, 18.0, 21.0]
    ranges = probe.plan_chunks(24.0, keys, 4)
    sizes = [e - s for s, e in ranges]
    assert len(ranges) == 4
    assert max(sizes) <= probe._MAX_CHUNK_SKEW * min(sizes)
    assert ranges[0][0] == 0.0 and ranges[-1][1] == 24.0
    for (_, prev_end), (next_start, _) in pairwise(ranges):
        assert prev_end == next_start


def test_balanced_keyframes_are_still_used_for_cut_points():
    """The guard must not throw IDR alignment away wholesale: where the keyframes
    do give even chunks, they stay the cut points."""
    keys = [0.0, 3.0, 6.0, 9.0, 12.0, 15.0, 18.0, 21.0]
    ranges = probe.plan_chunks(24.0, keys, 8)
    assert all(start in keys for start, _ in ranges)


def test_keyframes_outside_duration_are_ignored():
    keys = [-1.0, 0.0, 2.0, 4.0, 99.0]
    ranges = probe.plan_chunks(4.0, keys, 2)
    assert all(0.0 <= s < e <= 4.0 for s, e in ranges)


def test_empty_keyframe_list_still_covers_the_file():
    ranges = probe.plan_chunks(12.0, [], 3)
    assert ranges[0][0] == 0.0 and ranges[-1][1] == 12.0


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        probe.plan_chunks(10.0, [0.0], 0)
    with pytest.raises(ValueError):
        probe.plan_chunks(0.0, [0.0], 2)


def test_expected_video_frames_is_one_packet_per_frame():
    """24 s at 30 fps must produce exactly 720 video packets. This number, not
    ffprobe's duration field, is the integrity check after concat."""
    from vodpipe import ts

    assert ts.expected_video_frames(24.0, 30.0) == 720
    assert ts.expected_video_frames(24.0, 29.97) == 719


def test_parse_frame_rate_handles_rationals_and_garbage():
    assert probe.parse_frame_rate("30/1") == 30.0
    assert probe.parse_frame_rate("30000/1001") == 29.97
    assert probe.parse_frame_rate("0/0") == 0.0
    assert probe.parse_frame_rate(None) == 0.0
    assert probe.parse_frame_rate("nonsense") == 0.0


def test_source_from_probe_picks_the_video_stream():
    probe_json = {
        "format": {"duration": "24.0", "bit_rate": "217704831"},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "h264", "width": 3840,
             "height": 2160, "avg_frame_rate": "30/1"},
        ],
    }
    src = probe.source_from_probe("x.mp4", probe_json, keyframes=[0.0, 2.0])
    assert src.width == 3840 and src.is_4k()
    assert src.vcodec == "h264" and src.acodec == "aac"
    assert src.fps == 30.0
    assert "IDR: 2" in src.summary()


def test_source_from_probe_without_video_raises():
    with pytest.raises(ValueError, match="no video stream"):
        probe.source_from_probe("x.mp4", {"streams": [{"codec_type": "audio"}]})
