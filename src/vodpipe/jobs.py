"""Job-level logic shared by the HTTP API and the queue worker (no web deps).

Kept free of FastAPI and Celery imports on purpose: this is where the two
subtle queue rules live, and they must be unit-testable without a broker.
Only `vodpipe.ff` (stdlib under the hood) is pulled in, to reuse its path
scrubbing on the one payload that quotes worker exceptions back to clients.

1. A cancelled job is SUCCESS to Celery - the task returned a partial result
   on purpose - but it is a distinct terminal state for the client. The flag
   lives in the payload, not in the Celery state.
2. Progress is read from the same result backend that holds the status, so
   there is no second source of truth that could disagree with it.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit, urlunsplit

from .ff import scrub
from .ladder import select as ladder_select
from .mediarange import is_inside
from .report import RESULT_KEYS

CANCEL_KEY_PREFIX = "vodpipe:cancel:"
CANCEL_TTL_S = 3600

#: ids are `uuid4().hex[:12]`; anything else was not issued by this service.
JOB_ID_RE = re.compile(r"\A[0-9a-f]{12}\Z")


def cancel_key(job_id: str) -> str:
    """Redis key holding the cooperative cancel flag for a job."""
    return CANCEL_KEY_PREFIX + job_id


def validate_job_id(job_id: str) -> str:
    """Return the id unchanged if this service could have issued it, else raise.

    Starlette hands over the *decoded* path segment, so `%2e%2e` arrives as `..`
    and `%5c` as a separator. Any route that puts the id into a filesystem path
    or a Redis key needs this gate first; containment checks alone are not enough
    (an absolute segment makes a path trivially "inside" itself).
    """
    if not JOB_ID_RE.match(job_id):
        raise ValueError("malformed job id")
    return job_id


def job_dir(job_id: str, root: str) -> str:
    """Resolve a job's directory under `root`, refusing any escape from it.

    Validating the id is what makes this safe, not the containment check that
    follows. `os.path.join` walks out of `root` before `is_inside` is ever
    consulted, and an absolute segment (a drive letter plus a backslash) makes
    base and candidate identical, so a bare `is_inside(path, base)` check is
    satisfied by *any* file on the machine. So: reject ids this service never
    issued, then re-check containment against the *root* - not against a value
    derived from the same untrusted input.
    """
    validate_job_id(job_id)
    root_n = os.path.normpath(root)
    base = os.path.normpath(os.path.join(root_n, job_id))
    if not is_inside(base, root_n):
        raise ValueError("job path escapes the root")
    return base


def snapshot(state: str, info: dict | None) -> dict:
    """Celery state + stored payload -> the view the client polls.

    `info` is AsyncResult.info: the progress meta while running, the returned
    dict on success, the exception text on failure.
    """
    info = info or {}
    out = {"state": state, "progress": 0.0, "stage": None, "error": None,
           "cancelled": False, "rungs": None, "artifacts": None, "result": None}

    if state == "PROGRESS":
        out["state"] = "RUNNING"
        out["progress"] = float(info.get("progress", 0.0))
        out["stage"] = info.get("stage")
        out["rungs"] = info.get("rungs")
    elif state == "SUCCESS":
        out["progress"] = 1.0
        out["cancelled"] = bool(info.get("cancelled"))
        out["stage"] = "cancelled" if out["cancelled"] else "done"
        if out["cancelled"]:
            out["state"] = "CANCELLED"
        out["rungs"] = info.get("rungs")
        out["artifacts"] = info.get("artifacts")
        out["result"] = {k: info.get(k) for k in RESULT_KEYS}
    elif state == "REVOKED":
        # Only reachable if someone revoked outside this codebase; still map it.
        out["state"] = "CANCELLED"
        out["cancelled"] = True
    elif state == "FAILURE":
        # Worker exceptions quote absolute host paths (and sometimes the broker
        # URL). This payload is returned verbatim to anyone polling the job, so
        # it goes through the same scrubber the committed reports use.
        out["error"] = scrub(str(info))
    return out


def resolve_source(source: str | None, default: str, media_root: str) -> str:
    """Normalize a requested source to a path relative to the media root.

    The worker may live in a container where the same file has a different
    absolute path, so the job spec carries a root-relative path. Anything
    outside the root (including `..` tricks) is refused here, once, rather
    than being checked again in three places.
    """
    src = source or default
    if not os.path.isabs(src):
        src = os.path.join(media_root, src)
    src = os.path.normpath(src)
    root = os.path.normpath(media_root)
    if os.path.commonpath([src, root]) != root:
        raise ValueError("source must live inside the media root")
    return os.path.relpath(src, root)


def rung_subset(labels: list[str] | None, rungs) -> list | None:
    """Filter a ladder by label, keeping the tallest-first order.

    Returns None for "use the default ladder" so the caller keeps its own
    default instead of an empty list (an empty ladder silently does nothing).
    Lenient on purpose: the API already rejected unknown labels in `JobIn`, so
    by the time a spec reaches the worker an unmatched label is not a client
    error to surface, and the default ladder is the safe fallback.
    """
    return ladder_select(labels, rungs, strict=False) or None


def rung_summary(results: list[dict], vmaf: dict | None = None) -> list[dict]:
    """Compact per-rung rows for the client (progress payloads stay small)."""
    rows = []
    for r in results:
        measured = r.get("measured") or {}
        row = {
            "rung": r.get("rung"),
            "encoder": r.get("encoder"),
            "mode": r.get("mode"),
            "wall_s": r.get("wall_s"),
            "speed_x": r.get("speed_x"),
            "measured_kbps": measured.get("video_kbps") or measured.get("bitrate_kbps"),
        }
        if r.get("cancelled"):
            row["mode"] = "cancelled"
        if vmaf:
            row["vmaf"] = (vmaf.get(r.get("rung")) or {}).get("vmaf_mean")
        rows.append(row)
    return rows


def redact_url(url: str) -> str:
    """Broker/backend URL with the password removed, for anything a client sees.

    /health used to echo the configured URL verbatim, which in a public repo or
    a shared dashboard hands out the broker credentials. Host and port are kept:
    they are what an operator actually needs to diagnose a connection problem.
    """
    parts = urlsplit(url)
    if not parts.netloc or "@" not in parts.netloc:
        return url
    user = parts.username or ""
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, f"{user}@{host}", parts.path, parts.query,
                       parts.fragment))
