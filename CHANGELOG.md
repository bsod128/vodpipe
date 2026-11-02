# Changelog

All notable changes to this project are documented here. The measured numbers
refer to the artifacts committed under `bench/`.

## 1.0.0

The pipeline is complete and every claim in the README is backed by a committed
artifact.

### Added
- Ladder encoding: five rungs (2160p..432p), CBR with per-rung caps, GOP tied to
  fps, exactly-16:9 rungs (non-16:9 breaks DASH AdaptationSets).
- Encoder abstraction: h264_nvenc / hevc_nvenc / h264_qsv / libx264, with option
  spellings resolved from `ffmpeg -h` at runtime instead of guessed.
- GPU discovery: `nvidia-smi` CSV plus a real one-second NVENC probe per card;
  the index is never hardcoded because it changes with the hardware config.
- Chunked parallel path with concat, plus a packet-count integrity check
  (container duration is not trustworthy on concatenated TS).
- CMAF packaging to HLS and DASH from one pass, with an HLS parser anchored on
  the `NAME=` attribute.
- Watermarking as filter fragments inside the encode pass: static, drifting
  dynamic, and a near-invisible second layer.
- VMAF with the distorted rendition upscaled to the reference size; mean, min,
  p99 and harmonic aggregates, with the basis (pooled vs recomputed) recorded.
- Job queue: FastAPI + Celery + RabbitMQ, Redis for progress and cooperative
  cancellation; byte-range delivery of manifests and segments (RFC 7233).
- CLI: `doctor`, `master`, `demo`, `bench`, `watermark`, `figures`,
  `job` - one entrypoint for everything, no Docker needed for the first six.
- Test suite for the pure logic (argument construction, parsers, range handling,
  job state mapping), with `ffmpeg` and `gpu` marked tests skipped on a
  GPU-less CI runner.
- Generated deterministic 4K master (`vodpipe master`) instead of a downloaded
  sample: reproducible, and no licensing question about the source.

### Measured
- Ladder of 5 rungs on 4K: 82.6 s wall chunked (63.4 s whole-file), VMAF 94.88 (2160p)
  to 59.63 (432p) on the chunked run.
- NVENC vs x264-medium: only 1.3-1.7x on this GPU, not the 10x folklore.
- Chunking on one GPU is strictly worse (2160p: 29.8 s whole, 35.3 s at 8x8) -
  the GPU is already saturated by one encode.
- Fused watermark: within measurement noise (every variant inside the baseline's
  own 2.9% spread), i.e. not measurably slower.
- Two-pass encode: +103.9% wall.
- Cancel at `encode 1/3`: status CANCELLED with the two finished rungs kept.

## 1.0.1 - hardening after a code review

A review of the finished tree reported 14 findings. Each was reproduced against
the repo's own binaries before anything was changed; three were real defects that
no test had caught, and each is now pinned by a test. The rest were either dead
code, duplication, or suggestions deliberately declined (noted at the bottom).

### Fixed
- **Filtergraph injection through watermark text.** `escape()` backslash-escaped
  `'` and the call site wrapped the result in single quotes - but inside a
  quoted filtergraph region a backslash is literal, so the quote still closed
  the value: `x',<filter>=...` appended filters to the encode (verified: a
  `scale=...,drawtext=...` payload ran to completion with rc 0). The escaping
  list was also missing `,`, `;` and `=`, which are graph and option separators.
  The correct rule came from measurement, not from documentation: each value was
  drawn three ways (`text=`, `textfile=` as the ground truth, and a payload that
  tries to add a filter) and the rendered frames compared. Quoting **and**
  backslash-escaping every graph/option metacharacter (`, ; = [ ] : '` and the
  escape character itself) is the spelling that parses, renders identically to
  `textfile=`, and cannot splice. Note that the shell close-and-reopen idiom
  (`'\''`) is wrong for a filtergraph - it leaves the colons unescaped, which
  breaks a Windows font path and any `id:99` overlay, and that regression is why
  the tests now run the shipped marks through the real binary. The API also
  validates watermark text against an allow-list.
- **`encoder=libx264` with the default preset was a guaranteed ffmpeg failure.**
  A single global preset default (`p5`, an NVENC spelling) reached libx264 and
  killed the documented GPU-absent fallback *after* the job was queued. Presets
  are now resolved per backend, and the API rejects a preset from the wrong
  vocabulary with a 422 before a worker is occupied.
- **Forced `-r <rung ceiling>` while the integrity check and VMAF used the source
  frame rate.** On a 24 fps master this reported a false frame-count failure and
  silently misaligned every VMAF frame pair (libvmaf pairs by index). Encode and
  both checks now share one commanded-rate basis via `encode.out_fps`.
  (No committed number moved: the 4K master is exactly 30 fps.)
- `ff.find()` accepted `VODPIPE_FFMPEG=<path>` but raised on the bare
  `VODPIPE_FFMPEG=ffmpeg` that CI itself sets.
- A missing pooled VMAF block reported the harmonic mean as a fabricated `0`; it
  is now recomputed and the basis is labelled (`vmaf_basis`).
- HLS and DASH disagreed on when the `aac_adtstoasc` bitstream filter applies.
- The M3 two-pass rival re-implemented rate control by hand, so the comparison
  was not apples-to-apples. It now reuses the same `rate_args`/`gop_args` as the
  fused path; re-measured, the second pass costs +103.9% (was +98.8%) and its
  bitrate comes down from 7308 to 7080 kbps because it no longer sets its own.

### Added
- `Runner.timeout_s`/`Runner.env`, so a bounded probe can no longer wedge a
  single-slot worker (Celery's task time limits are inert under `-P threads`).
  The GPU sweep now runs through `Runner` instead of calling `subprocess`
  directly, restoring the one-subprocess-place invariant.
- argv tests for encode/encoders (the tests whose absence let the two bugs
  above ship), a unified `ladder.select(strict=...)`, and shared `report.report_url`
  / `report.hls_url` so route strings have one owner.

### Removed
- `ssim/psnr` (never called, though `doctor` still probed the filters), the
  unused `make_logo`, `Health.ok_for`, `Runner.on_start`, legacy `logo_text` /
  `viewer_id` watermark keys, and duplicated `_clean`/`_state` helpers.
- Committed noise: two raw libvmaf dumps and a stale chunked artifact whose
  pooled block was empty (its zeros read like measurements).
- `vodpipe` no longer re-exports `ladder()`: on the package it shadowed the
  submodule of the same name, so `from vodpipe import ladder` returned the
  *function* and every `ladder.validate(...)` call failed with an AttributeError.

### Changed
- `mypy` is a real CI gate now (it ran with `continue-on-error`, so a type
  break was invisible).
- The queue E2E was re-run against the hardened code: `bench/m4_cancelled_report.json`
  is that run (job `fd69c5d723af` - CANCELLED, two finished rungs, ladder 44.09 s),
  and `bench/m4_report.json` is a successful one of the same shape (job `f0fbbb61404f`
  - 2 rungs with a watermark, 24.24 s submit-to-SUCCESS, HLS and DASH from one CMAF
  feed). Previously only the cancelled case had a committed artifact.

### Review suggestions deliberately declined
- Path traversal on `/media/{job_id}/{rest}` and `/jobs/{job_id}/report`: probed
  with `..%2f` and every variant 404s; `mediarange.is_inside`,
  `jobs.resolve_source` and `packager.safe_label` already do the containment.
- Renaming report schema keys - they are pinned by the committed artifacts and
  by `figures.py`, and the report is the project's public output.
- The deliberate `*_args` duplication of a few option strings: it is the
  decide/do split the whole test suite is built on.
