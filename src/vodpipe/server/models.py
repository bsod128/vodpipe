"""API contracts as pydantic schemas.

Validation belongs here rather than scattered through the handlers: an
unknown encoder or rung is rejected with a 422 that names the allowed values,
before a worker is ever occupied.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# Names come from vodpipe.encoders / vodpipe.ladder so the API cannot drift
# away from what the pipeline actually implements.
from ..encoders import BACKENDS
from ..ladder import ladder

ENCODERS = tuple(sorted(BACKENDS))
LADDER = tuple(r.label for r in ladder())


class WatermarkIn(BaseModel):
    static: str | None = Field(None, description="Constant overlay (rights holder)")
    dynamic: str | None = Field(None, description="Per-session / per-viewer id")
    ghost: bool = Field(False, description="Near-invisible layer, survives cropping")


class JobIn(BaseModel):
    source: str | None = Field(None, description="Source path inside the media root")
    encoder: str = Field("h264_nvenc")
    preset: str = Field("p5", description="p1..p7 for NVENC, x264 preset names for libx264")
    nchunks: int = Field(1, ge=1, le=64,
                         description="Parallel chunks; >1 only helps across GPUs")
    workers: int = Field(1, ge=1, le=16)
    rungs: list[str] | None = Field(None, description="Subset of the ladder to encode")
    seg_dur: float = Field(4.0, gt=0.5, le=12.0, description="Segment length in seconds")
    metrics: bool = Field(True, description="Measure VMAF per rung")
    watermark: WatermarkIn | None = None

    @field_validator("encoder")
    @classmethod
    def _encoder_known(cls, value: str) -> str:
        if value not in ENCODERS:
            raise ValueError(f"encoder must be one of: {', '.join(ENCODERS)}")
        return value

    @field_validator("rungs")
    @classmethod
    def _rungs_known(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        bad = [r for r in value if r not in LADDER]
        if bad:
            raise ValueError(f"unknown rungs {bad}; allowed: {', '.join(LADDER)}")
        return value

    @field_validator("source")
    @classmethod
    def _no_traversal(cls, value: str | None) -> str | None:
        if value and ".." in value:
            raise ValueError("path traversal not allowed")
        return value


class RungOut(BaseModel):
    rung: str | None = None
    encoder: str | None = None
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
