r"""ffmpeg/ffprobe process boundary.

Three ideas carry this module:

1. **Binary discovery is explicit, never a hardcoded machine path.** Order:
   `VODPIPE_FFMPEG`/`VODPIPE_FFPROBE` -> repo `bin/` -> `PATH`. A pipeline that
   remembers `T:\some\path\ffmpeg.exe` cannot be cloned and run by anyone else.
2. **Build and driver are coupled.** A newer ffmpeg can be *worse*: NVENC API
   newer than the installed driver fails with "Required: 13.1 Found: 13.0". So
   the version is reported in every run, and option spellings are resolved at
   runtime rather than assumed (see `flag()`).
3. **Run and decide are separated.** `*_args()` functions are pure and unit
   tested; `Runner.run()` is the only code that calls subprocess.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))  # src/vodpipe -> repo root

# Only repo-relative candidates. `bin/` is created by scripts/fetch-ffmpeg.
_LOCAL_DIRS = [os.path.join(_REPO_ROOT, "bin")]


class FFmpegNotFound(FileNotFoundError):
    """No usable ffmpeg/ffprobe binary was found."""


class FFmpegError(RuntimeError):
    """ffmpeg exited non-zero. Keeps stderr so the caller can report the cause."""

    def __init__(self, cmd: Sequence[str], stderr: str, returncode: int):
        self.cmd = list(cmd)
        self.stderr = stderr or ""
        self.returncode = returncode
        shown = " ".join(str(c) for c in cmd[-12:])
        super().__init__(f"ffmpeg exited {returncode}: {shown}\n{self.stderr.strip()[:800]}")


def find(name: str) -> str:
    """Resolve an ffmpeg tool name to an executable path.

    Environment override wins, then the repo's `bin/`, then PATH. Names are
    compared without extension: on Windows `ffprobe.exe` must still match a
    request for `ffprobe` (a bare `basename == name` compare silently fails
    there, which once made every probe use the wrong binary).
    """
    env = os.environ.get("VODPIPE_" + name.upper())
    if env:
        if os.path.exists(env):
            return env
        raise FFmpegNotFound(f"VODPIPE_{name.upper()}={env!r} does not exist")

    for d in _LOCAL_DIRS:
        for cand in (name, name + ".exe"):
            local = os.path.join(d, cand)
            if os.path.isfile(local):
                return local

    for cand in (name, name + ".exe"):
        found = shutil.which(cand)
        if found:
            return found

    raise FFmpegNotFound(
        f"{name} not found. Install ffmpeg, drop it in ./bin, or set VODPIPE_{name.upper()}."
    )


@dataclass(frozen=True)
class Result:
    stdout: str
    stderr: str
    returncode: int
    wall_s: float
    speed_x: float = 0.0


@dataclass
class Runner:
    """Runs ffmpeg/ffprobe.

    Injectable on purpose: unit tests pass a fake `execute`, and the API worker
    can pass a hook for logging/cancel checks without touching this module.
    """

    execute: Callable[[list[str]], Any] | None = None
    """callable(argv) -> CompletedProcess-like; None means subprocess.run."""

    on_start: Callable[[list[str]], None] | None = None
    """callable(argv) -> None; used for logging or a cancel check before spawn."""

    def _default_execute(self, argv: list[str]):
        return subprocess.run(argv, capture_output=True, text=True, errors="replace")

    def run(self, argv: Sequence[str], check: bool = True) -> Result:
        argv = [str(a) for a in argv]
        if self.on_start:
            self.on_start(argv)
        execute = self.execute or self._default_execute
        t0 = time.perf_counter()
        proc = execute(argv)
        wall = time.perf_counter() - t0
        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""
        code = int(getattr(proc, "returncode", 0) or 0)
        if check and code != 0:
            raise FFError(argv, stderr, code)
        return Result(stdout=stdout, stderr=stderr, returncode=code, wall_s=wall,
                      speed_x=last_speed(stderr))


# Historical name kept short: the whole codebase raises/quotes FFError.
FFError = FFmpegError

_DEFAULT = Runner()


def run(argv: Sequence[str], check: bool = True) -> Result:
    return _DEFAULT.run(argv, check=check)


# --------------------------------------------------------------------------
# parsing helpers (pure)
# --------------------------------------------------------------------------


def as_int(value, default: int = 0) -> int:
    """JSON field -> int, tolerating what ffprobe actually emits.

    ffprobe writes the literal string "N/A" for an unavailable value (always
    for a video bit_rate inside MPEG-TS) and sometimes `null`. `int(x or 0)`
    raises on "N/A", which once killed a whole job in the metrics loop.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_SPEED_RE = re.compile(r"speed=\s*([0-9.]+)x")


def last_speed(stderr: str) -> float:
    """Throughput from ffmpeg's own `-stats` line, last one wins.

    This is ffmpeg's processed-realtimes-per-wall-second, which is the number
    people mean by "how fast does it encode"; wall time of the process is
    reported separately because it also includes spawn and mux overhead.
    """
    matches = _SPEED_RE.findall(stderr or "")
    return float(matches[-1]) if matches else 0.0


_ENCODER_LINE_RE = re.compile(r"^\s*[VAS]\S{5}\s+(\S+)", re.M)
# The filter flag column is 3 chars in ffmpeg 7.x (` T.C drawtext`, ` ... libvmaf`)
# and was 6 in older builds, so the width is a range rather than a constant.
_FILTER_LINE_RE = re.compile(r"^\s*[A-Z.]{3,6}\s+(\S+)\s", re.M)


def parse_encoder_names(dump: str) -> set[str]:
    r"""Names from `ffmpeg -encoders`.

    The flag column is six characters wide (` V....D h264_nvenc`), so the type
    letter must be followed by five more non-space chars. A pattern like
    `V[.\s].{3}` silently misses every hardware encoder.
    """
    return set(_ENCODER_LINE_RE.findall(dump or ""))


def parse_filter_names(dump: str) -> set[str]:
    return set(_FILTER_LINE_RE.findall(dump or ""))


def has_encoder(name: str, runner: Runner | None = None) -> bool:
    out = (runner or _DEFAULT).run([ffmpeg(), "-hide_banner", "-encoders"]).stdout
    return name in parse_encoder_names(out)


def has_filter(name: str, runner: Runner | None = None) -> bool:
    out = (runner or _DEFAULT).run([ffmpeg(), "-hide_banner", "-filters"]).stdout
    return name in parse_filter_names(out)


def version(runner: Runner | None = None) -> str:
    out = (runner or _DEFAULT).run([ffmpeg(), "-hide_banner", "-version"]).stdout
    return out.splitlines()[0] if out.strip() else "unknown"


def version_args() -> list[str]:
    return [ffmpeg(), "-hide_banner", "-version"]


# --------------------------------------------------------------------------
# option spelling, resolved at runtime (pure)
# --------------------------------------------------------------------------

_HELP_OPT_RE = re.compile(r"^\s+-\s*([A-Za-z0-9_.-]+)", re.M)


def parse_help_options(help_text: str) -> set[str]:
    """Option names accepted by this build, from `ffmpeg -h encoder=<codec>`."""
    return set(_HELP_OPT_RE.findall(help_text or ""))


def pick_option(help_text: str, *candidates: str) -> str | None:
    """First spelling this build accepts.

    Builds disagree: `h264_nvenc` accepts `-spatial_aq` in one build and
    `-spatial-aq` in another. Guessing wrong does not degrade quietly, it kills
    the run with "Unrecognized option", so we read the help text instead.
    """
    known = parse_help_options(help_text)
    for cand in candidates:
        if cand in known:
            return "-" + cand
    return None


class EncoderHelp:
    """Small cache so we pay for `ffmpeg -h encoder=...` once per codec."""

    def __init__(self, runner: Runner | None = None):
        self._runner = runner or _DEFAULT
        self._cache: dict[str, str] = {}

    def text(self, codec: str) -> str:
        if codec not in self._cache:
            self._cache[codec] = self._runner.run(
                [ffmpeg(), "-hide_banner", "-h", f"encoder={codec}"], check=False
            ).stdout
        return self._cache[codec]

    def flag(self, codec: str, *candidates: str) -> list[str]:
        picked = pick_option(self.text(codec), *candidates)
        return [picked] if picked else []


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------


def probe_args(path: str, entries: Sequence[str], select: str | None = None,
               count_packets: bool = False, out_format: str = "json") -> list[str]:
    args = [ffprobe(), "-v", "error", "-print_format", out_format]
    if select:
        args += ["-select_streams", select]
    if count_packets:
        args += ["-count_packets"]
    for e in entries:
        args += ["-show_entries", e]
    args.append(path)
    return args


def probe_json(path: str, entries: Sequence[str], select: str | None = None,
               count_packets: bool = False, runner: Runner | None = None) -> dict:
    argv = probe_args(path, entries, select=select, count_packets=count_packets)
    out = (runner or _DEFAULT).run(argv).stdout
    try:
        return json.loads(out or "{}")
    except json.JSONDecodeError as exc:  # keep the argv in the message
        raise FFError(argv, f"invalid ffprobe json: {exc}", 0) from exc


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------


@dataclass
class Health:
    ffmpeg: str
    version: str
    encoders: dict[str, bool] = field(default_factory=dict)
    filters: dict[str, bool] = field(default_factory=dict)
    detail: str = ""

    def ok_for(self, codec: str) -> bool:
        return self.encoders.get(codec, False)


REQUIRED_FILTERS = ("libvmaf", "drawtext", "scale", "setsar", "overlay", "ssim", "psnr")


def health(check_filters: Sequence[str] = REQUIRED_FILTERS) -> Health:
    try:
        ver = version()
    except FFmpegNotFound as exc:
        return Health(ffmpeg="missing", version="?", detail=str(exc))
    enc_names = parse_encoder_names(run([ffmpeg(), "-hide_banner", "-encoders"]).stdout)
    flt_names = parse_filter_names(run([ffmpeg(), "-hide_banner", "-filters"]).stdout)
    return Health(
        ffmpeg=ffmpeg(),
        version=ver,
        encoders={c: c in enc_names for c in ("h264_nvenc", "hevc_nvenc", "h264_qsv",
                                              "libx264")},
        filters={f: f in flt_names for f in check_filters},
    )


def clean_path(path) -> str:
    """Forward slashes. ffmpeg's arg/filtergraph parsers treat `\\` in Windows
    paths as an escape character, so a path is normalised on the way in.

    Also used when serialising reports, which keeps committed JSON stable
    across machines.
    """
    return str(path).replace("\\", "/")


LOCAL_PATH = "<" + "local-path" + ">"

_DRIVE = re.compile(r"[A-Za-z]:[\\/][^\s\"',;)\]}]*")
# `~/anything`, or an absolute path under a known filesystem root. Deliberately
# not "any leading slash": a repo-relative `./out/x.m3u8` is meaningful to the
# reader and must survive.
_UNIX = re.compile(r"(?<![\w/])("
                   r"~[\\/][^\s\"',;)\]}]*"
                   r"|/(?:home|mnt|srv|opt|Users|var|usr|etc|root)[\\/][^\s\"',;)\]}]*"
                   r")")


def scrub(text: str) -> str:
    """Replace machine-specific absolute paths in log or error text.

    Only for display and for what we commit: the original text is never
    rewritten before it reaches ffmpeg.
    """
    out = clean_path(text)
    out = _DRIVE.sub(LOCAL_PATH, out)
    return _UNIX.sub(LOCAL_PATH, out)


def ffmpeg() -> str:
    return find("ffmpeg")


def ffprobe() -> str:
    return find("ffprobe")

