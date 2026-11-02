"""Watermark filter fragments.

Everything here is a pure string builder. That matters for the claim this module
exists to support: a watermark should cost nothing, which is only true when its
filters run **inside the same encode pass** as the scale. A separate watermark
pass re-decodes and re-encodes the whole asset - measured at +97.1% wall time
(see `bench/m3_watermark.json`). So the filters are fragments that `encode`
appends after `scale`, never a second ffmpeg call.

Escaping is the part that silently breaks: `drawtext` option values live inside a
filtergraph string where `:` separates options, `,` separates filters and `[`/`]`
name pads. A subscriber ID containing a colon would otherwise be parsed as an
option boundary. `escape()` handles it, and a test pins it down.

This is a *visible/semi-visible* mark, i.e. session identification, not a
forensic watermark: it does not survive a re-encode of the picture. Real forensic
marking needs a robust payload spread across frames (DCT-band coefficients).
"""

from __future__ import annotations

import os
import re

from . import ff

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
            return f"fontfile='{escape(path)}'"
    return ""


def escape(value: str) -> str:
    """Escape a value used inside a filtergraph option.

    Order matters: backslash first, otherwise the backslashes this function
    introduces get escaped again.
    """
    out = value.replace("\\", "\\\\")
    for ch in (":", "'", "[", "]"):
        out = out.replace(ch, "\\" + ch)
    return out


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
        f"text='{escape(text)}'", f"fontsize={size}", f"fontcolor={color}",
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
    static = spec.get("static") or spec.get("logo_text")
    dynamic = spec.get("dynamic") or spec.get("viewer_id")
    ghost = spec.get("ghost")
    if ghost is True:
        ghost = dynamic_ghost(str(dynamic or static or "vod"))
    return chain(
        static_text(str(static)) if static else None,
        dynamic_text(str(dynamic)) if dynamic else None,
        ghost,
    )


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@#-]{1,64}$")


def is_safe_id(value: str) -> bool:
    """Watermark text ends up inside a filtergraph, so it is untrusted input.

    Even though `escape()` neutralises graph metacharacters, an allow-list keeps
    a client from smuggling an arbitrarily long or exotic expression into the
    encoder command line.
    """
    return bool(SAFE_ID_RE.fullmatch(value or ""))


def make_logo_args(path: str, text: str = "VODPIPE", w: int = 460, h: int = 120) -> list[str]:
    """argv that renders a PNG logo with lavfi - so the repo ships no binary assets."""
    return [
        ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black@0.0:s={w}x{h}:d=0.04",
        "-vf", chain(
            "format=rgba",
            "drawbox=x=0:y=0:w=iw:h=ih:color=white@0.10:t=fill",
            "drawbox=x=0:y=ih-4:w=iw:h=4:color=0xE53946@0.9:t=fill",
            _drawtext(text, size=44, font=font(True), color="white@0.9",
                      x="(w-text_w)/2", y="(h-text_h)/2-4"),
        ),
        "-frames:v", "1", path,
    ]


def make_logo(path: str, text: str = "VODPIPE", w: int = 460, h: int = 120,
              runner: ff.Runner | None = None) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    (runner or ff.Runner()).run(make_logo_args(path, text, w, h))
    return path
