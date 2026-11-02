# -*- coding: utf-8 -*-
"""Оркестратор: источник -> лестница -> упаковка -> метрики. Один проход на ранг,
фильтры вотермарка встроены в тот же проход."""
import json
import os
import time

from . import encode, ff, packager, probe, quality, watermark
from .encoders import ENCODERS, ladder_4k


class Pipeline:
    def __init__(self, source: str, workdir: str, wm: dict | None = None):
        self.src = probe.source_info(source)
        self.work = workdir
        self.wm = wm or {}
        os.makedirs(workdir, exist_ok=True)

    def wm_chain(self, vextra=()):
        """Сырые значения вотермарка (текст/bool, как их присылает API) -> filtergraph.

        Держим один источник правды: и API-воркер, и бенч M3 строят граф здесь,
        иначе бенч мерит не тот код, который работает в проде.
        """
        static = self.wm.get("static")
        dynamic = self.wm.get("dynamic")
        ghost = self.wm.get("ghost")
        if isinstance(ghost, bool) and ghost:
            ghost = watermark.dynamic_ghost(dynamic or static or "vod")
        return watermark.build(
            vextra=vextra,
            static=watermark.static_text(static) if static else None,
            dynamic=watermark.dynamic_text(dynamic) if dynamic else None,
            ghost=ghost,
        )

    def run_ladder(self, encoder_name="h264_nvenc", chunk_mode=None, nchunks=6,
                   workers=4, tag="", rungs=None, do_metrics=True, preset=None,
                   on_rung_done=None, should_cancel=None):
        # preset=None -> дефолт профиля энкодера; API передаёт его явно, чтобы
        # сравнивать p1/p5 без правки кода.
        # on_rung_done(i, rung) и should_cancel() -> честный прогресс и отмена
        # между рангами, а не только между фазами: лестница из 5 рангов это минуты.
        enc = ENCODERS[encoder_name](preset) if preset else ENCODERS[encoder_name]()
        rungs = rungs or ladder_4k(hevc_top=(encoder_name == "hevc_nvenc"))
        results = []
        t0 = time.perf_counter()
        for i, rung in enumerate(rungs):
            if should_cancel and should_cancel():
                results.append({"cancelled": True, "rung": rung.label,
                                "encoder": enc.name, "mode": "skipped",
                                "wall_s": 0.0, "speed_x": 0.0,
                                "out": None})
                break
            out = os.path.join(self.work, f"{tag or 'ladder'}_{rung.label}_{enc.name}.ts")
            ve = self.wm_chain()
            if chunk_mode == "chunked":
                r = encode.transcode_chunked(self.src, rung, enc, out, nchunks, workers,
                                             self.work, vextra=[ve] if ve else ())
            else:
                r = encode.transcode_whole(self.src, rung, enc, out,
                                           vextra=[ve] if ve else ())
            r["rung_res"] = f"{rung.width}x{rung.height}"
            r["target_kbps"] = rung.bitrate // 1000
            results.append(r)
            print(f"  [{rung.label:>6}] {enc.name:<10} {r.get('mode'):<7} "
                  f"speed={r['speed_x']:.2f}x  wall={r['wall_s']:.1f}s")
            # передаём список результатов в колбэк: вызывающий код не может
            # замкнуться на переменную, которая присвоится только после возврата
            if on_rung_done:
                on_rung_done(i + 1, len(rungs), results)
        wall = time.perf_counter() - t0

        if do_metrics:
            for r, rung in zip(results, rungs):
                # отменённый ранг — это маркер без файла: битрейт мерить не по чему
                if r.get("cancelled") or not r.get("out"):
                    continue
                r["measured"] = quality.bitrate(r["out"])
        return results, round(wall, 2)

    def package(self, results, out_dir, seg_dur=4.0):
        # TS-чанки после склейки содержат стыковые разрывы PCR: сначала нормализуем
        # каждый ранг remux'ом в MP4 (без перекодирования), затем уже CMAF.
        norm = []
        for r in results:
            mp4 = os.path.splitext(r["out"])[0] + ".mp4"
            chk = packager.normalize(r["out"], mp4, self.src.duration)
            r["normalize"] = chk
            norm.append((r["rung"], mp4))
        res = packager.package_hls(norm, out_dir, seg_dur)
        dash = packager.package_dash(norm, out_dir, seg_dur)
        stats = packager.manifest_stats(out_dir)
        return {"hls": res, "dash": dash, "ladder": stats}

    def vmaf_for(self, results, tag=""):
        """VMAF против источника (upscale-методология, см. quality.vmaf)."""
        out = {}
        ref_size = f"{self.src.width}x{self.src.height}"
        for r in results:
            out[r["rung"]] = quality.vmaf(
                self.src.path, r["out"],
                os.path.join(self.work, f"vmaf_{tag}{r['rung']}_{r['encoder']}.json"),
                ref_size=ref_size,
            )
            m = out[r["rung"]]
            print(f"  VMAF {r['rung']:>6} {r['encoder']:<10}: "
                  f"mean={m.get('vmaf_mean')} harm={m.get('vmaf_harmonic')} "
                  f"min={m.get('vmaf_min')}")
        return out


def dump(path: str, obj) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path
