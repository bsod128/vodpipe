"""HTTP range semantics and media MIME types for segment delivery.

A VOD player does not download a file: it asks for byte ranges of a segment while
the user scrubs, and it needs the right Content-Type or it refuses to parse the
playlist. Both behaviours are protocol-level and easy to get subtly wrong, so
they live here as pure functions with tests rather than inline in a route.

Follows RFC 9110 section 14.2 (Range) single-range semantics: a range whose unit
we do not implement, or which we cannot parse, is ignored (the whole body is
served); 416 is reserved for a well-formed `bytes=` range that cannot be satisfied.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

#: The extensions a player will actually request. `.m4s` must be served as ISO
#: media, not octet-stream: some Smart TV stacks refuse to parse fMP4 otherwise.
MEDIA_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m3u": "application/vnd.apple.mpegurl",
    ".mpd": "application/dash+xml",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".init": "video/mp4",
    ".cmfv": "video/mp4",
    ".cmfa": "audio/mp4",
    ".ts": "video/mp2t",
    ".json": "application/json",
}


def media_type(path: str) -> str:
    return MEDIA_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int  # inclusive, HTTP style
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total}"

    def content_headers(self) -> dict[str, str]:
        """Headers a 206 must carry. Omitting Content-Range makes clients that
        already have part of the file write the fragment to the wrong offset."""
        return {"content-range": self.content_range(), "accept-ranges": "bytes"}


class RangeNotSatisfiable(Exception):
    """Maps to HTTP 416 with a `Content-Range: bytes */total` header."""

    def __init__(self, total: int, reason: str = ""):
        self.total = total
        self.reason = reason
        super().__init__(reason or f"range not satisfiable for {total} bytes")

    @property
    def headers(self) -> dict[str, str]:
        return {"content-range": f"bytes */{self.total}", "accept-ranges": "bytes"}


def parse_range(header: str | None, total: int) -> ByteRange | None:
    """`Range` header -> resolved byte range, or None for a full read.

    Player-shaped cases:
      * `bytes=0-`     - whole file (the common first request);
      * `bytes=1000-`  - resume from an offset;
      * `bytes=-500`   - suffix range: the LAST 500 bytes.

    What we do with anything else is decided by RFC 9110, which is stricter than
    intuition. "A server MAY ignore the Range header field" (14.2), and
    "An origin server MUST ignore a Range header field that contains a range unit
    it does not understand" (14.2.1), while an invalid ranges-specifier is
    "MAY ignore or reject" (14.2.2). 416 is reserved for a *valid* `bytes=`
    specifier that cannot be satisfied. So:
      * `items=0-1`, `bytes=abc-def`, multi-range -> None: ignore, answer 200.
        Starlette's own FileResponse answers these with 400 text/plain; that is a
        defensible reading of "MAY reject", but "MUST ignore" for an unknown unit
        is the stronger rule, so we parse here instead of delegating.
      * `bytes=5000-` against a 1 kB file -> RangeNotSatisfiable -> 416.
    """
    if not header:
        return None
    header = header.strip()
    if "," in header:
        return None

    m = _RANGE_RE.match(header)
    if not m:
        return None
    if total <= 0:
        raise RangeNotSatisfiable(total, "empty file")
    raw_start, raw_end = m.group(1), m.group(2)

    if not raw_start and not raw_end:
        raise RangeNotSatisfiable(total, f"empty range: {header!r}")

    if not raw_start:
        # suffix range: last N bytes
        suffix = int(raw_end)
        if suffix == 0:
            raise RangeNotSatisfiable(total, "zero-length suffix range")
        start = max(0, total - suffix)
        end = total - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else total - 1
        if start > end:
            raise RangeNotSatisfiable(total, f"start after end: {header!r}")
        if start >= total:
            raise RangeNotSatisfiable(total, f"start past end of file: {header!r}")
        end = min(end, total - 1)  # clamp, per RFC: a longer end is not an error

    return ByteRange(start=start, end=end, total=total)


def is_inside(child: str, root: str) -> bool:
    """Containment check for path traversal (`..`, absolute paths, other drives).

    `commonpath` raises ValueError across drives on Windows, which is itself a
    "not inside" answer - an attacker escaping via `C:` should get a 404, not a
    500.
    """
    child_n, root_n = os.path.normpath(child), os.path.normpath(root)
    try:
        return os.path.commonpath([child_n, root_n]) == root_n
    except ValueError:
        return False
