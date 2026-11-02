"""Watermark filter construction and the escaping that keeps it parseable."""

from __future__ import annotations

import pytest

from vodpipe import watermark as wm


def test_static_and_dynamic_are_one_filter_chain():
    """A watermark only costs ~0% when its filters run inside the same encode
    pass as the scale. A second pass measured +97.1% wall time."""
    chain = wm.chain(wm.static_text("ACME"), wm.dynamic_text("viewer-42"))
    assert chain.count("drawtext=") == 2
    assert "ACME" in chain and "viewer-42" in chain


def test_chain_drops_disabled_layers():
    assert wm.chain(None, "scale=1920:-2", None) == "scale=1920:-2"
    assert wm.chain() == ""


def test_escape_protects_graph_metacharacters():
    """A viewer id with a colon would otherwise read as an option boundary and
    break the whole graph (or inject an option)."""
    escaped = wm.escape("a:b'c[d]e")
    for ch in (":", "'", "[", "]"):
        assert "\\" + ch in escaped
        assert escaped.replace("\\" + ch, "").count(ch) == 0


def test_escape_does_not_double_escape_its_own_backslashes():
    """Order matters: backslash first. Escaping it last would turn every
    introduced `\\:` into `\\\\:` and shift the whole graph."""
    assert wm.escape(r"a\b") == r"a\\b"
    assert wm.escape("a:b") == "a\\:b"


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


def test_from_spec_accepts_both_payload_spellings():
    """The benchmark and the API both build a watermark; older keys keep working
    so a stored job spec still runs."""
    a = wm.from_spec({"logo_text": "ACME", "viewer_id": "u9"})
    b = wm.from_spec({"static": "ACME", "dynamic": "u9"})
    assert a == b


def test_from_spec_empty_is_no_op():
    assert wm.from_spec(None) == ""
    assert wm.from_spec({}) == ""


@pytest.mark.parametrize("value", [
    "viewer-42", "sess_1.2", "user@example", "id:99", "#tag", "A" * 64])
def test_safe_ids_accepted(value):
    assert wm.is_safe_id(value)


@pytest.mark.parametrize("value", [
    "", "has space", "a:b;c", "x" * 65, "$(rm -rf)", "quote'd", "nl\nline"])
def test_unsafe_ids_rejected(value):
    """Watermark text is untrusted client input that lands inside a filtergraph.
    `escape()` neutralises the graph metacharacters, but an allow-list also
    stops absurdly long or exotic expressions reaching the encoder."""
    assert not wm.is_safe_id(value)
