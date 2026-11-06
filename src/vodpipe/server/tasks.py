"""Celery worker: a queue on top of vodpipe.Pipeline.

Topology: RabbitMQ is the broker (job buffer), Redis is the result backend and
also holds progress and the cancel flag. Progress is written with
task.update_state, so the API reads it through the same AsyncResult it uses for
the status - there is no second source of truth.

Cancellation is cooperative and checked between rungs: Celery's terminate=True
kills the worker process mid-encode and leaves a truncated TS segment, while a
pipeline must never publish broken output. Cancelling between rungs keeps every
finished rung usable, so a cancelled job still ships something.
"""

from __future__ import annotations

import logging
import os
import time

from celery import Celery

from .. import encoders, ff, gpu, pipeline, report
from ..jobs import cancel_key, rung_subset, rung_summary
from ..ladder import ladder
from . import settings

#: Worker-level events go to the logger, not to stdout: this process is started
#: by an operator or a supervisor, and its output is read as a log. The pipeline
#: core it calls still prints, because the same code drives `vodpipe demo`, where
#: stdout *is* the interface - a deliberate split, not an oversight.
log = logging.getLogger("vodpipe.worker")

app = Celery("vodpipe", broker=settings.BROKER_URL, backend=settings.RESULT_BACKEND)
app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_soft_time_limit=3600,
    task_time_limit=3900,
    # One job per worker: a single NVENC session already saturates the GPU.
    worker_prefetch_multiplier=1,
    # Worker died -> the job goes back to the queue instead of vanishing.
    task_acks_late=True,
    result_expires=86400,
)


def state_client():
    """The state/result Redis. Imported lazily so the library imports fine
    without the optional `server` extra installed."""
    import redis

    return redis.Redis.from_url(settings.STATE_URL, decode_responses=True)


def _check_cancel(job_id: str) -> bool:
    """True if the client asked to cancel. A Redis outage must not fail a job
    that is otherwise fine, so an unreachable backend reads as "no cancel"."""
    try:
        return bool(state_client().exists(cancel_key(job_id)))
    except Exception:
        return False


def _pick_gpu(encoder: str) -> dict | None:
    """Probe the cards instead of trusting a GPU index: the order changes with
    the machine's hardware configuration."""
    if not settings.GPU_AUTO:
        return None
    enc = encoders.build(encoder)
    if not enc.hw:
        return None
    rep = gpu.pick(enc.codec)
    # A pick that lands on the wrong card silently costs minutes per rung, so it
    # belongs in the log the operator reads, not only in stdout of a manual run.
    log.info("gpu pick: %s", gpu.summary(rep))
    return rep


def failed_integrity(checks: list) -> list[dict]:
    """Rungs whose frame-count check failed. `ok: false` means the concat lost or
    duplicated frames, so that rendition is not correct. Whole-file rungs emit no
    checks, so only a chunked job can produce a non-empty result."""
    return [c for c in checks if c.get("ok") is False]


def build_payload(*, job_id: str, cancelled: bool, encoder: str, preset: str | None,
                  nchunks: int, workers: int, source_summary: str, duration: float,
                  gpurep: dict | None, watermark: dict | None, results: list,
                  wall: float, pkg: dict | None, vmaf: dict | None,
                  checks: list, total_wall_s: float) -> dict:
    """The `report.json` shape, built from plain values.

    Split out of the task body because the task cannot run in a unit test (it
    needs a broker, a backend and a GPU) while this schema can. `verify` is the
    chunk-integrity result: the queued path used to ship a report without it, so
    the only stage that proves a concat dropped no frames existed on the CLI path.
    """
    return {
        "job_id": job_id,
        "cancelled": cancelled,
        "meta": report.job_meta(ffmpeg=ff.version(), encoder=encoder, preset=preset,
                                nchunks=nchunks, workers=workers, source=source_summary,
                                duration=duration, gpurep=gpurep, watermark=watermark),
        "encode": results,
        "ladder_wall_s": wall,
        "packaging": pkg,
        "vmaf": vmaf,
        "verify": checks,
        "total_wall_s": total_wall_s,
    }


@app.task(name="vodpipe.transcode", bind=True, max_retries=0)
def transcode(self, job_id: str, spec: dict) -> dict:
    """Run one ladder job and return the summary the client polls for.

    Progress budget: gpu-probe 0.01, encode 0.05..0.60 (per rung), packaging
    0.65, VMAF 0.75. Encoding is the long phase, so it reports per rung; if it
    only reported between phases the client would stare at one number for
    minutes.
    """
    t0 = time.perf_counter()
    src = spec.get("source") or settings.DEFAULT_SOURCE
    if not os.path.isabs(src):
        src = os.path.join(settings.MEDIA_ROOT, src)
    if not os.path.isfile(src):
        raise RuntimeError(f"source not found: {src}")

    encoder = spec.get("encoder", "h264_nvenc")
    # No preset default here: `encoders.build` fills the per-backend default.
    # Hardcoding "p5" (an NVENC preset) was what killed the libx264 fallback.
    preset = spec.get("preset")
    nchunks = int(spec.get("nchunks", 1))
    workers = int(spec.get("workers", 1))

    # Probing cards takes seconds per failed card, so it is its own stage:
    # otherwise the client sees "probe" and assumes the job is stuck.
    self.update_state(state="PROGRESS", meta={"job_id": job_id, "progress": 0.01,
                                             "stage": "gpu-probe", "rungs": []})
    gpurep = _pick_gpu(encoder)
    if gpurep and gpurep.get("picked") is None:
        raise RuntimeError("no GPU with working NVENC; use encoder=libx264")

    work = os.path.join(settings.WORK_ROOT, job_id)
    out_dir = os.path.join(settings.OUT_ROOT, job_id)
    os.makedirs(work, exist_ok=True)

    p = pipeline.Pipeline(src, work, wm_spec=spec.get("watermark") or {})
    rungs = rung_subset(spec.get("rungs"), ladder())

    def progress(frac: float, stage: str, done: list | None = None,
                 vmaf: dict | None = None) -> None:
        self.update_state(state="PROGRESS", meta={
            "job_id": job_id,
            "progress": round(min(frac, 0.99), 3),
            "stage": stage,
            "rungs": rung_summary(done or [], vmaf),
        })

    progress(0.02, "probe")

    cancelled = {"flag": False}

    def should_cancel() -> bool:
        if _check_cancel(job_id):
            cancelled["flag"] = True
            return True
        return False

    results, wall = p.run_ladder(
        encoder=encoder, preset=preset, nchunks=nchunks, workers=workers,
        tag="job", rungs=rungs,
        on_rung_done=lambda done, total, acc: progress(
            0.05 + 0.55 * done / max(total, 1), f"encode {done}/{total}", acc),
        should_cancel=should_cancel,
    )
    finished = [r for r in results if not r.get("cancelled")]
    # Integrity belongs to the pipeline, not to the CLI that happens to call it.
    # A queued chunked job used to publish without this check, so the one stage
    # that proves a concat dropped no frames only ran in `vodpipe demo`.
    # It is cheap when nothing was chunked (no ffprobe at all).
    checks = p.verify(finished)

    def finish(pkg: dict | None = None, vmaf: dict | None = None) -> dict:
        """One final report shape for both the success and the cancelled path.

        A cancelled job is not a failed job: finished rungs are kept, the
        status says CANCELLED, and the client decides whether to reuse them.
        """
        payload = build_payload(
            job_id=job_id, cancelled=cancelled["flag"], encoder=encoder, preset=preset,
            nchunks=nchunks, workers=workers, source_summary=p.src.summary(),
            duration=p.src.duration, gpurep=gpurep, watermark=spec.get("watermark"),
            results=results, wall=wall, pkg=pkg, vmaf=vmaf, checks=checks,
            total_wall_s=round(time.perf_counter() - t0, 2))
        report_path = report.dump(os.path.join(work, "report.json"),
                                  report.sanitize(payload, settings.MEDIA_ROOT))
        # Artifact paths are returned to the client, so they are repo-relative:
        # an absolute T:\... path is meaningless to anyone who is not this host
        # and leaks the machine layout into every response.
        artifacts = {
            "report": report.to_repo_relative(report_path, settings.MEDIA_ROOT),
            "report_url": report.report_url(job_id),
        }
        if pkg:
            artifacts.update({
                "hls_master": report.to_repo_relative(
                    os.path.join(out_dir, "hls", "master.m3u8"), settings.MEDIA_ROOT),
                "dash_mpd": report.to_repo_relative(
                    os.path.join(out_dir, "dash", "manifest.mpd"), settings.MEDIA_ROOT),
                "hls_url": report.hls_url(job_id),
                "hls_variants": pkg["hls"].get("variants"),
                "segments": pkg["hls"].get("segments"),
            })
        return {
            "job_id": job_id,
            "cancelled": cancelled["flag"],
            "total_wall_s": payload["total_wall_s"],
            "ladder_wall_s": wall,
            "rungs": rung_summary(results, vmaf),
            "artifacts": artifacts,
            "gpu": gpurep,
        }

    if cancelled["flag"]:
        return finish()

    pkg = vmaf = None
    if should_cancel():
        return finish()

    # Integrity is a gate, not a footnote. A chunked rung whose concatenated
    # packet count is wrong has lost (or duplicated) frames, so its output is
    # not a correct rendition no matter how fast it encoded; shipping it as
    # SUCCESS is worse than failing. Whole-file rungs produce no checks, so
    # only the chunked path can trip here. A cancelled job returns above and
    # is judged by its own "keep what finished, flagged" rule.
    bad = failed_integrity(checks)
    if bad:
        detail = ", ".join(f"{c['rung']} {c.get('vpkts')}!={c.get('expected_frames')}"
                           for c in bad)
        raise RuntimeError(f"chunk integrity check failed: {detail}")

    progress(0.65, "package", finished)
    pkg = p.package(finished, out_dir, seg_dur=float(spec.get("seg_dur", 4.0)))
    if should_cancel():
        # Packaging already produced a usable ladder: ship it, flagged.
        return finish(pkg)

    vmaf = {}
    if spec.get("metrics", True):
        progress(0.75, "vmaf", finished)
        try:
            vmaf = p.vmaf_for(finished, tag="job_")
        except Exception as exc:  # metrics must not lose an finished ladder
            vmaf = {"error": str(exc)}
    return finish(pkg, vmaf)


@app.task(name="vodpipe.ping")
def ping():
    return "pong"


def worker_argv() -> list[str]:
    """How to start the worker.

    On Windows Celery's prefork pool cannot fork (billiard limitation), so the
    pool is threads. Concurrency stays 1 either way: one NVENC job per worker.
    """
    argv = ["celery", "-A", "vodpipe.server.tasks", "worker", "-l", "info",
            "-Q", "celery", "-c", "1"]
    if os.name == "nt":
        argv += ["-P", "threads"]
    return argv


if __name__ == "__main__":
    import subprocess
    import sys

    sys.exit(subprocess.call(worker_argv()))
