# -*- coding: utf-8 -*-
"""Вотермарки: статичная (логотип/PNG-overlay, burn-in текст) и динамическая
(персональная, по сессии: движущийся текст + меняющаяся невидимая сетка).

Обе реализованы как filtergraph-фрагменты, чтобы их можно было включить в ТОТ ЖЕ
проход транскода. Это принципиально: отдельный проход с вотермарком удваивает
время пайплайна, а «без критической потери производительности» из вакансии
достигается именно слиянием фильтров в один graph.
"""
import os

FONT = r"C:/Windows/Fonts/segoeui.ttf"
FONT_BOLD = r"C:/Windows/Fonts/segoeuib.ttf"


def _esc(s: str) -> str:
    """Экранирование для значения опции фильтра ffmpeg (: [ ] , ' \\)."""
    return s.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'").replace("[", r"\[").replace("]", r"\]")


def _font(p: str) -> str:
    return "fontfile='" + _esc(p) + "'"


def static_text(text: str, size: int = 34, alpha: float = 0.55,
                margin: int = 36, color: str = "white@0.6") -> str:
    """Угол экрана, постоянный. Самый дешёвый вариант: один drawtext."""
    return (
        f"drawtext={_font(FONT)}:text='{_esc(text)}':fontsize={size}:"
        f"fontcolor={color}:x=w-tw-{margin}:y=h-th-{margin}:"
        f"borderw=2:bordercolor=black@0.5:alpha={alpha}"
    )


def dynamic_text(text: str, size: int = 30, speed: float = 17.0) -> str:
    """Персональный вотермарк: позиция «дрейфует» по выражению от t, поэтому
    кадр-точечный crop/mask её не вырежет, и текст меняет яркость.

    x/y считаются замедленным motion-паттерном; для честного A/B замеряем overhead
    относительно того же graph без этих двух drawtext.
    """
    x = "mod(t*{sp}\\,W-tw-40)+40".format(sp=speed)
    y = "(H-th)/2 + 120*sin(t*0.31)"
    a = "0.30+0.10*sin(t*1.7)"
    return (
        f"drawtext={_font(FONT_BOLD)}:text='{_esc(text)}':fontsize={size}:"
        f"fontcolor=white:borderw=1:bordercolor=black@0.4:"
        f"x='{x}':y='{y}':alpha='{a}'"
    )


def dynamic_ghost(text: str, size: int = 22, phase: float = 0.0) -> str:
    """Второй слой: почти невидимый, появляется только на доли кадра. Дешёвая
    замена forensic-вотермаркинга без перерасчёта пикселей."""
    x = "W-tw-60-mod((t+{p})*23\\,W-tw-40)".format(p=phase)
    y = "70+40*sin((t+{p})*0.9)".format(p=phase)
    a = "between(mod(t+{p}\\,4)\\,2.6\\,3.4)*0.28".format(p=phase)
    return (
        f"drawtext={_font(FONT)}:text='{_esc(text)}':fontsize={size}:"
        f"fontcolor=0x666666:x='{x}':y='{y}':alpha='{a}'"
    )


def overlay_logo(logo_path: str, index: int = 1, margin: int = 34, opacity: float = 0.85) -> str:
    """PNG-логотип поверх. Возвращает fragment для -filter_complex (не -vf):
    нужен второй вход. `index` — порядковый номер входа логотипа."""
    return (
        f"[0:v][{index}:v]overlay=x=main_w-overlay_w-{margin}:y=main_h-overlay_h-{margin}:"
        f"format=auto:alpha=1[wm]"
    )


def make_logo(path: str, text: str = "MAXIMA•VOD", w: int = 460, h: int = 120):
    """Генерируем PNG-логотип самим (lavfi + drawtext) — без внешних ассетов."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    from . import ff
    ff.run([
        ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=black@0.0:s={w}x{h}:d=0.04",
        "-vf", (
            f"format=rgba,drawbox=x=0:y=0:w=iw:h=ih:color=white@0.10:t=fill,"
            f"drawbox=x=0:y=ih-4:w=iw:h=4:color=0xE53946@0.9:t=fill,"
            f"drawtext={_font(FONT_BOLD)}:text='{_esc(text)}':fontsize=44:"
            f"fontcolor=white@0.9:x=(w-text_w)/2:y=(h-text_h)/2-4"
        ),
        "-frames:v", "1", path,
    ], check=True)
    return path


def build(vextra=(), static=None, dynamic=None, ghost=None):
    """Собираем последовательный chain фильтров для -vf / scale-префикса."""
    chain = list(vextra)
    for f in (static, dynamic, ghost):
        if f:
            chain.append(f)
    return ",".join(chain)
