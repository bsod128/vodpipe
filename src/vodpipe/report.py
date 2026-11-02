"""Building and storing the JSON artifacts in bench/.

The report is the deliverable of this repo: every claim in the README points
at a file here. Two rules keep it usable by anyone who clones the repo:

* paths are written repo-relative, never with a machine-specific root;
* the schema is stable, so figures.py can read older artifacts.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import ff

REDACTED = ff.LOCAL_PATH  # same placeholder the scrubber writes


def build_report(job_id: str, meta: dict, encode: list[dict], ladder_wall_s: float,
                 packaging: dict, vmaf: dict, verify: list[dict],
                 total_wall_s: float) -> dict:
    """One artifact shape for the CLI, the benchmark and the queue worker.

    `encode` rows already carry per-rung numbers; the rest is context needed
    to interpret them (which ffmpeg, which GPU, what the source was).
    """
    return {
        "job_id": job_id,
        "meta": meta,
        "encode": encode,
        "ladder_wall_s": ladder_wall_s,
        "packaging": packaging,
        "vmaf": vmaf,
        "verify": verify,
        "total_wall_s": total_wall_s,
    }


def _inside_repo(path, root: str) -> str | None:
    """Path relative to the repo when it lives inside it, else None."""
    text = ff.clean_path(path)
    base = ff.clean_path(root).rstrip("/")
    if base and text.lower().startswith(base.lower() + "/"):
        return text[len(base) + 1:]
    return None


def to_repo_relative(path, root: str) -> str:
    """Repo-relative forward-slashed path for display and for JSON."""
    return _inside_repo(path, root) or ff.clean_path(path)


def sanitize(obj: Any, root: str) -> Any:
    """Copy of `obj` with in-repo paths made repo-relative and everything else
    absolute replaced by a placeholder.

    Applied only on the way out. argv must keep real absolute paths, or ffmpeg
    would not find its files.
    """
    if isinstance(obj, str):
        rel = _inside_repo(obj, root)
        return rel if rel is not None else ff.scrub(obj)
    if isinstance(obj, dict):
        return {k: sanitize(v, root) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v, root) for v in obj]
    return obj


def dump(path, obj: Any) -> str:
    """Write JSON with a trailing newline so git diffs stay clean."""
    directory = os.path.dirname(str(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return str(path)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def ladder_table(results: list[dict]) -> str:
    """Markdown table of a ladder run, for the README and for a report."""
    rows = ["| rung | size | mode | speed | wall s | target kbps | measured kbps "
            "| VMAF mean | VMAF min |",
            "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if r.get("cancelled"):
            rows.append(f"| {r['rung']} | - | cancelled | - | - | - | - | - | - |")
            continue
        m = r.get("measured") or {}
        v = r.get("vmaf") or {}
        rows.append(
            f"| {r['rung']} | {r.get('rung_res', '-')} | {r['mode']} "
            f"| {r['speed_x']:.2f} | {r['wall_s']:.1f} "
            f"| {r.get('target_kbps', '-')} | {m.get('video_kbps', '-')} "
            f"| {v.get('vmaf_mean', '-')} | {v.get('vmaf_min', '-')} |")
    return "\n".join(rows)

