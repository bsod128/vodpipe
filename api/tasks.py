# -*- coding: utf-8 -*-
"""Celery-воркер: очередь задач поверх vodpipe.Pipeline.

Топология: RabbitMQ = broker (буфер задач), Redis = result backend + состояние
задачи (прогресс/отмена). Прогресс пишется через task.update_state, поэтому API
читает его тем же AsyncResult, чем и статус: второго источника правды нет.

Отмена кооперативная: между фазами проверяется флаг в Redis. terminate=True у
Celery убивает процесс воркера и оставляет битый TS-чанк, а видеопайплайну
нужна чистая остановка.
"""
from __future__ import annotations

import os
import sys
import time

from celery import Celery

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings  # noqa: E402
from vodpipe import ff, gpu, pipeline  # noqa: E402
from vodpipe.encoders import ENCODERS, ladder_4k  # noqa: E402

app = Celery("vodpipe", broker=settings.BROKER_URL, backend=settings.RESULT_BACKEND)
app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Moscow",
    task_soft_time_limit=3600,
    task_time_limit=3900,
    worker_prefetch_multiplier=1,   # одна задача на воркера: NVENC не делится
    task_acks_late=True,            # воркер умер -> задача вернулась в очередь
    result_expires=86400,
)

CANCEL_KEY = "vodpipe:cancel:"


def _redis():
    import redis
    return redis.Redis.from_url(settings.STATE_URL, decode_responses=True)


class JobCancelled(Exception):
    pass


def _check_cancel(task, job_id: str):
    try:
        if _redis().exists(CANCEL_KEY + job_id):
            raise JobCancelled("cancelled by client")
    except JobCancelled:
        raise
    except Exception:
        pass  # Redis недоступен -> не роняем задачу из-за bookkeeping


def _pick_gpu(encoder_name: str) -> dict | None:
    """Индекс GPU не хардкодим: он меняется вместе с конфигом железа."""
    if not settings.GPU_AUTO:
        return None
    enc = ENCODERS[encoder_name]()
    if not enc.hw:
        return None
    rep = gpu.pick_nvenc_gpu(enc.codec)
    print(gpu.summary(rep))
    return rep


def _subset(labels):
    if not labels:
        return None
    wanted = set(labels)
    rungs = [r for r in ladder_4k() if r.label in wanted]
    rungs.sort(key=lambda r: r.height, reverse=True)
    return rungs or None


def _summ_rungs(results, vmaf=None):
    out = []
    for r in results:
        m = r.get("measured") or {}
        row = {
            "rung": r["rung"],
            "encoder": r["encoder"],
            "mode": r.get("mode"),
            "wall_s": r.get("wall_s"),
            "speed_x": r.get("speed_x"),
            "measured_kbps": m.get("video_kbps") or m.get("bitrate_kbps"),
        }
        if vmaf:
            v = vmaf.get(r["rung"]) or {}
            row["vmaf"] = v.get("vmaf_mean")
        out.append(row)
    return out


@app.task(name="vodpipe.transcode", bind=True, max_retries=0)
def transcode(self, job_id: str, spec: dict) -> dict:
    """spec -> итоговый отчёт. Прогресс: encode 0.05..0.60, package 0.65, vmaf 0.75."""
    t0 = time.perf_counter()
    # Путь в задании относительный к медиа-корню: у воркера он может быть другим
    # (в контейнере /media, на хосте T:\...\media).
    src = spec.get("source") or settings.DEFAULT_SOURCE
    if not os.path.isabs(src):
        src = os.path.join(settings.MEDIA_ROOT, src)
    if not os.path.isfile(src):
        raise RuntimeError(f"source not found: {src}")

    enc_name = spec.get("encoder", "h264_nvenc")
    # опрос карт (несколько секунд на каждую отказавшую) — отдельная стадия,
    # иначе клиент видит "probe" и думает, что задача зависла
    self.update_state(state="PROGRESS", meta={"job_id": job_id, "progress": 0.01,
                                              "stage": "gpu-probe", "rungs": []})
    gpurep = _pick_gpu(enc_name)
    if gpurep and gpurep.get("picked") is None:
        raise RuntimeError("no GPU with working NVENC; use encoder=libx264")

    work = os.path.join(settings.WORK_ROOT, job_id)
    out_dir = os.path.join(settings.OUT_ROOT, job_id)
    os.makedirs(work, exist_ok=True)

    p = pipeline.Pipeline(src, work, wm=spec.get("watermark") or {})
    nchunks = int(spec.get("nchunks", 1))
    workers = int(spec.get("workers", 1))
    rungs = _subset(spec.get("rungs"))

    def prog(frac, stage, results=None, vmaf=None):
        self.update_state(state="PROGRESS", meta={
            "job_id": job_id,
            "progress": round(min(frac, 0.99), 3),
            "stage": stage,
            "rungs": _summ_rungs(results or [], vmaf),
        })

    def on_rung_done(done, total, done_results):
        # весь этап кодирования занимает 0.05..0.60: прогресс идёт по рангам,
        # иначе клиент минуты видит одну и ту же цифру
        prog(0.05 + 0.55 * done / max(total, 1), f"encode {done}/{total}", done_results)

    prog(0.02, "probe")

    cancelled = {"flag": False}

    def should_cancel():
        try:
            _check_cancel(self, job_id)
            return False
        except JobCancelled:
            cancelled["flag"] = True
            return True

    results, wall = p.run_ladder(
        encoder_name=enc_name,
        chunk_mode="chunked" if nchunks > 1 else "whole",
        nchunks=nchunks, workers=workers,
        tag="job", rungs=rungs, do_metrics=True,
        preset=spec.get("preset", "p5"),
        on_rung_done=on_rung_done, should_cancel=should_cancel,
    )
    def finish(pkg=None, vmaf=None, was_cancelled=False):
        """Один финальный отчёт для успешного и отменённого исходов.

        Отменённая задача != упавшая: готовые ранги сохраняются, статус CANCELLED,
        клиент решает, дожать их или выбросить.
        """
        report = {
            "job_id": job_id,
            "cancelled": was_cancelled,
            "meta": {
                "ffmpeg": ff.version(),
                "encoder": enc_name,
                "preset": spec.get("preset", "p5"),
                "chunked": [nchunks, workers] if nchunks > 1 else None,
                "source": p.src.summary(),
                "duration": p.src.duration,
                "gpu": gpurep,
                "watermark": spec.get("watermark"),
            },
            "encode": results,
            "ladder_wall_s": wall,
            "packaging": pkg,
            "vmaf": vmaf,
            "total_wall_s": round(time.perf_counter() - t0, 2),
        }
        pipeline.dump(os.path.join(work, "report.json"), report)
        art = {"report": os.path.join(work, "report.json")}
        if pkg:
            art.update({
                "hls_master": os.path.join(out_dir, "hls", "master.m3u8"),
                "dash_mpd": os.path.join(out_dir, "dash", "manifest.mpd"),
                "hls_variants": pkg["hls"].get("variants"),
                "segments": pkg["hls"].get("segments"),
            })
        return {
            "job_id": job_id,
            "cancelled": was_cancelled,
            "total_wall_s": report["total_wall_s"],
            "ladder_wall_s": wall,
            "rungs": _summ_rungs(results, vmaf),
            "artifacts": art,
            "gpu": gpurep,
        }

    results = [r for r in results if not r.get("cancelled")]
    if cancelled["flag"]:
        return finish(None, None, was_cancelled=True)
    _check_cancel(self, job_id)

    pkg = vmaf = None
    try:
        prog(0.65, "package", results)
        pkg = p.package(results, out_dir, seg_dur=float(spec.get("seg_dur", 4.0)))
        _check_cancel(self, job_id)

        vmaf = {}
        if spec.get("metrics", True):
            prog(0.75, "vmaf", results)
            try:
                vmaf = p.vmaf_for(results, tag="job_")
            except Exception as e:  # VMAF не должен ронять уже готовый результат
                vmaf = {"error": str(e)}
    except JobCancelled:
        # упаковка уже сделана -> отдаём её, просто помечая задачу отменённой
        return finish(pkg, vmaf, was_cancelled=True)

    return finish(pkg, vmaf, was_cancelled=False)


@app.task(name="vodpipe.ping")
def ping():
    return "pong"


if __name__ == "__main__":
    # На Windows prefork-пул Celery не работает (billiard не умеет fork),
    # поэтому пул потоковый: одна NVENC-задача на воркера всё равно concurrency=1.
    pool = "threads" if os.name == "nt" else "prefork"
    app.worker_main(["worker", "--loglevel=info", "--concurrency=1", "-P", pool])
