"""Shared test helpers.

The pipeline's logic is separable from ffmpeg, so most tests here never touch a
binary. The ones that do are marked and skip cleanly, which is what keeps CI
runnable on a GPU-less runner.
"""

from __future__ import annotations

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
