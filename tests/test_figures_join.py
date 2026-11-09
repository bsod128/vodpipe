"""`ladder_rows` - the join between the manifest and the encode rows.

This is where a dual-codec report broke quietly: the charts looked fine, every
VMAF bar read zero, and both codecs plotted the same bitrate.
"""

from __future__ import annotations

from vodpipe import cli


def _dual_report() -> dict:
    """A dual-codec report in the shape `cmd_demo` writes.

    Built here rather than read from `bench/` on purpose: the contract is how the
    three blocks of a report key onto each other, and that has to hold whatever
    one artifact happens to contain.
    """
    # Bitrates differ per codec, as they do in a real run: two codecs filling the
    # same target leave slightly different numbers in BANDWIDTH.
    rungs = {"hevc_nvenc": [("3840x2160", "2160p", 30_990, 94.76),
                            ("1920x1080", "1080p", 7_043, 82.20)],
             "h264_nvenc": [("3840x2160", "2160p", 31_656, 94.90),
                            ("1920x1080", "1080p", 7_485, 82.16)]}
    variants: list[dict] = []
    vmaf: dict[str, dict] = {}
    encode: list[dict] = []
    for encoder, codec_str in (("hevc_nvenc", "hvc1.1.4.L150.B01"),
                               ("h264_nvenc", "avc1.640033")):
        for res, label, kbps, mean in rungs[encoder]:
            variants.append({"bandwidth": kbps * 1000, "resolution": res,
                             "codecs": f"{codec_str},mp4a.40.2",
                             "uri": f"stream_{label}_{encoder}.m3u8"})
            vmaf[f"{label}_{encoder}"] = {"vmaf_mean": mean, "vmaf_min": mean - 1.5}
            encode.append({"rung": label, "encoder": encoder, "rung_res": res,
                           "wall_s": 30.0, "cancelled": False})
    return {"encode": encode, "vmaf": vmaf,
            "packaging": {"hls": {"variants": variants}, "dash": {"representations": []}}}


def test_dual_report_keys_every_row_by_rendition():
    rows = cli.ladder_rows(_dual_report())
    assert [r["rung"] for r in rows] == ["2160p_hevc_nvenc", "1080p_hevc_nvenc",
                                         "2160p_h264_nvenc", "1080p_h264_nvenc"]


def test_dual_report_has_no_missing_numbers():
    """Zero in either column means the lookup missed - which is exactly what
    keying bitrate by resolution and VMAF by rung used to do."""
    rows = cli.ladder_rows(_dual_report())
    assert all(r["total_kbps"] and r["vmaf_mean"] for r in rows), rows
    # Both codecs delivered the same resolutions, so four distinct bitrates proves
    # they were not collapsed onto one manifest row each.
    assert len({r["total_kbps"] for r in rows}) == 4
    assert len({r["vmaf_mean"] for r in rows}) == 4


def test_single_codec_report_is_unchanged():
    """The shape every committed artifact has: the label *is* the rung."""
    rep = {
        "encode": [{"rung": "1080p", "encoder": "h264_nvenc", "rung_res": "1920x1080",
                    "wall_s": 9.5, "cancelled": False}],
        "vmaf": {"1080p": {"vmaf_mean": 82.16, "vmaf_min": 80.82}},
        "packaging": {"hls": {"variants": [{"bandwidth": 7485362, "resolution": "1920x1080",
                                            "codecs": "avc1.640033,mp4a.40.2",
                                            "uri": "stream_1080p.m3u8"}]},
                      "dash": {"representations": []}},
    }
    rows = cli.ladder_rows(rep)
    assert [(r["rung"], r["total_kbps"], r["vmaf_mean"]) for r in rows] == [
        ("1080p", 7485, 82.16)]


def test_cancelled_rungs_are_left_out():
    rep = _dual_report()
    rep["encode"][0]["cancelled"] = True
    assert [r["rung"] for r in cli.ladder_rows(rep)] == ["1080p_hevc_nvenc",
                                                         "2160p_h264_nvenc",
                                                         "1080p_h264_nvenc"]


def test_a_report_with_nothing_finished_produces_no_charts(tmp_path):
    rep = _dual_report()
    for e in rep["encode"]:
        e["cancelled"] = True
    assert cli.ladder_rows(rep) == []
    assert cli.ladder_figures(rep, str(tmp_path)) == []
