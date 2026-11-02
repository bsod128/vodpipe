"""Queue job semantics: the two rules that are easy to get wrong.

Runs without FastAPI, Celery or a broker - which is exactly why this logic
lives in vodpipe.jobs rather than inside a route handler.
"""

from __future__ import annotations

import os

import pytest

from vodpipe import jobs
from vodpipe.ladder import ladder


def test_cancelled_job_is_a_distinct_terminal_state():
    """Celery reports SUCCESS because the task returned a partial result on
    purpose. The client still needs CANCELLED, and the flag lives in the payload."""
    view = jobs.snapshot("SUCCESS", {"cancelled": True, "rungs": [{"rung": "1080p"}]})
    assert view["state"] == "CANCELLED"
    assert view["cancelled"] is True
    assert view["stage"] == "cancelled"
    assert view["progress"] == 1.0
    assert view["rungs"] == [{"rung": "1080p"}]


def test_successful_job_exposes_artifacts_and_timings():
    view = jobs.snapshot("SUCCESS", {
        "cancelled": False, "total_wall_s": 12.3, "ladder_wall_s": 9.9,
        "gpu": {"picked": 4}, "artifacts": {"hls_master": "x"}, "rungs": []})
    assert view["state"] == "SUCCESS"
    assert view["stage"] == "done"
    assert view["result"] == {"total_wall_s": 12.3, "ladder_wall_s": 9.9,
                             "gpu": {"picked": 4}}


def test_running_state_passes_progress_through():
    view = jobs.snapshot("PROGRESS", {"progress": 0.42, "stage": "encode 2/5",
                                     "rungs": [{"rung": "2160p"}]})
    assert view["state"] == "RUNNING"
    assert view["progress"] == 0.42
    assert view["stage"] == "encode 2/5"


def test_pending_and_unknown_states_are_not_running():
    assert jobs.snapshot("PENDING", None)["state"] == "PENDING"
    assert jobs.snapshot("PENDING", None)["progress"] == 0.0


def test_failure_state_reports_the_error_text():
    view = jobs.snapshot("FAILURE", "no GPU with working NVENC")
    assert view["state"] == "FAILURE"
    assert "NVENC" in view["error"]


def test_revoked_is_mapped_to_cancelled():
    """Only reachable if something revoked outside this codebase; the client
    must still see a coherent terminal state."""
    assert jobs.snapshot("REVOKED", None)["state"] == "CANCELLED"


def test_cancel_key_is_scoped_and_prefixed():
    assert jobs.cancel_key("abc123").endswith("abc123")
    assert jobs.cancel_key("abc123") != jobs.cancel_key("abc124")
    assert jobs.CANCEL_TTL_S > 0, "a stale flag must expire, never linger forever"


def test_rung_summary_keeps_rows_compact():
    """Progress payloads are polled every couple of seconds: full per-rung
    detail (per-frame VMAF, chunk lists) would make the polling itself costly."""
    wide = {"rung": "1080p", "encoder": "h264_nvenc", "mode": "whole", "wall_s": 3.1,
            "speed_x": 7.9, "measured": {"video_kbps": 5000},
            "chunks": [{"t": i} for i in range(400)], "vmaf_frames": [1.0] * 4000}
    rows = jobs.rung_summary([wide], vmaf={"1080p": {"vmaf_mean": 94.88}})
    assert rows == [{"rung": "1080p", "encoder": "h264_nvenc", "mode": "whole",
                    "wall_s": 3.1, "speed_x": 7.9, "measured_kbps": 5000,
                    "vmaf": 94.88}]
    assert len(repr(rows)) < 400


def test_rung_summary_marks_cancelled_rungs():
    rows = jobs.rung_summary([{"rung": "720p", "cancelled": True, "out": None}])
    assert rows[0]["mode"] == "cancelled"


def test_rung_subset_keeps_tallest_first_and_none_for_default():
    rungs = ladder()
    assert jobs.rung_subset(None, rungs) is None
    assert jobs.rung_subset([], rungs) is None
    picked = jobs.rung_subset(["720p", "2160p", "1080p"], rungs)
    assert [r.label for r in picked] == ["2160p", "1080p", "720p"]


def test_rung_subset_of_unknown_labels_falls_back_to_default():
    """Returning [] here would silently encode nothing; None means "use the
    default ladder" and the API has already rejected unknown labels anyway."""
    assert jobs.rung_subset(["480p"], ladder()) is None


def test_source_is_stored_relative_to_the_media_root():
    """The worker may live in a container where the same file has a different
    absolute path, so the job spec carries a root-relative path."""
    root = os.path.join("T:", os.sep, "media")
    rel = jobs.resolve_source(os.path.join("src_clip", "m.mp4"), None, root)
    assert rel == os.path.join("src_clip", "m.mp4")
    absolute = os.path.join(root, "src_clip", "m.mp4")
    assert jobs.resolve_source(absolute, None, root) == os.path.join("src_clip", "m.mp4")


def test_source_outside_the_media_root_is_refused():
    root = os.path.join("T:", os.sep, "media")
    for attempt in ("..\\secrets.csv", os.path.join("..", "secrets.csv")):
        with pytest.raises(ValueError):
            jobs.resolve_source(attempt, None, root)


def test_default_source_is_resolved_inside_the_root():
    root = os.path.join("T:", os.sep, "media")
    default = os.path.join(root, "src_clip", "4k_master.mp4")
    assert jobs.resolve_source(None, default, root) == os.path.join(
        "src_clip", "4k_master.mp4")


def test_health_reports_urls_without_passwords():
    """/health is public and used in screenshots, so it must not hand out broker
    credentials. Host and port stay: that is what an operator needs."""
    red = jobs.redact_url("amqp://vodpipe:secret@rabbit:5672/")
    assert red == "amqp://vodpipe@rabbit:5672/"
    assert "secret" not in red
    assert jobs.redact_url("redis://localhost:6379/0") == "redis://localhost:6379/0"
    assert jobs.redact_url("redis://:pw@redis:6379/0") == "redis://@redis:6379/0"


def test_worker_argv_uses_threads_on_windows_and_one_slot_anywhere():
    """Celery cannot fork on Windows (billiard), and one NVENC session already
    saturates a card, so concurrency is 1 regardless of platform."""
    from vodpipe.server import tasks

    argv = tasks.worker_argv()
    assert "-c" in argv and argv[argv.index("-c") + 1] == "1"
    if os.name == "nt":
        assert "-P" in argv and argv[argv.index("-P") + 1] == "threads"
