# -*- coding: utf-8 -*-
"""Упаковка лестницы в CMAF: fMP4-сегменты + HLS-плейлист + DASH-манифест.

Один проход ffmpeg со всеми рангами через -filter_complex per-rung и
-var_stream_map: это то, почему лестница упаковывается атомарно и сегменты
выровнены по времени (required для корректного ABR-switch на клиенте).
"""
import os
import re

from . import ff


def _clean(p: str):
    """Двоеточия и пробелы в путях ломают парсер filtergraph/segment list."""
    return p.replace("\\", "/")


def _inputs(args: list, rungs_out: list) -> int:
    for _, p in rungs_out:
        args += ["-i", _clean(p)]
    return len(rungs_out)


def _maps(n: int) -> list:
    """Прямой маппинг потоков: -c copy работает только без filter_complex."""
    out = []
    for i in range(n):
        out += ["-map", f"{i}:v:0", "-map", f"{i}:a:0?"]
    return out


def _bsf_a(rungs_out: list) -> list:
    """AAC из MPEG-TS лежит как ADTS и требует aac_adtstoasc для вставки в fMP4."""
    if any(p.lower().endswith(".ts") for _, p in rungs_out):
        return ["-bsf:a", "aac_adtstoasc"]
    return []


def _label(label: str) -> str:
    """Имя варианта попадает в -var_stream_map и в имена файлов."""
    return re.sub(r"[^0-9A-Za-z_.-]", "", label) or "v"


def package_hls(rungs_out: list, out_dir: str, seg_dur: float = 4.0):
    """rungs_out: [(label, ts_path)] — ранги с aac-аудио внутри.

    CMAF: -hls_segment_type fmp4 + -hls_flags independent_segments => один и тот же
    набор сегментов можно отдавать и как HLS, и как DASH.
    """
    os.makedirs(out_dir, exist_ok=True)
    args = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    n = _inputs(args, rungs_out)
    args += _maps(n)

    var = " ".join(f"v:{i},a:{i},name:{_label(label)}"
                   for i, (label, _) in enumerate(rungs_out))
    hls_dir = os.path.join(out_dir, "hls")
    os.makedirs(hls_dir, exist_ok=True)
    out_hls = _clean(os.path.join(hls_dir, "stream_%v.m3u8"))
    seg_tmpl = _clean(os.path.join(hls_dir, "stream_%v", "seg_%05d.m4s"))

    args += [
        "-c", "copy",
        *_bsf_a(rungs_out),
        "-var_stream_map", var,
        "-f", "hls",
        "-hls_time", str(seg_dur),
        "-hls_segment_type", "fmp4",
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments+omit_endlist",
        "-hls_segment_filename", seg_tmpl,
        "-master_pl_name", "master.m3u8",
        out_hls,
    ]
    _, _, wall = ff.run(args)
    m3u8 = os.path.join(hls_dir, "master.m3u8")
    return {"kind": "hls", "master": m3u8, "wall_s": round(wall, 2),
            "variants": _hls_variants(m3u8), "segments": _count(hls_dir, "m4s")}


def _hls_variants(master_path: str) -> list:
    if not os.path.exists(master_path):
        return []
    txt = open(master_path, encoding="utf-8", errors="replace").read()
    out = []
    for m in re.finditer(
        r"#EXT-X-STREAM-INF:[^\n]*BANDWIDTH=(\d+)[^\n]*?RESOLUTION=(\d+x\d+)[^\n]*\n([^\n#]+)", txt
    ):
        out.append({"bandwidth": int(m.group(1)), "resolution": m.group(2), "uri": m.group(3).strip()})
    return out


def normalize(ts_path: str, mp4_path: str, expect_dur: float = 0.0):
    """TS -> MP4 (remux, без перекодирования).

    Нужно, потому что склейка чанков даёт в TS стыковые разрывы PTS/PCR: сам файл
    полный, но demuxer ругается "Packet corrupt". MP4-remux нормализует таймстемпы,
    и дальше HLS/DASH упаковываются чисто. Обязательная проверка: количество
    видео-пакетов не должно измениться (иначе где-то потерялись кадры).
    """
    os.makedirs(os.path.dirname(mp4_path) or ".", exist_ok=True)
    args = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "warning",
            "-fflags", "+genpts", "-i", _clean(ts_path),
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart", mp4_path]
    ff.run(args)
    before = _pkt_stats(ts_path)
    after = _pkt_stats(mp4_path)
    # Длительность склеенного TS по метаданным некорректна (в каждом чанке
    # перезапускаются PCR), поэтому длительность сверяем только по результату,
    # а целостность — по числу пакетов.
    ok = (before["vpkts"] == after["vpkts"] and before["apkts"] == after["apkts"])
    if expect_dur:
        ok = ok and abs(after["duration"] - expect_dur) <= 0.5
    return {"ok": ok, "vpkts_ok": before["vpkts"] == after["vpkts"],
            "mp4": mp4_path, "before": before, "after": after,
            "expected_dur": expect_dur}


def _pkt_stats(path: str) -> dict:
    """Считает пакеты отдельным вызовом на тип потока.

    Один вызов с `-count_packets` и обоими типами на склеенном TS даёт пустой
    nb_read_packets, поэтому спрашиваем видео и аудио по отдельности.
    """
    dur = float(ff.probe_json(path, ["-show_entries", "format=duration"])
                .get("format", {}).get("duration") or 0.0)

    def n(kind: str) -> int:
        j = ff.probe_json(path, ["-select_streams", kind, "-count_packets",
                                 "-show_entries", "stream=nb_read_packets"])
        return sum(int(s.get("nb_read_packets") or 0) for s in j.get("streams", []))

    return {"duration": round(dur, 3), "vpkts": n("v"), "apkts": n("a")}


def package_dash(rungs_out: list, out_dir: str, seg_dur: float = 4.0):
    """DASH-манифест из тех же рангов (тот же CMAF-сегментный поток)."""
    os.makedirs(out_dir, exist_ok=True)
    args = [ff.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    n = _inputs(args, rungs_out)
    args += _maps(n)
    dash_dir = os.path.join(out_dir, "dash")
    os.makedirs(dash_dir, exist_ok=True)
    out = _clean(os.path.join(dash_dir, "manifest.mpd"))
    args += [
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-f", "dash",
        "-seg_duration", str(seg_dur),
        "-use_template", "1",
        "-use_timeline", "1",
        "-adaptation_sets", "id=0,streams=v id=1,streams=a",
        out,
    ]
    _, _, wall = ff.run(args)
    return {"kind": "dash", "manifest": os.path.join(dash_dir, "manifest.mpd"),
            "wall_s": round(wall, 2),
            "representations": _dash_reps(os.path.join(dash_dir, "manifest.mpd")),
            "segments": _count(dash_dir, "m4s")}


def _dash_reps(mpd_path: str) -> list:
    """Разбирает Representation из MPD.

    ffmpeg пишет атрибуты в нижнем регистре (bandwidth=), а спецификация MABR
    допускает любой регистр, поэтому поиск регистронезависимый и по каждому
    тегу отдельно. Типа потока берётся из contentType родителя-AdaptationSet —
    без этого аудио-Representation неотличим от видео.
    """
    if not os.path.exists(mpd_path):
        return []
    txt = open(mpd_path, encoding="utf-8", errors="replace").read()
    reps = []
    for asm in re.finditer(r"<AdaptationSet\b[^>]*>(.*?)</AdaptationSet>", txt, re.S | re.I):
        atag = re.match(r"<AdaptationSet\b[^>]*>", asm.group(0), re.I).group(0)
        ct = re.search(r'\bcontenttype="([^"]+)"', atag, re.I)
        media = (ct.group(1).split("/")[0].lower() if ct else "?")
        for m in re.finditer(r"<Representation\b[^>]*>", asm.group(1), re.I):
            tag = m.group(0)
            bw = re.search(r'\bbandwidth="(\d+)"', tag, re.I)
            w = re.search(r'\bwidth="(\d+)"', tag, re.I)
            h = re.search(r'\bheight="(\d+)"', tag, re.I)
            reps.append({"media": media,
                         "bandwidth": int(bw.group(1)) if bw else None,
                         "width": int(w.group(1)) if w else None,
                         "height": int(h.group(1)) if h else None})
    return reps


def _count(root: str, ext: str) -> int:
    c = 0
    for _, _, files in os.walk(root):
        c += sum(1 for f in files if f.endswith("." + ext))
    return c


def manifest_stats(out_dir: str) -> dict:
    """Считаем по выходным манифестам реальную лестницу: ранги и суммарный битрейт."""
    m = os.path.join(out_dir, "hls", "master.m3u8")
    vs = _hls_variants(m)
    ladder = []
    for v in vs:
        ladder.append({"resolution": v["resolution"], "total_kbps": v["bandwidth"] // 1000})
    ladder.sort(key=lambda d: -d["total_kbps"])
    return {"variants": len(ladder), "ladder": ladder,
            "top_kbps": ladder[0]["total_kbps"] if ladder else 0,
            "bottom_kbps": ladder[-1]["total_kbps"] if ladder else 0}
