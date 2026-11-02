"""Charts for the README, written as SVG by hand.

Why not matplotlib: it is a 40 MB dependency for three bar charts, it makes
`pip install -e .` heavy for someone who just wants to run the pipeline, and its
output is not diffable. SVG written from the same JSON that produced the numbers
is deterministic, text-diffable, testable in CI without a plotting stack, and
crisp at any size in a browser or on GitHub.

Data in, file out: nothing here re-runs ffmpeg.
"""

from __future__ import annotations

import html

_BG = "#ffffff"
_GRID = "#e6e8eb"
_TEXT = "#1f2328"
_MUTED = "#57606a"
_PALETTE = ["#1f6feb", "#e53946", "#2ea043", "#bf8700", "#8250df", "#0969da"]
_FONT = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"


def _nice_max(value: float) -> float:
    """Round an axis maximum up to something a human can read."""
    if value <= 0:
        return 1.0
    mag = 10 ** len(str(int(value))) // 10 or 1
    step = mag / 4 or 0.25
    return ((int(value / step) + 1) * step)


def grouped_bars(categories: list[str], series: list[tuple[str, list[float]]],
                 out_path: str, *, title: str = "", ylabel: str = "",
                 width: int = 900, height: int = 420, note: str = "") -> str:
    """Grouped bar chart. `series` is [(label, values-per-category)]."""
    m_left, m_right, m_top, m_bottom = 62, 18, 46 if title else 26, 58
    plot_w = width - m_left - m_right
    plot_h = height - m_top - m_bottom

    peak = max((v for _, vals in series for v in vals), default=1.0)
    top = _nice_max(peak)

    n_cat = max(len(categories), 1)
    n_ser = max(len(series), 1)
    group_w = plot_w / n_cat
    gap = group_w * 0.16
    bar_w = (group_w - gap) / n_ser

    def y(v: float) -> float:
        return m_top + plot_h * (1 - (v / top))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{_FONT}" role="img"',
        f' aria-label="{html.escape(title or "bar chart")}">',
        f'<rect width="{width}" height="{height}" fill="{_BG}"/>',
    ]

    ticks = 5
    for i in range(ticks + 1):
        val = top * i / ticks
        yy = y(val)
        parts.append(f'<line x1="{m_left}" y1="{yy:.1f}" x2="{m_left + plot_w}" '
                     f'y2="{yy:.1f}" stroke="{_GRID}"/>')
        parts.append(f'<text x="{m_left - 8}" y="{yy + 4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="{_MUTED}">{val:g}</text>')

    for ci, cat in enumerate(categories):
        gx = m_left + ci * group_w
        parts.append(f'<text x="{gx + group_w / 2:.1f}" y="{m_top + plot_h + 18}" '
                     f'text-anchor="middle" font-size="12" fill="{_TEXT}">'
                     f'{html.escape(str(cat))}</text>')
        for si, (_label, vals) in enumerate(series):
            v = vals[ci] if ci < len(vals) else 0.0
            x = gx + gap / 2 + si * bar_w
            yy = y(v)
            fill = _PALETTE[si % len(_PALETTE)]
            parts.append(f'<rect x="{x:.1f}" y="{yy:.1f}" width="{bar_w * 0.9:.1f}" '
                         f'height="{m_top + plot_h - yy:.1f}" fill="{fill}"/>')
            parts.append(f'<text x="{x + bar_w * 0.45:.1f}" y="{yy - 5:.1f}" '
                         f'text-anchor="middle" font-size="10" fill="{_MUTED}">{v:g}</text>')

    parts.append(f'<line x1="{m_left}" y1="{m_top + plot_h}" x2="{m_left + plot_w}" '
                 f'y2="{m_top + plot_h}" stroke="{_TEXT}" stroke-width="1"/>')
    if ylabel:
        parts.append(f'<text x="14" y="{m_top + plot_h / 2:.1f}" font-size="12" '
                     f'fill="{_MUTED}" transform="rotate(-90 14 {m_top + plot_h / 2:.1f})" '
                     f'text-anchor="middle">{html.escape(ylabel)}</text>')
    if title:
        parts.append(f'<text x="{m_left}" y="24" font-size="15" font-weight="600" '
                     f'fill="{_TEXT}">{html.escape(title)}</text>')

    # legend
    lx = m_left
    ly = height - 16
    for si, (label, _) in enumerate(series):
        parts.append(f'<rect x="{lx}" y="{ly - 9}" width="11" height="11" '
                     f'fill="{_PALETTE[si % len(_PALETTE)]}"/>')
        parts.append(f'<text x="{lx + 16}" y="{ly}" font-size="11" fill="{_MUTED}">'
                     f'{html.escape(label)}</text>')
        lx += 24 + 7 * len(label)
    if note:
        parts.append(f'<text x="{m_left + plot_w}" y="{ly}" text-anchor="end" '
                     f'font-size="10" fill="{_MUTED}">{html.escape(note)}</text>')

    parts.append("</svg>")
    svg = "\n".join(parts)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    return out_path


def ladder_chart(rows: list[dict], out_path: str) -> str:
    """M1: per-rung wall time and the bitrate the master playlist ended up with."""
    labels = [r["rung"] for r in rows]
    return grouped_bars(
        labels,
        [("wall, s", [float(r.get("wall_s") or 0) for r in rows]),
         ("total bitrate, Mbps", [round((r.get("total_kbps") or 0) / 1000, 1) for r in rows])],
        out_path,
        title="M1 - per-rung encode time and delivered bitrate",
        ylabel="seconds / Mbps",
        note="h264_nvenc, 24 s 4K master",
    )


def vmaf_chart(rows: list[dict], out_path: str) -> str:
    labels = [r["rung"] for r in rows]
    return grouped_bars(
        labels,
        [("VMAF mean", [float(r.get("vmaf_mean") or 0) for r in rows]),
         ("VMAF min", [float(r.get("vmaf_min") or 0) for r in rows])],
        out_path,
        title="M1 - VMAF against the 4K reference (upscale methodology)",
        ylabel="VMAF",
        note="mean and worst frame per rung",
    )


def scaling_chart(rungs: list[str], series_by_workers: list[tuple[str, list[float]]],
                  out_path: str) -> str:
    return grouped_bars(
        rungs, series_by_workers, out_path,
        title="M2 - chunking on one GPU: more workers, more wall time",
        ylabel="wall, seconds",
        note="lower is better; NVENC block is single per card",
    )


def watermark_chart(rows: list[dict], out_path: str) -> str:
    labels = [r["tag"] for r in rows]
    return grouped_bars(
        labels,
        [("wall, s", [float(r.get("wall_s") or 0) for r in rows]),
         ("VMAF drop vs clean", [max(0.0, float(r.get("vmaf_drop") or 0)) for r in rows])],
        out_path,
        title="M3 - watermark cost: fused filters vs a second pass",
        ylabel="seconds / VMAF points",
        note="fused is inside the encode pass; twopass re-encodes everything",
    )
