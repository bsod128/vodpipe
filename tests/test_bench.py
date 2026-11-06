"""Selection.parse must reject a typo, not quietly shrink the matrix.

A benchmark that drops an unknown config and exits 0 prints a table missing a
row the reader assumes is there - the same failure mode the strict `--rungs`
path was added to close.
"""
from __future__ import annotations

import pytest

from vodpipe import bench


def test_all_expands_to_every_matrix_tag():
    sel = bench.Selection.parse("720p", "all")
    assert sel.configs == sorted({c[0] for c in bench.MATRIX})


def test_subset_is_kept_in_order():
    sel = bench.Selection.parse("720p", "nvenc_p5_whole,nvenc_p5_c2w2")
    assert sel.configs == ["nvenc_p5_whole", "nvenc_p5_c2w2"]


def test_unknown_config_raises_rather_than_running_short():
    with pytest.raises(ValueError, match="unknown config"):
        bench.Selection.parse("720p", "nvenc_p5_whole,not_a_real_config")


def test_noise_runs_reports_a_spread_per_config(tmp_path, monkeypatch):
    # Drive noise_runs with a stubbed matrix so no encode is needed: the rows it
    # sees are the only thing it summarises.
    calls = {"n": 0}

    def fake_run_matrix(src, work, sel, do_vmaf=False, runner=None):
        calls["n"] += 1
        return [{"rung": "720p", "tag": "nvenc_p5_whole", "wall_s": 5.0 + calls["n"]}]

    monkeypatch.setattr(bench, "run_matrix", fake_run_matrix)
    sel = bench.Selection.parse("720p", "nvenc_p5_whole")
    rows = bench.noise_runs(None, str(tmp_path), sel, repeats=3)
    assert len(rows) == 1
    row = rows[0]
    assert row["runs_s"] == [6.0, 7.0, 8.0]
    assert row["min_s"] == 6.0 and row["max_s"] == 8.0
    assert row["spread_pct"] == 33.3  # (8-6)/6
