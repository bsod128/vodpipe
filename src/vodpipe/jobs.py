"""Job-level logic shared by the HTTP API and the queue worker (stdlib only).

Kept free of FastAPI and Celery imports on purpose: this is where the two
subtle queue rules live, and they must be unit-testable without a broker.

1. A cancelled job is SUCCESS to Celery - the task returned a partial result
   on purpose - but it is a distinct terminal state for the client. The flag
   lives in the payload, not in the Celery state.
2. Progress is read from the same result backend that holds the status, so
   there is no second source of truth that could disagree with it.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

CANCEL_KEY_PREFIX = "vodpipe:cancel:"
CANCEL_TTL_S = 3600

# Client-visible states. PENDING/RUNNING are non-terminal.
STATES = ("PENDING", "RUNNING", "SUCCESS", "FAILURE", "CANCELLED")


def cancel_key(job_id: str) -> str:
    """Redis key holding the cooperative cancel flag for a job."""
    return CANCEL_KEY_PREFIX + job_id


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
        out["result"] = {k: info.get(k)
                        for k in ("total_wall_s", "ladder_wall_s", "gpu")}
    elif state == "REVOKED":
        # Only reachable if someone revoked outside this codebase; still map it.
        out["state"] = "CANCELLED"
        out["cancelled"] = True
    elif state == "FAILURE":
        out["error"] = str(info)
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
    """
    if not labels:
        return None
    wanted = set(labels)
    picked = [r for r in rungs if r.label in wanted]
    picked.sort(key=lambda r: r.height, reverse=True)
    return picked or None


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
