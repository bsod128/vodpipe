"""HTTP range semantics used to deliver segments (RFC 7233 single-range)."""

from __future__ import annotations

import pytest

from vodpipe import mediarange as mr


def test_no_header_means_full_read():
    assert mr.parse_range(None, 1000) is None


def test_open_ended_range_covers_the_rest():
    r = mr.parse_range("bytes=500-", 1000)
    assert (r.start, r.end, r.length) == (500, 999, 500)


def test_suffix_range_is_the_last_n_bytes():
    """`bytes=-500` means the last 500 bytes, not the first 500 - the trap most
    hand-written parsers get."""
    r = mr.parse_range("bytes=-500", 1000)
    assert (r.start, r.end) == (500, 999)


def test_suffix_longer_than_file_clamps_to_the_whole_file():
    r = mr.parse_range("bytes=-5000", 1000)
    assert (r.start, r.end) == (0, 999)


def test_end_past_eof_is_clamped_not_an_error():
    """RFC 7233: a longer end is not an error, the response is truncated."""
    r = mr.parse_range("bytes=0-99999", 1000)
    assert r.end == 999


@pytest.mark.parametrize("header", ["bytes=1000-2000", "bytes=200-100", "bytes=-0",
                                    "bytes=", "items=0-1"])
def test_unsatisfiable_or_unparsable_headers_raise_416(header):
    with pytest.raises(mr.RangeNotSatisfiable):
        mr.parse_range(header, 1000)


def test_multi_range_is_refused_instead_of_silently_partial():
    """Serving only the first range would hand a player the wrong bytes with a
    206. An error it can retry as a single range is better than a wrong answer."""
    with pytest.raises(mr.RangeNotSatisfiable) as exc:
        mr.parse_range("bytes=0-99,200-299", 1000)
    assert "multi-range" in str(exc.value)


def test_416_carries_content_range_advertisement():
    with pytest.raises(mr.RangeNotSatisfiable) as exc:
        mr.parse_range("bytes=5000-", 1000)
    assert exc.value.headers["content-range"] == "bytes */1000"


def test_206_headers_include_content_range_and_accept_ranges():
    r = mr.parse_range("bytes=0-9", 100)
    assert r.content_headers() == {"content-range": "bytes 0-9/100",
                                   "accept-ranges": "bytes"}


def test_zero_length_file_cannot_satisfy_any_range():
    with pytest.raises(mr.RangeNotSatisfiable):
        mr.parse_range("bytes=0-", 0)


def test_segment_media_types():
    """.m4s must be ISO media, not octet-stream: some Smart TV stacks refuse to
    parse fMP4 otherwise."""
    assert mr.media_type("out/x.m3u8") == "application/vnd.apple.mpegurl"
    assert mr.media_type("out/x.mpd") == "application/dash+xml"
    assert mr.media_type("out/seg-1.m4s") == "video/iso.segment"
    assert mr.media_type("out/seg.ts") == "video/mp2t"
    assert mr.media_type("out/unknown") == "application/octet-stream"


@pytest.mark.parametrize("child,root", [
    (r"T:\out\job1\..\..\etc\passwd", r"T:\out\job1"),
    (r"C:\Windows\x", r"T:\out\job1"),
    (r"T:\out", r"T:\out\job1"),
])
def test_path_traversal_is_rejected(child, root):
    assert not mr.is_inside(child, root)


def test_inside_is_allowed():
    assert mr.is_inside(r"T:\out\job1\hls\seg-1.ts", r"T:\out\job1")
    assert mr.is_inside(r"T:\out\job1\hls\master.m3u8", r"T:\out\job1")
