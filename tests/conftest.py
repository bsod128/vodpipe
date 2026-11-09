"""Shared test helpers.

The pipeline's logic is separable from ffmpeg, so most tests here never touch a
binary. The ones that do are marked and skip cleanly, which is what keeps CI
runnable on a GPU-less runner.
"""

from __future__ import annotations

import contextlib
import os
import shutil

import pytest

from vodpipe import ff

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ffmpeg_available() -> bool:
    try:
        ff.find("ffmpeg")
        return True
    except ff.FFmpegNotFound:
        return False


def ffmpeg_scratch_writable(path: str) -> bool:
    """Whether ffmpeg itself can write a file into `path`.

    A capability probe, like `ffmpeg_available`: tests that assert on what ffmpeg
    produced need both the binary and somewhere to put it. A sandbox that lets the
    Python process write a directory but denies the spawned ffmpeg is a real
    environment here, and it is not a check that failed.
    """
    probe = os.path.join(path, "vodpipe-probe.ts")
    try:
        ff.Runner(timeout_s=60).run(
            [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.04", "-frames:v", "1",
             probe], check=True)
        return True
    except Exception:
        return False
    finally:
        with contextlib.suppress(OSError):
            os.remove(probe)


requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="no ffmpeg (put it in ./bin or set VODPIPE_FFMPEG)")

requires_gpu = pytest.mark.skipif(
    os.environ.get("VODPIPE_GPU_TESTS") != "1",
    reason="set VODPIPE_GPU_TESTS=1 to run NVENC probes")


def tmp_path_of(tmp_path) -> str:
    return str(tmp_path)


def make_fake_runner(payloads: dict[str, tuple[int, str, str]]):
    """A Runner whose `execute` returns canned results.

    Keyed by a substring of argv so a test can assert both what was decided and
    what the caller does with the result - without ffmpeg present.
    """
    calls: list[list[str]] = []

    def execute(argv):
        calls.append(list(argv))
        for key, (code, out, err) in payloads.items():
            if key in " ".join(argv):
                return _Completed(code, out, err)
        return _Completed(0, "", "")

    return ff.Runner(execute=execute), calls


class _Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def free_gb_at(path: str) -> float:
    drive = os.path.splitdrive(os.path.abspath(path))[0] or "/"
    return shutil.disk_usage(drive).free / 1e9
