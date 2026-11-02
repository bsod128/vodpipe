"""The API boundary: what a client may submit, and what it gets back.

The queue itself is exercised end-to-end by hand (docker compose); what is cheap
to get wrong and expensive to discover late lives here, at the schema edge,
before a worker is ever occupied:

* an encoder/preset pair ffmpeg will reject - the GPU-absent fallback used to
  inherit an NVENC preset and die *after* the job was queued;
* watermark text that would break out of, or escape, the filtergraph;
* the report and media URLs the worker returns actually matching the routes;
* `/health` naming its dependencies without handing out their credentials.

Needs the optional `server` extra; the file skips cleanly without it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")

from pydantic import ValidationError  # noqa: E402

from vodpipe import report  # noqa: E402
from vodpipe.server import main, settings, tasks  # noqa: E402
from vodpipe.server.models import JobIn, WatermarkIn  # noqa: E402

# --------------------------------------------------------------- preset rules


def test_preset_defaults_are_per_backend():
    """Each backend has its own vocabulary; there is no global default."""
    assert JobIn(encoder="libx264").preset == "medium"
    assert JobIn(encoder="h264_nvenc").preset == "p5"
    assert JobIn(encoder="hevc_nvenc").preset == "p5"


def test_nvenc_preset_is_rejected_for_libx264_at_the_boundary():
    """The bug: a global default of "p5" reached libx264, and x264 exited 1 on
    a job that had already been accepted and queued."""
    with pytest.raises(ValidationError, match="libx264"):
        JobIn(encoder="libx264", preset="p5")


def test_a_valid_explicit_preset_survives():
    assert JobIn(encoder="h264_nvenc", preset="p1").preset == "p1"
    assert JobIn(encoder="libx264", preset="slow").preset == "slow"


def test_encoder_vocabulary_is_enforced():
    with pytest.raises(ValidationError, match="encoder must be one of"):
        JobIn(encoder="h264_crayon")


def test_unknown_rung_is_rejected():
    with pytest.raises(ValidationError, match="unknown rungs"):
        JobIn(rungs=["1000p"])


# ----------------------------------------------------------- watermark rules


@pytest.mark.parametrize("value", ["MAXIMA VOD", "viewer-99", "id:7", "a.b_c"])
def test_watermark_allow_list_accepts_normal_ids(value):
    assert WatermarkIn(static=value).static == value


@pytest.mark.parametrize("value", ["x',scale=1", "x';y", "a,b", "x" * 65, "nl\nx"])
def test_filtergraph_payloads_are_rejected_at_the_boundary(value):
    """Watermark text lands inside a -vf chain. `watermark.escape()` keeps a
    legitimate value parseable; the schema refuses anything else outright."""
    with pytest.raises(ValidationError, match="watermark text"):
        WatermarkIn(dynamic=value)


def test_source_path_traversal_is_rejected():
    with pytest.raises(ValidationError, match="path traversal"):
        JobIn(source="../../etc/passwd")


# ------------------------------------------------------------ urls / routes


def test_url_helpers_match_registered_routes():
    """The worker hands these URLs back in a job's artifacts while FastAPI
    registers the routes, so both are derived from the same strings."""
    assert report.report_url("j1") == "/jobs/j1/report"
    assert report.hls_url("j1") == "/media/j1/hls/master.m3u8"
    assert report.media_url("j1", "dash/stream.mpd") == "/media/j1/dash/stream.mpd"

    registered = {r.path for r in main.app.routes if hasattr(r, "path")}
    assert "/jobs/{job_id}/report" in registered
    assert "/media/{job_id}/{rest:path}" in registered


def test_api_and_worker_share_one_state_client():
    """Progress and cancellation go through a single client factory, or the API
    and the worker can end up pointing at different Redis instances."""
    assert main._state is tasks.state_client


def test_health_reports_dependencies_without_leaking_credentials(monkeypatch):
    """/health is public, so it names each dependency with the password
    stripped. A dependency that is down is a flag in the body, never a 500:
    "queue down" and "progress down" need different fixes, and the operator has
    to be able to tell them apart.

    `main` reads the URLs through the settings module, so patching the module
    attributes is enough - no reload, no state leaking into other tests.
    """
    monkeypatch.setattr(settings, "BROKER_URL", "amqp://vodpipe:s3cr3t@rabbit:5672/")
    monkeypatch.setattr(settings, "STATE_URL", "redis://:s3cr3t@redis:6379/0")

    res = testclient.TestClient(main.app).get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["api"] == "ok"
    assert body["broker"] == "amqp://vodpipe@rabbit:5672/"
    assert body["backend"] == "redis://@redis:6379/0"
    assert "s3cr3t" not in res.text
    for flag in ("broker_ok", "backend_ok", "queue_depth"):
        assert flag in body
