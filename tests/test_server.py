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

from vodpipe import mediarange as mr  # noqa: E402
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


# ------------------------------------------------- path containment (security)

GOOD_ID = "a1b2c3d4e5f6"
BAD_IDS = ["..", "%2e%2e", "%2E%2E", "a" * 11 + "z", "A1B2C3D4E5F6",
           "a1b2c3d4e5f6x", "j1"]


@pytest.fixture
def job_tree(tmp_path, monkeypatch):
    """A real one-job tree under roots we control, so 404 vs 200 is meaningful.

    A route that answered 404 to *everything* would pass a traversal test that
    only asserts 404, so every case below is paired with a request that must
    still succeed.
    """
    out_root = tmp_path / "out"
    work_root = tmp_path / "work"
    (out_root / GOOD_ID / "hls").mkdir(parents=True)
    (work_root / GOOD_ID).mkdir(parents=True)
    (out_root / GOOD_ID / "hls" / "seg_00001.m4s").write_bytes(b"A" * 2048)
    (work_root / GOOD_ID / "report.json").write_text('{"ok": true}', encoding="utf-8")
    # Canary outside both roots: reachable only by escaping.
    (tmp_path / "outside.ts").write_bytes(b"B" * 64)
    (tmp_path / "outside_report.json").write_text('{"leak": true}', encoding="utf-8")
    monkeypatch.setattr(settings, "OUT_ROOT", str(out_root))
    monkeypatch.setattr(settings, "WORK_ROOT", str(work_root))
    return {"out": out_root, "work": work_root, "tmp": tmp_path}


def test_legit_media_and_report_are_served(job_tree):
    """The positive control for every traversal assertion below."""
    client = testclient.TestClient(main.app)
    ok = client.get(f"/media/{GOOD_ID}/hls/seg_00001.m4s")
    assert ok.status_code == 200, ok.status_code
    assert ok.headers["accept-ranges"] == "bytes"
    assert ok.headers["content-type"] == "video/iso.segment"
    part = client.get(f"/media/{GOOD_ID}/hls/seg_00001.m4s",
                      headers={"range": "bytes=0-99"})
    assert part.status_code == 206
    assert part.headers["content-range"] == "bytes 0-99/2048"
    rep = client.get(f"/jobs/{GOOD_ID}/report")
    assert rep.status_code == 200 and rep.json() == {"ok": True}


def test_media_route_cannot_escape_its_job_directory(job_tree):
    """The bug: Starlette decodes `%2e%2e` and `%5c` *before* the handler sees
    them, so `os.path.join` walked out of OUT_ROOT; and an absolute segment
    (`C:` + backslash) made base == candidate, which made the self-referential
    `is_inside(path, base)` trivially true. `/media/%2e%2e/outside.ts` used to
    return 200 with a file from above the root."""
    client = testclient.TestClient(main.app)
    vectors = [
        "/media/%2e%2e/outside.ts",
        "/media/%2E%2E/outside.ts",
        "/media/%2e%2e%5coutside.ts",
        f"/media/{GOOD_ID}/hls/../../outside.ts",
        f"/media/{GOOD_ID}/hls/%2e%2e/outside.ts",
        f"/media/{GOOD_ID}%5c..%5coutside.ts",
        f"/media/{GOOD_ID}/../outside.ts",
        "/media/C%3a%5coutside.ts",
    ]
    for url in vectors:
        r = client.get(url)
        assert r.status_code == 404, (url, r.status_code, r.text[:60])


@pytest.mark.parametrize("bad_id", BAD_IDS)
def test_malformed_job_ids_are_404_not_a_backend_probe(bad_id, job_tree):
    """Ids this service never issued are rejected at the boundary, before the id
    reaches a filesystem path or a Redis key."""
    client = testclient.TestClient(main.app)
    assert client.get(f"/media/{bad_id}/hls/seg_00001.m4s").status_code == 404
    assert client.get(f"/jobs/{bad_id}").status_code == 404
    assert client.post(f"/jobs/{bad_id}/cancel").status_code == 404
    assert client.get(f"/jobs/{bad_id}/report").status_code == 404


def test_report_route_cannot_escape_its_work_root(job_tree):
    """`/jobs/<id>/report` had no containment check at all: any directory under
    the parent of WORK_ROOT containing a `report.json` was readable."""
    client = testclient.TestClient(main.app)
    for url in ["/jobs/%2e%2e/report", "/jobs/%2e%2e%5creport", "/jobs/%2E%2E/report"]:
        assert client.get(url).status_code == 404, url
    assert client.get(f"/jobs/{GOOD_ID}/report").status_code == 200


def test_iter_range_reads_in_bounded_chunks(tmp_path, monkeypatch):
    """`bytes=0-` is the most common first request a player sends and its length is
    attacker-shaped, so range delivery must not read the whole answer into one
    buffer. Chunk size is capped and the bytes stay byte-exact."""
    monkeypatch.setattr(main, "RANGE_CHUNK_BYTES", 4096)
    seg = tmp_path / "seg.m4s"
    seg.write_bytes(bytes(range(256)) * 40)  # 10240 bytes
    br = mr.parse_range("bytes=1000-", seg.stat().st_size)
    chunks = list(main.iter_range(str(seg), br))
    assert b"".join(chunks) == seg.read_bytes()[1000:]
    assert max(len(c) for c in chunks) <= main.RANGE_CHUNK_BYTES
    assert len(chunks) > 1  # chunked, not one big read


def test_failure_payload_is_scrubbed_of_host_paths():
    """The worker's exception text quotes absolute paths; /jobs/{id} returns it
    verbatim, so it must go through the same scrubber as committed reports."""
    from vodpipe import ff, jobs

    view = jobs.snapshot("FAILURE", ValueError(r"T:\resume\media\work\jobs\x\y.ts failed"))
    assert "resume" not in view["error"]
    assert ff.LOCAL_PATH in view["error"]


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
