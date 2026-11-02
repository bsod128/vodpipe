# -*- coding: utf-8 -*-
"""Контракты API: pydantic-схемы вместо свободных dict в теле запроса."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

ENCODERS = ("h264_nvenc", "hevc_nvenc", "h264_qsv", "libx264")
LADDER = ("2160p", "1440p", "1080p", "720p", "432p")


class WatermarkIn(BaseModel):
    static: str | None = Field(None, description="Постоянная строка (правообладатель)")
    dynamic: str | None = Field(None, description="Per-stream id / id сессии")
    ghost: bool = Field(False, description="Полупрозрачный маркер, заметный при вырезке кадра")


class JobIn(BaseModel):
    source: str | None = Field(None, description="Путь к исходнику внутри медиа-корня")
    encoder: str = Field("h264_nvenc")
    preset: str = Field("p5", description="p1..p7 для NVENC, имена x264 для libx264")
    nchunks: int = Field(1, ge=1, le=64)
    workers: int = Field(1, ge=1, le=16)
    rungs: list[str] | None = Field(None, description="Подмножество лестницы")
    seg_dur: float = Field(4.0, gt=0.5, le=12.0)
    metrics: bool = True
    watermark: WatermarkIn | None = None

    @field_validator("encoder")
    @classmethod
    def _enc(cls, v: str) -> str:
        if v not in ENCODERS:
            raise ValueError(f"encoder must be one of {', '.join(ENCODERS)}")
        return v

    @field_validator("rungs")
    @classmethod
    def _rungs(cls, v):
        if v is None:
            return v
        bad = [r for r in v if r not in LADDER]
        if bad:
            raise ValueError(f"unknown rungs: {bad}; allowed: {', '.join(LADDER)}")
        return v

    @field_validator("source")
    @classmethod
    def _src(cls, v: str | None) -> str | None:
        if v and ".." in v:
            raise ValueError("path traversal not allowed")
        return v


class RungOut(BaseModel):
    rung: str
    encoder: str
    mode: str | None = None
    wall_s: float | None = None
    speed_x: float | None = None
    measured_kbps: float | None = None
    vmaf: float | None = None


class JobOut(BaseModel):
    job_id: str
    state: str = Field(description="PENDING|RUNNING|SUCCESS|FAILURE|CANCELLED")
    progress: float = 0.0
    stage: str | None = None
    error: str | None = None
    cancelled: bool = False
    rungs: list[RungOut] | None = None
    artifacts: dict | None = None
    result: dict | None = None


class HealthOut(BaseModel):
    api: str
    broker: str
    backend: str
    broker_ok: bool
    backend_ok: bool
    queue_depth: int | None = None
