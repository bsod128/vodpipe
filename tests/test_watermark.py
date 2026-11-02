"""Watermark filter construction and the escaping that keeps it parseable."""

from __future__ import annotations

import pytest
from conftest import requires_ffmpeg

from vodpipe import ff
from vodpipe import watermark as wm


def test_static_and_dynamic_are_one_filter_chain():
    """A watermark only costs ~0% when its filters run inside the same encode
    pass as the scale. A second pass measured +103.9% wall time."""
    chain = wm.chain(wm.static_text("ACME"), wm.dynamic_text("viewer-42"))
    assert chain.count("drawtext=") == 2
    assert "ACME" in chain and "viewer-42" in chain


def test_chain_drops_disabled_layers():
    assert wm.chain(None, "scale=1920:-2", None) == "scale=1920:-2"
    assert wm.chain() == ""


def test_escape_quotes_and_escapes_every_graph_metacharacter():
    """Two halves, both measured against a real ffmpeg, not assumed.

    Quoting alone is not enough: the option parser runs after the quotes are
    stripped, so a colon inside the quotes still ends the option. Escaping alone
    is not enough either: a bare quote closes the value and the rest of the
    payload becomes filters. Hence quote *and* escape.
    """
    assert wm.escape("a:b,c[d]") == r"'a\:b\,c\[d\]'"
    assert wm.escape(r"a\b") == r"'a\\b'"          # the escape char itself
    assert wm.escape("id:99") == r"'id\:99'"
    # A Windows font path only parses this way - quoting alone broke it.
    assert wm.font().startswith("fontfile='C\\:/")


def test_escaping_never_leaves_an_unescaped_quote_inside_the_value():
    """A `'` inside a quoted region ends it, which is how `x',BOGUS9,...` once
    appended filters to the encode. Whatever the spelling, the quoted region
    must contain no bare quote.

    (drawtext cannot *render* a literal apostrophe in any spelling, which is why
    the API allow-list does not accept one; this test pins the parseability and
    the containment, not the glyph.)
    """
    for value in ("it's", "x',scale=64:64,drawtext=text=INJECTED"):
        escaped = wm.escape(value)
        assert escaped.startswith("'") and escaped.endswith("'")
        inner = escaped[1:-1].replace("\\'", "")
        assert "'" not in inner, f"bare quote left in {escaped!r}"


@pytest.mark.parametrize("text", ["MAXIMA VOD", "id:99", "viewer-42f7a9"])
@requires_ffmpeg
def test_every_shipped_mark_parses_in_a_real_graph(text):
    """The regression this guards: an escaping change that looked more correct
    made the *font path* unparseable, so every watermark encode died with EINVAL
    before a single frame was written. Unit assertions on the string cannot catch
    that; ffmpeg can.

    1 frame of a synthetic source, `-f null`: a few hundred milliseconds.
    """
    if "drawtext" not in ff.filter_names():
        pytest.skip("this ffmpeg build has no drawtext")

    argv = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.04",
            "-vf", wm.static_text(text), "-frames:v", "1", "-f", "null", "-"]
    res = ff.Runner(timeout_s=60).run(argv, check=False)
    assert res.returncode == 0, f"{text!r} did not parse: {res.stderr[-300:]}"


def test_comma_in_an_expression_is_not_a_filter_separator():
    """drawtext x/y expressions contain commas; unescaped they split the graph
    into extra filters."""
    expr = wm._comma("mod(t*17, W-tw-40)")
    assert r"\," in expr
    assert wm.dynamic_text("v").count("\\,") >= 1


def test_dynamic_mark_moves_so_a_crop_cannot_remove_it():
    """A fixed-position overlay is removed by cropping the corner. Position and
    alpha here are functions of `t`, so there is no fixed pixel region."""
    text = wm.dynamic_text("session-7")
    assert "mod(t*" in text
    assert "sin(t" in text
    assert "alpha='" in text


def test_ghost_layer_is_gated_to_part_of_every_second():
    text = wm.dynamic_ghost("x", phase=1.3)
    assert "between(mod(t+1.3" in text


def test_from_spec_maps_api_payload_to_filters():
    spec = {"static": "ACME", "dynamic": "sess-1", "ghost": True}
    chain = wm.from_spec(spec)
    assert chain.count("drawtext=") == 3


def test_from_spec_ignores_unknown_keys():
    """The spec vocabulary is `static/dynamic/ghost` (what WatermarkIn emits).

    Unknown keys are not silently mapped to something else - the old
    `logo_text`/`viewer_id` aliases were removed because they were a second,
    undocumented spelling of the same two fields with no producer left.
    """
    assert wm.from_spec({"logo_text": "ACME", "viewer_id": "u9"}) == ""
    assert wm.from_spec({"static": "ACME", "dynamic": "u9"}) != ""


def test_from_spec_empty_is_no_op():
    assert wm.from_spec(None) == ""
    assert wm.from_spec({}) == ""


@pytest.mark.parametrize("value", [
    "viewer-42", "sess_1.2", "user@example", "id:99", "#tag", "MAXIMA VOD", "A" * 64])
def test_safe_ids_accepted(value):
    """A space is allowed on purpose: a rights-holder overlay reads e.g.
    "MAXIMA VOD", and the M3 benchmark uses exactly that spec."""
    assert wm.is_safe_id(value)


@pytest.mark.parametrize("value", [
    "", "a:b;c", "quote'd", "x, scale=1", "x';y", "nl\nline", "x" * 65, "$(rm -rf)"])
def test_unsafe_ids_rejected(value):
    """Watermark text is untrusted client input that lands inside a filtergraph.
    `escape()` neutralises the graph metacharacters, but an allow-list also
    stops absurdly long or exotic expressions reaching the encoder."""
    assert not wm.is_safe_id(value)
