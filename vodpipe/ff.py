# -*- coding: utf-8 -*-
"""Обвязка над ffmpeg/ffprobe: поиск бинарников, запуск, парсинг speed из stderr."""
import json
import os
import re
import shutil
import subprocess
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# ffmpeg 7.1-essentials — единственная сборка, чей NVENC работает с нашим драйвером
# (найт N-128198 требует драйвер >= 610 и падает на "Required: 13.1 Found: 13.0").
# ffprobe берём из той же сборки, чтобы кодек/тайминги в пробах и в коде совпадали;
# отдельный ночной ffprobe остаётся запасным вариантом.
_CANDS = [
    r"T:\resume\video\bin\ffmpeg.exe",
    r"T:\resume\video\bin\ffprobe.exe",
    os.path.join(_ROOT, "bin", "ffmpeg.exe"),
    r"T:\resume\media\bin\ffprobe.exe",
    os.path.join(_ROOT, "bin", "ffprobe.exe"),
]


def work_root() -> str:
    """Каталог под черновую работу (чанки, лестница, VMAF-логи).

    Держим рядом с проектом на T:, но место под тяжёлые прогоны конечное: при
    нехватке места переопределяем переменной VODPIPE_WORK или --work.
    """
    return os.environ.get("VODPIPE_WORK") or os.path.join(_ROOT, "work")


def _which(name: str) -> str:
    """Ищет бинарь: сначала явная переменная окружения, затем кандидаты, затем PATH.

    Кандидаты сравниваются по имени без расширения: на Windows `ffprobe.exe`
    иначе никогда не совпало бы с запросом `ffprobe`.
    """
    env = os.environ.get("VODPIPE_" + name.upper())
    if env and os.path.exists(env):
        return env
    for c in _CANDS:
        if os.path.splitext(os.path.basename(c))[0].lower() == name.lower() \
                and os.path.exists(c):
            return c
    p = shutil.which(name) or shutil.which(name + ".exe")
    if p:
        return p
    raise FileNotFoundError(name + " не найден; задайте VODPIPE_" + name.upper())


_OPT_CACHE = {}


def encoder_options(codec: str) -> str:
    """Даump -h encoder=<codec>: нужен, чтобы подбирать написание опций
    (в разных сборках ffmpeg: -spatial_aq против -spatial-aq) и не падать на
    «Unrecognized option»."""
    if codec not in _OPT_CACHE:
        out, _, _ = run([ffmpeg(), "-hide_banner", "-h", "encoder=" + codec], check=False)
        _OPT_CACHE[codec] = out
    return _OPT_CACHE[codec]


def opt(codec: str, *names: str) -> list:
    """Вернуть флаг в форме, которую понимает эта сборка: opt('h264_nvenc',
    'spatial_aq', 'spatial-aq') -> ['-spatial-aq'] или ['-spatial_aq']."""
    help_txt = encoder_options(codec)
    for n in names:
        if re.search(r"-\s*" + re.escape(n) + r"\b", help_txt):
            return ["-" + n]
    return []


def ffmpeg() -> str:
    return _which("ffmpeg")


def ffprobe() -> str:
    return _which("ffprobe")


_SPEED_RE = re.compile(r"speed=\s*([0-9.]+)x")


class FFError(RuntimeError):
    def __init__(self, cmd, stderr):
        super().__init__("ffmpeg failed rc: " + " ".join(str(c) for c in cmd[-16:]))
        self.cmd = list(cmd)
        self.stderr = stderr


def run(args, check=True, log=False):
    """Запуск ffmpeg/ffprobe.

    Возвращает (stdout, speed_x, wall_s). speed_x — из последней строки
    статистики ffmpeg: это честная мера «раз реального времени за секунду
    wall-clock», а не субъективная оценка скорости.
    """
    args = [str(a) for a in args]
    t0 = time.perf_counter()
    r = subprocess.run(args, capture_output=True, text=True, errors="replace")
    wall = time.perf_counter() - t0
    if check and r.returncode != 0:
        raise FFError(args, r.stderr or "")
    speed = 0.0
    if r.stderr:
        matches = _SPEED_RE.findall(r.stderr)
        if matches:
            speed = float(matches[-1])
    if log and r.stderr:
        tail = [ln for ln in r.stderr.splitlines() if ln.strip()][-1:]
        if tail:
            print("   ", tail[0][:150])
    return (r.stdout or ""), speed, wall


def probe_json(path, extra=None):
    args = [ffprobe(), "-v", "error", "-print_format", "json"]
    if extra:
        args += list(extra)
    args.append(path)
    out, _, _ = run(args)
    return json.loads(out or "{}")


def has_encoder(name: str) -> bool:
    """Проверка наличия энкодера по выводу `-encoders`.

    Формат строки: ` V....D h264_nvenc  ...` — буква типа (V/A/S) плюс ещё пять
    флагов, поэтому колонку флагов ловим как \\S{6}, а не как фиксированный
    шаблон из точек: иначе NVENC-строки (V....D) не матчатся.
    """
    out, _, _ = run([ffmpeg(), "-hide_banner", "-encoders"], check=True)
    return re.search(r"^\s*[VAS]\S{5}\s+" + re.escape(name) + r"\b", out, re.M) is not None


def has_filter(name: str) -> bool:
    out, _, _ = run([ffmpeg(), "-hide_banner", "-filters"], check=True)
    return re.search(r"^\s*[A-Z.]{3}\s+" + re.escape(name) + r"\s", out, re.M) is not None


def version() -> str:
    out, _, _ = run([ffmpeg(), "-hide_banner", "-version"])
    return out.splitlines()[0] if out else "?"
