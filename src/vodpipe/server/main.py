"""FastAPI in front of Celery: submit, progress, cancel, serve the ladder.

What is designed here rather than just wired:
  * the queue answers 202 + job_id immediately; the client polls progress;
  * progress is read from the same result backend as the status, so the two
    cannot disagree;
  * cancellation is cooperative (a Redis flag), not SIGKILL of the worker:
    killing an NVENC session mid-chunk leaves a corrupt TS segment;
  * byte-range serving, because players need ranges for CMAF/fMP4 and seeking.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import uvicorn
from celery.result import AsyncResult
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ..jobs import CANCEL_TTL_S, cancel_key, redact_url, resolve_source, snapshot
from ..mediarange import RangeNotSatisfiable, is_inside, media_type, parse_range
from . import settings, tasks
from .models import HealthOut, JobIn, JobOut

app = FastAPI(
    title="vodpipe",
    description="VOD transcoding: NVENC ladder, CMAF HLS/DASH, watermarking, VMAF.",
    version="1.0.0",
)

# The Redis client factory lives with the worker (tasks.state_client); the API
# reuses it so the connection settings and the lazy import exist once.
_state = tasks.state_client


# --------------------------------------------------------------------- health


@app.get("/health", response_model=HealthOut)
def health():
    """Report each dependency separately: the broker and the result backend are
    different services, and "queue down" and "progress down" need different fixes.

    A failing probe is recorded, never raised - an unreachable queue should
    still produce a 200 that names the broken part. Connection strings are
    reported with the password stripped: this endpoint is public.
    """
    broker_ok = backend_ok = False
    depth: int | None = None
    with contextlib.suppress(Exception):
        tasks.app.connection().ensure_connection(max_retries=1, timeout=2)
        broker_ok = True
    with contextlib.suppress(Exception):
        _state().ping()
        backend_ok = True
    with contextlib.suppress(Exception):
        depth = _queue_depth()
    return HealthOut(api="ok",
                     broker=redact_url(settings.BROKER_URL),
                     backend=redact_url(settings.STATE_URL),
                     broker_ok=broker_ok, backend_ok=backend_ok, queue_depth=depth)


def _queue_depth() -> int:
    """Queue depth straight from AMQP: declaring the queue is idempotent, exactly
    as Celery does, so this needs no management API."""
    import pika

    conn = pika.BlockingConnection(pika.URLParameters(settings.BROKER_URL))
    try:
        return conn.channel().queue_declare(queue="celery", durable=True).method.message_count
    finally:
        conn.close()


# ----------------------------------------------------------------------- jobs


@app.post("/jobs", response_model=JobOut, status_code=202)
def submit(job: JobIn):
    try:
        source = resolve_source(job.source, settings.DEFAULT_SOURCE, settings.MEDIA_ROOT)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not os.path.isfile(os.path.join(settings.MEDIA_ROOT, source)):
        raise HTTPException(404, f"source not found: {source}")

    job_id = uuid.uuid4().hex[:12]
    spec = {
        "source": source,
        "encoder": job.encoder,
        "preset": job.preset,
        "nchunks": job.nchunks,
        "workers": job.workers,
        "rungs": job.rungs,
        "seg_dur": job.seg_dur,
        "metrics": job.metrics,
        "watermark": job.watermark.model_dump() if job.watermark else None,
    }
    # task_id == job_id: the client's handle is also the Celery key, so status
    # needs no id mapping table.
    tasks.transcode.apply_async(args=[job_id, spec], task_id=job_id, queue="celery")
    return JobOut(job_id=job_id, state="PENDING", stage="queued")


def _snapshot(job_id: str) -> JobOut:
    result = AsyncResult(job_id, app=tasks.app)
    view = snapshot(result.state, result.info)
    return JobOut(job_id=job_id, **view)


@app.get("/jobs/{job_id}", response_model=JobOut)
def status(job_id: str):
    return _snapshot(job_id)


@app.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel(job_id: str):
    """Ask the worker to stop at the next rung boundary.

    Celery's revoke is deliberately not used: it marks the task REVOKED and
    erases the stored result, which would throw away the rungs that already
    finished successfully. The TTL means a stale flag can never cancel a later
    job that happens to reuse an id.
    """
    with contextlib.suppress(Exception):
        # If the backend is down the job simply runs to completion; the client
        # can retry. Failing the request would be worse than a no-op cancel.
        _state().setex(cancel_key(job_id), CANCEL_TTL_S, "1")
    return _snapshot(job_id)


# Route templates are literal by FastAPI's design. The client-facing URLs the
# worker returns live in `report.report_url`/`report.hls_url`; if you rename a
# route here, change those too.
@app.get("/jobs/{job_id}/report")
def job_report(job_id: str):
    path = os.path.join(settings.WORK_ROOT, job_id, "report.json")
    if not os.path.isfile(path):
        raise HTTPException(404, "no report yet")
    return FileResponse(path, media_type="application/json", filename=f"{job_id}.json")


# ---------------------------------------------------------------------- media


@app.get("/media/{job_id}/{rest:path}")
def serve(job_id: str, rest: str, request: Request):
    """Serve manifests and segments, with byte-range support for seeking.

    Both the job directory and the resolved file are checked against their
    roots: `..` in the URL must not reach outside the job's own output.
    """
    base = os.path.normpath(os.path.join(settings.OUT_ROOT, job_id))
    path = os.path.normpath(os.path.join(base, rest))
    if not is_inside(path, base) or not os.path.isfile(path):
        raise HTTPException(404, "not found")

    size = os.path.getsize(path)
    try:
        byte_range = parse_range(request.headers.get("range"), size)
    except RangeNotSatisfiable as exc:
        return JSONResponse({"detail": "range not satisfiable"}, status_code=416,
                            headers={"content-range": f"bytes */{size}", **exc.headers})

    if byte_range is None:
        return FileResponse(path, media_type=media_type(path),
                            headers={"accept-ranges": "bytes"})

    with open(path, "rb") as handle:
        handle.seek(byte_range.start)
        body = handle.read(byte_range.length)
    return Response(content=body, status_code=206, media_type=media_type(path),
                    headers=byte_range.content_headers())


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
