"""Report serialization: what gets committed must not carry local paths."""

from __future__ import annotations

import os

from vodpipe import report


def test_paths_inside_the_repo_become_relative(tmp_path):
    root = str(tmp_path)
    inner = str(tmp_path / "work" / "job1" / "report.json")
    assert report.to_repo_relative(inner, root) == "work/job1/report.json"
    assert report.to_repo_relative(tmp_path / "bench" / "m1.json", root) == "bench/m1.json"


def test_absolute_paths_outside_the_repo_are_redacted(tmp_path):
    root = str(tmp_path)
    outside = r"C:\Users\someone\Temp\x.mp4" if os.name == "nt" else "/home/someone/x.mp4"
    value = report.sanitize({"path": outside}, root)["path"]
    assert "someone" not in value and "Temp" not in value
    assert report.REDACTED in value


def test_nested_structures_are_walked(tmp_path):
    root = str(tmp_path)
    payload = {"rows": [{"out": str(tmp_path / "a.ts"), "n": 1}],
               "meta": {"gpu": {"cards": [{"reason": str(tmp_path / "x.log")}]}}}
    out = report.sanitize(payload, root)
    assert out["rows"][0]["out"] == "a.ts"
    assert out["rows"][0]["n"] == 1
    assert out["meta"]["gpu"]["cards"][0]["reason"] == "x.log"


def test_numbers_and_none_survive_untouched(tmp_path):
    payload = {"wall_s": 12.5, "n": 3, "err": None, "ok": True}
    assert report.sanitize(payload, str(tmp_path)) == payload


def test_dump_round_trips_and_ends_with_newline(tmp_path):
    path = report.dump(str(tmp_path / "sub" / "x.json"), {"a": 1})
    assert report.load(path) == {"a": 1}
    with open(path, encoding="utf-8") as handle:
        assert handle.read().endswith("\n"), "trailing newline keeps git diffs clean"


def test_ladder_table_shows_a_cancelled_rung_without_inventing_numbers():
    table = report.ladder_table([
        {"rung": "2160p", "mode": "whole", "wall_s": 30.1, "speed_x": 0.8,
         "rung_res": "3840x2160", "target_kbps": 28000,
         "measured": {"video_kbps": 32892}, "vmaf": {"vmaf_mean": 94.88, "vmaf_min": 71.2}},
        {"rung": "720p", "cancelled": True},
    ])
    assert "32892" in table and "94.88" in table
    assert "cancelled" in table
    assert "None" not in table, "a cancelled rung must not print None"


def test_report_shape_is_stable_for_the_figure_reader():
    """figures.py reads these keys; renaming one silently produces no chart."""
    rep = report.build_report("job", {"ffmpeg": "7.1"}, [{"rung": "1080p"}], 12.0,
                            {"hls": {}, "dash": {}}, {"1080p": {}}, [], 15.0)
    for key in ("job_id", "meta", "encode", "ladder_wall_s", "packaging", "vmaf",
                "verify", "total_wall_s"):
        assert key in rep
