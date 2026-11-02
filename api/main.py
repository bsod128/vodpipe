# -*- coding: utf-8 -*-
"""FastAPI поверх Celery: приём задач транскодинга, прогресс, отмена, отдача артефактов.

Что здесь спроектировано, а не «просто роуты»:
  * очередь принимает задачу мгновенно и отвечает 202 + job_id; клиент поллит прогресс;
  * прогресс читается из того же result backend, что и статус, - рассинхрона нет;
  * отмена кооперативная (флаг в Redis), а не SIGKILL воркера: обрывать NVENC-сессию
    посреди чанка нельзя, останется битый TS;
  * Range-раздача сегментов: плеер требует byte-range для CMAF/fMP4 и перемотки.
"""
from __future__ import annotations

import os
import re
import uuid

import redis
import uvicorn
from celery.result import AsyncResult
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

import settings
import tasks
from models import HealthOut, JobIn, JobOut

app = FastAPI(
    title="vodpipe",
    description="VOD-транскодинг: NVENC-лестница, CMAF HLS/DASH, водяные маркеры, VMAF.",
    version="1.0",
)

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
MEDIA_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mpd": "application/dash+xml",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".ts": "video/mp2t",
    ".json": "application/json",
}


def _state():
    return redis.Redis.from_url(settings.STATE_URL, decode_responses=True)


def _inside(path: str, root: str) -> bool:
    path, root = os.path.normpath(path), os.path.normpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _resolve_source(job: JobIn) -> str:
    """Путь нормализуем к виду относительно медиа-корня: воркер может жить в другом
    контейнере/на хосте, где у того же файла другой абсолютный путь."""
    src = job.source or settings.DEFAULT_SOURCE
    if not os.path.isabs(src):
        src = os.path.join(settings.MEDIA_ROOT, src)
    src = os.path.normpath(src)
    root = os.path.normpath(settings.MEDIA_ROOT)
    if not _inside(src, root):
        raise HTTPException(400, "source must live inside the media root")
    if not os.path.isfile(src):
        raise HTTPException(404, f"source not found: {src}")
    return os.path.relpath(src, root)


@app.get("/health", response_model=HealthOut)
def health():
    st = _state()
    broker_ok = backend_ok = False
    depth = None
    try:
        # Проверка именно брокера (RabbitMQ), а не result backend: это разные узлы.
        conn = tasks.app.connection()
        conn.ensure_connection(max_retries=1, timeout=2)
        broker_ok = True
    except Exception:
        pass
    try:
        st.ping()
        backend_ok = True
    except Exception:
        pass
    try:
        depth = _queue_depth()
    except Exception:
        pass
    return HealthOut(api="ok", broker=settings.BROKER_URL, backend=settings.STATE_URL,
                     broker_ok=broker_ok, backend_ok=backend_ok, queue_depth=depth)


def _queue_depth() -> int:
    """Глубина очереди прямо из AMQP: declare (идемпотентно, как у Celery) без
    зависимости от management API."""
    import pika
    p = pika.BlockingConnection(pika.URLParameters(settings.BROKER_URL))
    try:
        ch = p.channel()
        return ch.queue_declare(queue="celery", durable=True).method.message_count
    finally:
        p.close()


@app.post("/jobs", response_model=JobOut, status_code=202)
def submit(job: JobIn):
    src = _resolve_source(job)
    job_id = uuid.uuid4().hex[:12]
    spec = {
        "source": src,
        "encoder": job.encoder,
        "preset": job.preset,
        "nchunks": job.nchunks,
        "workers": job.workers,
        "rungs": job.rungs,
        "seg_dur": job.seg_dur,
        "metrics": job.metrics,
        "watermark": job.watermark.model_dump() if job.watermark else None,
    }
    tasks.transcode.apply_async(args=[job_id, spec], task_id=job_id, queue="celery")
    return JobOut(job_id=job_id, state="PENDING", stage="queued")


def _snapshot(job_id: str) -> JobOut:
    r = AsyncResult(job_id, app=tasks.app)
    state = r.state
    out = JobOut(job_id=job_id, state=state)
    if state == "PROGRESS":
        meta = r.info or {}
        out.state = "RUNNING"
        out.progress = meta.get("progress", 0.0)
        out.stage = meta.get("stage")
        out.rungs = meta.get("rungs")
    elif state == "SUCCESS":
        res = r.info or {}
        out.progress = 1.0
        out.stage = "cancelled" if res.get("cancelled") else "done"
        out.cancelled = bool(res.get("cancelled"))
        # отменённая задача остаётся SUCCESS для Celery (она штатно вернула
        # частичный результат), но для клиента это отдельный терминальный статус
        if out.cancelled:
            out.state = "CANCELLED"
        out.rungs = res.get("rungs")
        out.artifacts = res.get("artifacts")
        out.result = {k: res.get(k) for k in ("total_wall_s", "ladder_wall_s", "gpu")}
    elif state == "REVOKED":
        out.state = "CANCELLED"
        out.cancelled = True
    elif state == "FAILURE":
        out.error = str(r.result)
    return out


@app.get("/jobs/{job_id}", response_model=JobOut)
def status(job_id: str):
    return _snapshot(job_id)


@app.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel(job_id: str):
    """Отмена только на границе фазы/ранга.

    Celery-revoke здесь не используется: он помечает задачу REVOKED и затирает
    результат, а нам нужно вернуть уже готовые ранги и отчёт.
    """
    try:
        _state().setex("vodpipe:cancel:" + job_id, 3600, "1")
    except Exception:
        pass
    return _snapshot(job_id)


@app.get("/jobs/{job_id}/report")
def report(job_id: str):
    path = os.path.join(settings.WORK_ROOT, job_id, "report.json")
    if not os.path.isfile(path):
        raise HTTPException(404, "no report yet")
    return FileResponse(path, media_type="application/json", filename=f"{job_id}.json")


@app.get("/media/{job_id}/{rest:path}")
def serve(job_id: str, rest: str, request: Request):
    """CMAF-сегменты и манифесты с Range-поддержкой (перемотка, byte-range)."""
    base = os.path.normpath(os.path.join(settings.OUT_ROOT, job_id))
    if not _inside(base, settings.MEDIA_ROOT):
        raise HTTPException(400, "bad path")
    path = os.path.normpath(os.path.join(base, rest))
    if not _inside(path, base) or not os.path.isfile(path):
        raise HTTPException(404, "not found")

    media = MEDIA_TYPES.get(os.path.splitext(path)[1], "application/octet-stream")
    size = os.path.getsize(path)

    rng = request.headers.get("range")
    if rng:
        m = RANGE_RE.match(rng)
        if not m:
            raise HTTPException(416, "unsupported range")
        start = int(m.group(1)) if m.group(1) else 0
        end = int(m.group(2)) if m.group(2) else size - 1
        if start >= size or end >= size or start > end:
            return JSONResponse({"detail": "range not satisfiable"}, status_code=416,
                                headers={"content-range": f"bytes */{size}"})
        with open(path, "rb") as f:
            f.seek(start)
            body = f.read(end - start + 1)
        return Response(content=body, status_code=206, media_type=media,
                        headers={"content-range": f"bytes {start}-{end}/{size}",
                                 "accept-ranges": "bytes"})

    return FileResponse(path, media_type=media, headers={"accept-ranges": "bytes"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("VODPIPE_PORT", 8000)))
