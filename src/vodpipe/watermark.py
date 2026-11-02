r"""Watermark filter fragments.

Everything here is a pure string builder. That matters for the claim this module
exists to support: a watermark should cost nothing, which is only true when its
filters run **inside the same encode pass** as the scale. A separate watermark
pass re-decodes and re-encodes the whole asset - measured at +103.9% wall time
(see `bench/m3_watermark.json`). So the filters are fragments that `encode`
appends after `scale`, never a second ffmpeg call.

Escaping is the part that silently breaks. In a `-vf` chain `,` separates
filters, `;` separates chains and `:` separates options, so an unescaped value
can turn into extra filters. The rule below was measured against ffmpeg 7.1, not
taken from documentation: each value was drawn three ways - `text=`, `textfile=`
(which reads the bytes verbatim and is therefore the ground truth), and a
payload trying to append a filter - and the rendered frames were compared.

Quoting **and** backslash-escaping the metacharacters is the spelling that
parses, renders identically to `textfile=`, and cannot splice. Each half alone
fails: an escaped quote inside a quoted region is not an escape (a backslash is
literal there), so the quote closes the value and the payload becomes filters;
and quoting without escaping leaves the colons to the option parser, so a
Windows font path and any `id:99` overlay stop parsing at all. The shell idiom
`'\''` is therefore wrong for a filtergraph.

Two things follow. A payload that would splice now fails closed (ffmpeg refuses
to parse it) instead of running, and the API allow-list refuses such text before
it reaches a command line. drawtext cannot render a literal apostrophe in any
spelling, which is why `'` is not in the allow-list.

This is a *visible/semi-visible* mark, i.e. session identification, not a
forensic watermark: it does not survive a re-encode of the picture. Real forensic
marking needs a robust payload spread across frames (DCT-band coefficients).
"""

from __future__ import annotations

import os
import re

# Default fonts resolved per-OS; overridable so a Linux container can run too.
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def font(bold: bool = False) -> str:
    """First font that exists, else let drawtext use its built-in default."""
    order = (_FONT_CANDIDATES[1], _FONT_CANDIDATES[0],
             _FONT_CANDIDATES[3], _FONT_CANDIDATES[2]) if bold else _FONT_CANDIDATES
    for path in order:
        if os.path.exists(path):
            return f"fontfile={escape(path)}"
    return ""


#: Characters that end or reshape a filtergraph value: `,` separates filters,
#: `;` separates filterchains, `[]` wrap pad links, `=` splits an option from
#: its name, `:` splits options inside one filter, and `'` opens/closes a quoted
#: region. A backslash is the escape character itself.
_GRAPH_SPECIALS = "\\:[]',;="


def escape(value: str) -> str:
    """Quote and escape one value inside a filtergraph option.

    Both halves are required, and each was checked against a real ffmpeg rather
    than assumed: quoting alone leaves a bare `'` able to close the value and
    splice filters onto the encode, and backslash-escaping alone leaves an
    unquoted value unable to contain a colon at all (a Windows font path such as
    `C:/Windows/Fonts/...` does not even parse). Escaping is done character by
    character, so a backslash in the input is escaped exactly once.
    """
    return "'" + "".join("\\" + c if c in _GRAPH_SPECIALS else c
                         for c in value) + "'"


def _comma(expr: str) -> str:
    """A comma inside an expression must not read as a filter separator."""
    return expr.replace(",", r"\,")


def static_text(text: str, size: int = 34, alpha: float = 0.55, margin: int = 36,
                color: str = "white@0.6") -> str:
    """Corner logo/text. Cheapest possible mark: one drawtext."""
    return _drawtext(text, size=size, font=font(False),
                     color=color,
                     x=f"w-tw-{margin}", y=f"h-th-{margin}",
                     borderw=2, bordercolor="black@0.5", alpha=str(alpha))


def dynamic_text(text: str, size: int = 30, speed: float = 17.0) -> str:
    """Per-subscriber mark that drifts and breathes.

    Drift is the point: a fixed-position overlay can be removed by a crop or a
    box-blur over that corner. A mark whose position and alpha are functions of
    `t` has no fixed pixel region to target.
    """
    x = _comma(f"mod(t*{speed}, W-tw-40)+40")
    y = "(H-th)/2 + 120*sin(t*0.31)"
    alpha = "0.30+0.10*sin(t*1.7)"
    return _drawtext(text, size=size, font=font(True), color="white",
                     x=f"'{x}'", y=f"'{y}'", borderw=1, bordercolor="black@0.4",
                     alpha=f"'{alpha}'")


def dynamic_ghost(text: str, size: int = 22, phase: float = 0.0) -> str:
    """Second, nearly invisible layer that only appears for part of a second.

    Cheap second identification channel: different phase from the primary mark,
    so cropping one still leaves the other in a different frame range.
    """
    x = _comma(f"W-tw-60-mod((t+{phase})*23, W-tw-40)")
    y = f"70+40*sin((t+{phase})*0.9)"
    alpha = _comma(f"between(mod(t+{phase},4), 2.6, 3.4)*0.28")
    return _drawtext(text, size=size, font=font(False), color="0x666666",
                     x=f"'{x}'", y=f"'{y}'", alpha=f"'{alpha}'")


def _drawtext(text: str, *, size: int, font: str, color: str, x: str, y: str,
              borderw: int = 0, bordercolor: str = "black@0.5", alpha: str = "1") -> str:
    parts = []
    if font:
        parts.append(font)
    parts += [
        f"text={escape(text)}", f"fontsize={size}", f"fontcolor={color}",
        f"x={x}", f"y={y}",
    ]
    if borderw:
        parts += [f"borderw={borderw}", f"bordercolor={bordercolor}"]
    if alpha != "1":
        parts.append(f"alpha={alpha}")
    return "drawtext=" + ":".join(parts)


def chain(*filters: str | None) -> str:
    """Join filters into one `-vf` chain, dropping the empty ones.

    Kept as a function (rather than `",".join`) so callers can pass `None` for
    "this watermark layer is off" without filtering it themselves.
    """
    return ",".join(f for f in filters if f)


# --------------------------------------------------------------------------
# spec -> filters (this is what the API sends; the benchmark uses the same path)
# --------------------------------------------------------------------------


def from_spec(spec: dict | None) -> str:
    """Raw API watermark spec -> filter chain.

    One place converts "what the client asked for" into "what ffmpeg runs", so
    the M3 benchmark measures exactly the code that serves production instead of
    a hand-written copy of it.
    """
    if not spec:
        return ""
    static = spec.get("static")
    dynamic = spec.get("dynamic")
    ghost = spec.get("ghost")
    if ghost is True:
        ghost = dynamic_ghost(str(dynamic or static or "vod"))
    return chain(
        static_text(str(static)) if static else None,
        dynamic_text(str(dynamic)) if dynamic else None,
        ghost,
    )


#: Watermark text allow-list. Space is included on purpose (a rights-holder
#: overlay is e.g. "MAXIMA VOD"); every graph metacharacter (quote, comma,
#: colon-as-option-boundary when unquoted, brackets) is excluded, so this list
#: is what lets the API accept a spec without reasoning about escaping.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@# -]{1,64}$")


def is_safe_id(value: str) -> bool:
    """Watermark text ends up inside a filtergraph, so it is untrusted input.

    `escape()` keeps the graph parseable; this allow-list is the second line of
    defence the API enforces, so a client cannot push an arbitrarily long or
    exotic expression into the encoder command line.
    """
    return bool(SAFE_ID_RE.fullmatch(value or ""))
