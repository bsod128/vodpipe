"""API contracts as pydantic schemas.

Validation belongs here rather than scattered through the handlers: an
unknown encoder or rung is rejected with a 422 that names the allowed values,
before a worker is ever occupied.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

# Names come from vodpipe.encoders / vodpipe.ladder so the API cannot drift
# away from what the pipeline actually implements.
from vodpipe.encoders import BACKENDS, PRESET_DEFAULTS, validate_preset
from vodpipe.ladder import ladder
from vodpipe.watermark import is_safe_id

ENCODERS = tuple(sorted(BACKENDS))
LADDER = tuple(r.label for r in ladder())
ENCODER_PRESET_HELP = ", ".join(f"{name} -> {p}" for name, p in sorted(PRESET_DEFAULTS.items()))


class WatermarkIn(BaseModel):
    static: str | None = Field(None, description="Constant overlay (rights holder)")
    dynamic: str | None = Field(None, description="Per-session / per-viewer id")
    ghost: bool = Field(False, description="Near-invisible layer, survives cropping")

    @field_validator("static", "dynamic")
    @classmethod
    def _watermark_text_is_constrained(cls, value: str | None) -> str | None:
        """Watermark text is client input that ends up inside a filtergraph.

        `watermark.escape()` is what keeps the graph parseable; this allow-list
        is the second line of defence, so a client cannot push an arbitrarily
        long or exotic expression into the encoder command line.
        """
        if value is not None and not is_safe_id(value):
            raise ValueError(
                "watermark text must be 1-64 characters of A-Za-z0-9 . _ : @ # space -")
        return value


class JobIn(BaseModel):
    source: str | None = Field(None, description="Source path inside the media root")
    encoder: str = Field("h264_nvenc")
    preset: str | None = Field(
        None, description=f"Encoder preset; per-backend default ({ENCODER_PRESET_HELP})")
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

    @model_validator(mode="after")
    def _preset_valid_for_encoder(self) -> JobIn:
        """A preset valid for one backend can be fatal for another.

        Catching it here returns a 422 instead of accepting a job the worker
        then fails on after it has been queued (the GPU-absent fallback hit
        exactly this when a global default of "p5" reached libx264).
        """
        self.preset = validate_preset(self.encoder, self.preset)
        return self

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
