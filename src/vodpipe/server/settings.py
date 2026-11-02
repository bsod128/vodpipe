"""Server settings: one place, all read from the environment.

The container and the host worker point at the same broker through these
variables, so the same code runs in both without a config file. Paths are
resolved against a media root: a job spec carries root-relative paths, which
is what makes a host worker and a container worker interchangeable.
"""

from __future__ import annotations

import os

# Repo root: .../src/vodpipe/server -> up four levels.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

MEDIA_ROOT = os.environ.get("VODPIPE_MEDIA", _ROOT)
WORK_ROOT = os.environ.get("VODPIPE_WORK", os.path.join(MEDIA_ROOT, "work", "jobs"))
OUT_ROOT = os.environ.get("VODPIPE_OUT", os.path.join(MEDIA_ROOT, "out", "jobs"))

BROKER_URL = os.environ.get("VODPIPE_BROKER", "amqp://vodpipe:vodpipe-dev@localhost:5672/")
RESULT_BACKEND = os.environ.get("VODPIPE_BACKEND", "redis://localhost:6379/0")
# Progress and cancel flags live in the result backend: one source of truth.
STATE_URL = os.environ.get("VODPIPE_STATE", RESULT_BACKEND)

DEFAULT_SOURCE = os.environ.get("VODPIPE_SRC",
                                os.path.join(MEDIA_ROOT, "src_clip", "4k_master.mp4"))

# GPU index is not hardcoded anywhere: it changes with the machine's GPU order.
GPU_AUTO = os.environ.get("VODPIPE_GPU_AUTO", "1") != "0"

PORT = int(os.environ.get("VODPIPE_PORT", "8000"))
