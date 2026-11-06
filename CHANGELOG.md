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
- NVENC vs x264-medium: only 1.4-1.7x on this GPU, not the 10x folklore.
- Chunking on one GPU gives no real speedup (2160p: 29.6 s whole, 36.3 s at 8x8) -
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
- ~~Path traversal on `/media/{job_id}/{rest}` and `/jobs/{job_id}/report`: probed
  with `..%2f` and every variant 404s; `mediarange.is_inside`,
  `jobs.resolve_source` and `packager.safe_label` already do the containment.~~
  **This call was wrong and 1.0.2 reverses it.** The probe only used the
  percent-encoding of a slash; the encoding of the dot does get through.
- Renaming report schema keys - they are pinned by the committed artifacts and
  by `figures.py`, and the report is the project's public output.
- The deliberate `*_args` duplication of a few option strings: it is the
  decide/do split the whole test suite is built on.

## 1.0.2 - the path-traversal finding we had declined

A second review round reported a path traversal on the two routes that put a
client-supplied id into a filesystem path. It reproduced on the first try, and it
served files that live outside every root the service knows about.

The decline in 1.0.1 was not lazy, it was under-tested: it probed `..%2f` and
`../`, both of which Starlette rejects while routing, so the routes looked sealed.
`%2e%2e` and `%5c` are not rejected - Starlette hands the handler the *decoded*
segment, so `..` and a backslash arrive already assembled. `os.path.join(OUT_ROOT,
job_id)` then walks out of the root before any containment check runs, and a second
flaw completes the job: an absolute segment (`C:` + `\`) makes the candidate and
the base the same path, so `is_inside(path, base)` - the check that was supposed to
catch this - is satisfied by *any* file on the machine, because every path is
inside itself.

What the shipped code served before this release, from the in-process test client:

- `/media/%2e%2e/leak_canary.ts` -> `200`, a file one level above the jobs root
- `/media/C%3a%5cWindows%5cwin.ini` -> `206`, `C:\Windows\win.ini`

The `206` is the part that makes it a streaming media route rather than a static
one: the range parser happily computed an end past EOF and answered with the first
`total` bytes of a file it was never pointed at.

### Fixed
- **Path traversal on `/media/{job_id}/{rest}` and `/jobs/{job_id}/report`.** The
  trust boundary moved to where the id enters the process: `jobs.validate_job_id()`
  accepts only `^[0-9a-f]{12}\Z`, the shape `uuid4().hex[:12]` produces, and
  `jobs.job_dir()` composes it with containment re-checked against the **root** -
  never against a value derived from the same untrusted input. Every job route
  shares one `_checked()` guard, so the rule cannot drift when a route is added.
  Containment checks were not the answer here and the docstring now says so: the
  id is validated, and `is_inside` stays as defence in depth (`mediarange.is_inside`
  itself was correct all along, including the cross-drive `ValueError` on Windows).
- **`/jobs/{id}` echoed the worker's exception text unredacted.** `FAILURE` payloads
  quote absolute host paths and sometimes the broker URL, and the endpoint returns
  them verbatim to anyone polling the id. `jobs.snapshot()` now runs that string
  through the same `ff.scrub()` the committed reports use.
- **Range semantics did not match the RFC we cited.** The module docstring pointed
  at RFC 7233; the rules actually needed are RFC 9110 section 14.2, and they were
  applied backwards in one place: an unparsable or unsupported range (an unknown
  unit, `items=0-1`, `bytes=abc-def`, a multi-range) is a header we are allowed to
  ignore - `An origin server MUST ignore a Range header field that contains a range
  unit it does not understand` - so it now serves the whole body with `200`. `416`
  is reserved for a *well-formed* `bytes=` range that cannot be satisfied. The
  wording was taken from the specification text, not from a summary of it.
- **Range delivery buffered the answer in the API process.** `bytes=0-` is the
  first request a player makes and its length is attacker-shaped, so `read(length)`
  put whole segments in memory on every open. `iter_range()` streams it in 1 MiB
  chunks. `FileResponse` was considered and rejected: it streams ranges well, but it
  answers `400` to a header it cannot parse, which would silently undo the lenient
  policy above - so the range this project resolves itself is now streamed by
  this project. Byte-exactness is tested against a real 3.7 MB segment.

### Added
- 21 tests for the above, replacing 6 that encoded the wrong Range contract
  (171 -> 186): a positive control that legitimate media and reports are still
  served (a security suite without one proves nothing), eight traversal vectors
  through `/media`, seven malformed ids reaching `/jobs/{id}` and
  `/jobs/{id}/cancel`, `/jobs/%2e%2e/report`, chunk-bounded range reads, and a
  scrubbed `FAILURE` payload.
- `jobs.JOB_ID_RE` / `validate_job_id()` / `job_dir()`: the id trust boundary has
  one owner shared by the API and the worker.

### Notes on method
- The traversal claim was verified by serving a file above the root and
  `C:\Windows\win.ini` from the app under test, and the fix was verified the same
  way: all nine vectors are now `404`. Reproducing a finding before fixing it is
  the rule; this release exists because the earlier round applied it to everything
  *except* the finding it declined.
- Version numbers are now consistent: `pyproject.toml` said 1.0.0 while
  `vodpipe.__version__` said 1.0.1.

## 1.0.3 - review round two, and a planner bug the benchmark exposed

A second review pass over the same tree, run after the security round so it could
not collide with it. It found no new security defect but nine hygiene problems,
and chasing one of them into `plan_chunks()` found a real bug in the parallel
planner - which then invalidated a committed benchmark artifact, so this release
re-measures M2.

### Fixed
- **A typo'd `--rungs` exited 0 without encoding anything.** `ladder.select()`
  ignored unknown labels, so `vodpipe ladder --rungs 2106p` "succeeded" with an
  empty ladder. Selection is now strict: an unknown label is an error and the CLI
  returns 2.
- **`plan_chunks()` could plan a 3x unbalanced split.** On the committed 4K
  master (24 s, GOP 3 s), `n=4` produced chunks of 3/6/6/9 s: the greedy
  "nearest keyframe to the ideal cut" rule has no balance guard, so the first
  bucket absorbed the only keyframe at 3 s and the last bucket ran to the end.
  Wall time is the longest chunk, so `c4w4` wasted workers. A skew guard
  (`_MAX_CHUNK_SKEW = 2.0`) falls back to a uniform grid when the keyframe plan is
  more than 2x lopsided; the same input now yields 6/6/6/6 s. Cut points stay on
  IDR frames only - the guard changes *which* balanced plan is chosen, not the
  rule.
- **`cmd_job` could poll forever.** The client printed progress with its own
  sleep loop and had no exit condition when a worker died. It now uses
  `Client.wait()` with `--timeout` (default 1800 s).
- **`--source` defaulted to a file that does not exist** in a fresh checkout.
  Corrected to the generated `src_clip/4k_master.mp4`.
- **The queued path ran no integrity check.** The CLI verified frame counts on
  concatenated rungs; the worker did not, so a truncated ladder could report
  `SUCCESS`. `pipeline.verify()` is now called in the task and the verdict ships
  in the report under `verify`.
- Core-only test run failed without Celery installed: one test imported
  `vodpipe.server.tasks` unconditionally. Now `pytest.importorskip("celery")`.

### Changed
- `bench/m2_bench.json` regenerated, because the committed artifact had measured
  the unbalanced planner. The headline conclusions did not change; three numbers
  and one claim did:
  - 2160p chunking: 29.6 s whole, 30.7 (2x2), 32.4 (4x4), 36.3 (8x8). The old
    artifact said 29.8 / 30.1 / 31.5 / 35.3.
  - NVENC vs x264-medium: 1.45x / 1.72x / 1.58x (was 1.58x / 1.74x / 1.64x);
    x264 walls scatter up to 15.5% run-to-run because they are CPU-bound, so
    these are read as "about 1.5x", not to two decimals.
  - The README used to say chunking "never wins, at any rung". That is too
    strong. At 720p, 2x2 is 5.72-5.86 s vs whole-file 5.86-6.08 s across three
    repeats - a 1-3% edge against a 3.8% run-to-run spread. It is a tie, not a
    win, and it reverses as workers increase. The README and the interview pack
    now say that.
  - `c4w4` at 2160p is the row the planner fix actually changed: per-chunk walls
    went from 29.7/29.6/18.8/31.3 s to 32.2/32.2/32.2/32.2 s. Wall got 2.9%
    *worse* (31.45 -> 32.36 s) because balance removed a lucky tail: the old
    plan's short chunk finished early and hid nothing. The honest number is the
    slower one.
- Worker logs go through `logging.getLogger("vodpipe.worker")` instead of
  `print()`, which is what makes them land in the Celery log.
- Report construction extracted into `tasks.build_payload()` so the report schema
  is testable without running an encode.

### Removed
- Dead public API that no caller, test or doc used: `JobCancelled`,
  `encode.on_chunk_done`, `ff.run`, `Rung.as_dict`, `Source.as_dict`, and the
  `pipeline.do_bitrates` / `expect_frames` parameters (the bitrate probe is
  unconditional and the frame expectation is derived, so both were knobs that
  only mislead if turned).

### Added
- `test_probe_chunks`: balance guard tests (unbalanced keyframes fall back to the
  grid; balanced keyframes are still used for cut points).
- `test_jobs`: a report-contract test pinning `verify` and the path-scrubbing of
  the payload the client and status page render.
- 189 tests total (188 pass, 1 skips without a GPU). Core-only run (no
  FastAPI/Celery/Redis installed): 156 passed, 4 skipped.

### Notes on method
- The M2 re-measurement was worth doing even though nothing "broke": a committed
  artifact is a claim, and after the planner fix the old one described a design
  the shipped code no longer produces. Its pre-fix copy is kept out of the tree
  for comparison.
- Run-to-run noise is now measured rather than asserted: three full repeats of
  the affected configs gave 0.8-1.3% spread on NVENC walls and 11.6-15.5% on
  x264 walls. That is what lets the README call the 720p 2x2 row a tie instead of
  a win or a loss.
