"""Orchestrator: source -> ladder -> concat/verify -> package -> metrics.

Design points that are easy to get wrong:

* **Progress is per rung, not per phase.** A five-rung ladder takes minutes; if
  progress only advanced between probe/encode/package/metrics the client would
  stare at one number the whole time. `on_rung_done` fires after each rung.
* **Cancellation is cooperative, between rungs.** Cancelling mid-encode leaves a
  truncated file; between rungs the completed rungs are whole, so a cancelled job
  still ships usable output. `should_cancel` is injected so both the queue layer
  (Redis flag) and the CLI (Ctrl-C) can use it.
* **The callback receives the results list.** A closure over a variable that is
  only assigned after this function returns raises "referenced before
  assignment" - so state is passed in explicitly.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable

from . import encode, encoders, ff, metrics, packager, probe, ts, watermark
from .ladder import Rung, ladder


class Pipeline:
    def __init__(self, source: str, workdir: str, wm_spec: dict | None = None,
                 runner: ff.Runner | None = None):
        self.runner = runner or ff.Runner()
        self.src = probe.probe_source(source, runner=self.runner)
        self.work = workdir
        self.wm_spec = wm_spec or {}
        # Raw spec -> filter chain happens once, here, so the benchmark and the
        # queue worker measure exactly the graph production runs.
        self.wm = watermark.from_spec(self.wm_spec)
        os.makedirs(workdir, exist_ok=True)

    # ------------------------------------------------------------- encode
    def run_ladder(self, encoder: str = "h264_nvenc", preset: str | None = None,
                   nchunks: int = 1, workers: int = 1, tag: str = "",
                   rungs: list[Rung] | None = None,
                   on_rung_done: Callable[[int, int, list], None] | None = None,
                   should_cancel: Callable[[], bool] | None = None) -> tuple[list[dict], float]:
        """Encode every rung. Returns (results, ladder_wall_s).

        `nchunks > 1` uses the parallel path. Measured in bench/m2_bench.json: on
        one GPU more workers makes wall time *worse*, so the default is 1.
        """
        enc = encoders.build(encoder, preset)
        rungs = rungs or ladder()
        results: list[dict] = []
        t0 = time.perf_counter()

        for i, rung in enumerate(rungs):
            if should_cancel and should_cancel():
                # A marker, not a result: it has no file, so every later stage
                # must skip it. (A cancelled job once crashed here exactly, when
                # the bitrate loop probed out=None.)
                results.append({"cancelled": True, "rung": rung.label,
                                "encoder": enc.name, "mode": "skipped",
                                "wall_s": 0.0, "speed_x": 0.0, "out": None})
                break

            stem = f"{tag or 'ladder'}_{rung.label}_{enc.name}"
            out = os.path.join(self.work, stem + ".ts")
            if nchunks > 1:
                r = self._encode_chunked(rung, enc, out, nchunks, workers, stem)
            else:
                r = encode.transcode_whole(self.src, rung, enc, out, wm=self.wm,
                                           runner=self.runner)
            r["rung_res"] = f"{rung.width}x{rung.height}"
            r["target_kbps"] = rung.kbps
            # What the encoder was asked to write, kept so packaging can
            # re-assert it at the MP4 hop (`ts.normalize`). The fourcc that ends
            # up in the manifest is read back from the file, not taken from here.
            # Named `encoder_tag`, not `tag`: `tag` already means "matrix config
            # label" in bench.py rows (`nvenc_p5_whole`) and in the figures that
            # read them, and one JSON key meaning two unrelated things is how a
            # chart silently starts plotting the wrong series.
            r["encoder_tag"] = enc.tag
            results.append(r)
            print(f"  [{rung.label:>6}] {enc.name:<11} {r['mode']:<7} "
                  f"speed={r['speed_x']:>5.2f}x  wall={r['wall_s']:>6.1f}s")
            if on_rung_done:
                on_rung_done(i + 1, len(rungs), results)

        wall = time.perf_counter() - t0
        for r in results:
            if r.get("cancelled") or not r.get("out"):
                continue
            r["measured"] = metrics.bitrate(r["out"], runner=self.runner)
        return results, round(wall, 2)

    def _encode_chunked(self, rung: Rung, enc: encoders.Encoder, out: str,
                        nchunks: int, workers: int, stem: str) -> dict:
        chunk_dir = os.path.join(self.work, f"chunks_{stem}")
        result = encode.transcode_chunked(self.src, rung, enc, out, nchunks, workers,
                                          chunk_dir, wm=self.wm, runner=self.runner)
        # Chunks are the biggest intermediates in the pipeline and are useless
        # the instant concat has run.
        shutil.rmtree(chunk_dir, ignore_errors=True)
        return result

    # ---------------------------------------------------------- integrity
    def verify(self, results: list[dict]) -> list[dict]:
        """Frame-count check for every chunked rung.

        Container duration on a concatenated TS is not trustworthy (per-chunk PCR
        restart), so the check is the video packet count. See vodpipe.ts.
        """
        checks = []
        for r in results:
            if r.get("cancelled") or not r.get("out") or r["mode"] != "chunked":
                continue
            # The expectation uses the fps that rung was commanded to emit
            # (`out_fps`), not the source fps: they differ whenever the rung
            # ceiling resamples the master, and comparing across bases reports
            # a false frame-count failure.
            fps = r.get("out_fps") or self.src.fps
            expect = ts.expected_video_frames(self.src.duration, fps)
            checks.append({"rung": r["rung"],
                           **ts.verify_concat(r["out"], expect, runner=self.runner)})
        return checks

    # ---------------------------------------------------------- packaging
    def package(self, results: list[dict], out_dir: str, seg_dur: float = 4.0) -> dict:
        mp4s = self.normalize(results)
        if not mp4s:
            return {"hls": None, "dash": None, "ladder": {"variants": 0, "ladder": []}}
        rungs = [(r["rung"], r["normalize"]["mp4"]) for r in mp4s]
        return self._package_rungs(rungs, mp4s, out_dir, seg_dur)

    def normalize(self, results: list[dict]) -> list[dict]:
        """TS -> fragmented MP4 for every finished rung, in ladder order.

        `ts.normalize` reads the fourcc and audio presence back from the file it
        just wrote, so packaging decisions (which AdaptationSet a rendition joins,
        whether HLS may state ``CODECS=``, which output stream index it occupies)
        are made from what was really written, never from the encoder config.
        """
        out: list[dict] = []
        for r in results:
            if r.get("cancelled") or not r.get("out"):
                continue
            mp4 = os.path.splitext(r["out"])[0] + ".mp4"
            # MPEG-TS cannot carry the fourcc, so the remux re-derives it and HEVC
            # lands as `hev1`. That costs the ladder its client-side fallback
            # (HLS writes no CODECS= for hev1), so re-assert the encoder's tag.
            r["normalize"] = ts.normalize(r["out"], mp4, self.src.duration,
                                          tag=r.get("encoder_tag", ""), runner=self.runner)
            out.append(r)
        return out

    def package_dual(self, primary: list[dict], fallback: list[dict], out_dir: str,
                     seg_dur: float = 4.0) -> dict:
        """One manifest carrying both codecs, primary's rungs first.

        The point of a dual-codec ladder is that a client which cannot decode
        H.265 still finds a complete H.264 ladder in the same master playlist,
        so both groups go to a single packager invocation: two invocations
        would produce two masters, and a client never sees the fallback.

        Labels become ``<rung>_<encoder>`` because both ladders deliver the same
        resolutions; the packager writes one variant file per label.
        """
        mp4s = self.normalize(primary) + self.normalize(fallback)
        if not mp4s:
            return {"hls": None, "dash": None, "ladder": {"variants": 0, "ladder": []}}
        rungs = [(packager.safe_label(f"{r['rung']}_{r['encoder']}"), r["normalize"]["mp4"])
                 for r in mp4s]
        labels = [label for label, _ in rungs]
        if len(set(labels)) != len(labels):
            raise ValueError(f"duplicate rendition label in {labels}")
        return self._package_rungs(rungs, mp4s, out_dir, seg_dur)

    def _package_rungs(self, rungs: list[tuple[str, str]], encoded: list[dict],
                       out_dir: str, seg_dur: float) -> dict:
        # Both packagers need audio presence, and both get it from the same read
        # back off the written MP4: HLS because a silent rendition must not be
        # given an `a:` selector (`packager.var_stream_map`), DASH because output
        # stream indices shift when a rendition is silent
        # (`packager.output_stream_index`).
        audio = [r["normalize"]["has_audio"] for r in encoded]
        families = [packager.codec_family(r["normalize"]["video_tag"]) for r in encoded]
        hls = packager.package_hls(rungs, out_dir, seg_dur, runner=self.runner, audio=audio)
        dash = packager.package_dash(rungs, out_dir, seg_dur, runner=self.runner,
                                     families=families, audio=audio)
        return {"hls": hls, "dash": dash, "ladder": packager.manifest_stats(out_dir)}

    # ------------------------------------------------------------ metrics
    def vmaf_for(self, results: list[dict], tag: str = "",
                 key: Callable[[dict], str] | None = None) -> dict:
        """VMAF per rendition.

        `key` decides how a rendition is named in the result. It defaults to the
        rung label, which is what every committed artifact uses; a dual-codec run
        passes a codec-aware key because it delivers 1080p twice and the second
        measurement would otherwise overwrite the first.
        """
        key = key or (lambda r: r["rung"])
        out: dict = {}
        for r in results:
            if r.get("cancelled") or not r.get("out"):
                continue
            log = os.path.join(self.work, f"vmaf_{tag}{r['rung']}_{r['encoder']}.json")
            m = metrics.vmaf(self.src.path, r["out"], log,
                             ref_size=self.src.resolution, runner=self.runner)
            out[key(r)] = m
            if "error" in m:
                print(f"  VMAF {key(r):>18}: {m['error']}")
            else:
                print(f"  VMAF {key(r):>18}: "
                      f"mean={m['vmaf_mean']} harm={m['vmaf_harmonic']} min={m['vmaf_min']}")
        return out
