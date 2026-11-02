"""HTTP range semantics and media MIME types for segment delivery.

A VOD player does not download a file: it asks for byte ranges of a segment while
the user scrubs, and it needs the right Content-Type or it refuses to parse the
playlist. Both behaviours are protocol-level and easy to get subtly wrong, so
they live here as pure functions with tests rather than inline in a route.

Follows RFC 7233 single-range semantics.
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

    @property
    def is_full(self) -> bool:
        return self.start == 0 and self.end == self.total - 1

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

    Cases that matter for players:
      * `bytes=0-`          - whole file (the common first request);
      * `bytes=1000-`       - resume from an offset;
      * `bytes=-500`        - suffix range: the LAST 500 bytes;
      * `bytes=0-99,200-299`- multi-range: not supported, answered as 416 rather
        than silently serving the first range (a wrong answer is worse than an
        error the client will retry as a single range).
    """
    if not header:
        return None
    header = header.strip()
    if "," in header:
        raise RangeNotSatisfiable(total, "multi-range requests are not supported")

    m = _RANGE_RE.match(header)
    if not m or total <= 0:
        raise RangeNotSatisfiable(total, f"unparsable range: {header!r}")
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
