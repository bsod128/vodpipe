# vodpipe

A VOD transcoding pipeline built around FFmpeg and NVENC: ladder encoding, CMAF
packaging (HLS + DASH) from one pass, watermarking, VMAF quality measurement, and
a job queue with cooperative cancellation.

Everything in the Results section is measured on real hardware and committed as a
machine-readable artifact under [`bench/`](bench). There are no estimates here.

```
4K master -> probe -> ladder (5 rungs, NVENC) -> concat + integrity check
                  -> CMAF packaging (HLS + DASH) -> VMAF / bitrate -> report
                                     ^
                       FastAPI + Celery + RabbitMQ/Redis
```

## Why this repo exists

I come from frontend/Angular and wanted a video-processing role, so instead of
claiming experience I built the thing and measured it. The interesting part is
not that it works - it is what the measurements said, because three of the four
answers contradicted what I had read on the internet:

| Claim often repeated | What I measured (RTX 3060, ffmpeg 7.1) |
|---|---|
| NVENC is ~10x faster than x264 | **1.4-1.7x** on these rungs. At 2160p NVENC is only 0.82x realtime |
| Split the file, encode in parallel, get a speedup | On one GPU it is **never a real win**: 2160p 29.6 s whole, 36.3 s at 8x8; 2x2 ties whole inside noise at 1080p/720p |
| Watermarking costs CPU time you should budget for | Fused into the encode pass it is **free**: every variant lands inside the baseline's own 2.9% run-to-run spread. A separate pass costs **+103.9%** |

The reason parallel chunking fails is that the NVENC block is a fixed unit on the
card: one 4K encode already saturates it, so N processes only add contention plus
concat cost. Chunking earns its keep in the other direction - it bounds memory
and lets a job be resumed per chunk - and that is how the code uses it.

## Quickstart

No Docker and no GPU needed for the first three commands.

```bash
./scripts/bootstrap.sh          # venv, package, ffmpeg check, doctor, master
.venv/bin/python -m vodpipe demo
```

Windows: `scripts\bootstrap.ps1`, then `.venv\Scripts\python -m vodpipe demo`.
With `make`: `make doctor master demo`.

`vodpipe` needs ffmpeg on `PATH`, or `ffmpeg.exe`/`ffprobe.exe` in [`bin/`](bin),
or `VODPIPE_FFMPEG` pointing at it. It never downloads a binary silently.

The source clip is **generated**, not downloaded (`vodpipe master`): a
deterministic 4K lavfi composite (seed 42, 2-second GOP). Public sample mirrors
are unreachable from several networks and carry licensing questions, so the
pipeline is reproducible from an empty checkout instead. VMAF on synthetic
content is not the same as on a film master; the ratios carry, the absolutes
belong to this source.

## Commands

| Command | What it does |
|---|---|
| `vodpipe doctor` | which ffmpeg, encoders, filters and GPUs this machine really has |
| `vodpipe master` | generate the deterministic 4K source clip |
| `vodpipe demo` | full pipeline on a chosen ladder, writes `bench/demo.json` |
| `vodpipe bench` | encoder/parallelism matrix (M2) |
| `vodpipe watermark` | watermark overhead matrix (M3) |
| `vodpipe figures` | SVG charts from `bench/*.json` into `docs/figures/` |
| `vodpipe job` | submit / poll / cancel against a running API |

Every command is a thin wrapper over the library: the same calls are available to
the queue worker, the tests and a notebook without going through argparse.

## Results

### M1 - ladder of 5 rungs from a 24 s 4K master

`h264_nvenc`, whole-file encode, wall time per rung; bitrate read back out of the
HLS master playlist (BANDWIDTH is audio+video, which is what a player sees):

| Rung | wall, s | speed | delivered bitrate | VMAF mean | VMAF min |
|---|---|---|---|---|---|
| 2160p | 29.8 | 0.82x | 31 656 kbps | 94.90 | 93.37 |
| 1440p | 14.0 | 1.78x | 12 319 kbps | 87.14 | 85.90 |
| 1080p | 9.3 | 2.71x | 7 485 kbps | 82.16 | 80.82 |
| 720p | 5.9 | 4.44x | 3 680 kbps | 76.12 | 73.57 |
| 432p | 4.5 | 5.89x | 1 625 kbps | 59.61 | 57.69 |

Ladder wall time: **63.5 s** for five rungs, VMAF from 94.90 down to 59.61.
Artifact: [`bench/demo.json`](bench) (`vodpipe demo`, whole-file encoding).

The same ladder through the chunked path (8 chunks, 4 workers) is kept as
[`bench/m1_h264_nvenc_chunk8.json`](bench): **80.9 s** ladder wall. Chunking cost
~17 s here and added a concat step - the M2 result again, at ladder scale. VMAF
mean per rung is identical to within 0.05, as it should be: splitting the work
must not change what is encoded, only how it is split. Every chunked rung
reports `vpkts=720/720` in the report's `verify` list, so the concat is checked
against the exact frame count, not against ffprobe's duration guess.

![M1 time and bitrate](docs/figures/m1_time_bitrate.svg)
![M1 VMAF](docs/figures/m1_vmaf.svg)

### M2 - encoder and parallelism matrix

Wall time per rung (lower is better). x264 at `medium`, NVENC whole-file, then
chunked with 2x2 / 4x4 / 8x8 workers:

| Rung | x264 medium | NVENC whole | 2x2 | 4x4 | 8x8 |
|---|---|---|---|---|---|
| 2160p | 42.9 s (0.57x) | **29.6 s (0.82x)** | 30.7 | 32.4 | 36.3 |
| 1080p | 16.4 s (1.49x) | 9.5 s (2.63x) | **9.4** | 10.1 | 12.1 |
| 720p | 10.8 s (2.28x) | 6.8 s (3.98x) | **5.9** | 6.2 | 8.0 |

Chunking does not give a meaningful speedup on one card. At 2160p it only costs
time (+3.9% at 2x2, up to +23% at 8x8). At 1080p and 720p the 2x2 row lands
inside measurement noise of whole-file: a 3-repeat run put 720p whole-file at
5.87-6.05 s and 2x2 at 5.84-5.91 s - a range that overlaps, and the 2x2 edge is
under whole-file's own 3.1% run-to-run spread. So at most it is a rounding-error
win, and it reverses as workers increase. The per-process throughput does go up,
which is why a benchmark that reports only "speed" would have called it a win:
the code reports `wall_s` and `agg_speed_x` separately, because wall time is what
a user waits for.

That noise is measured, not assumed: a 3-repeat run of this same matrix gives the
spread of the very numbers in the table above. NVENC wall times repeat to
0.1-3.1% across runs; x264 scatters up to 9.9% (it is CPU-bound, and the machine
is not idle). So every NVENC-vs-NVENC comparison below ~4% is read as a tie, and
this table is a single run, not an average - quoting it to one decimal is the
honest limit of what one machine can claim.

Artifacts: [`bench/m2_bench.json`](bench) (the table),
[`bench/m2_noise.json`](bench) (the 3-repeat spread behind the tie calls).

![M2 scaling](docs/figures/m2_scaling.svg)

### M3 - watermark cost

Same rung (1080p), same encoder, each variant repeated twice:

| Mode | wall, s | overhead | VMAF mean | VMAF min | kbps |
|---|---|---|---|---|---|
| none (baseline) | 9.23 | - | 82.16 | 80.82 | 6909 |
| static corner logo | 9.16 | **-0.8%** | 82.17 | 80.98 | 6916 |
| drifting per-viewer id | 9.13 | **-1.1%** | 81.92 | 80.57 | 6949 |
| + near-invisible second layer | 9.07 | **-1.7%** | 81.93 | 80.41 | 6922 |
| separate watermark pass | 18.82 | **+103.9%** | 81.85 | 80.76 | 7080 |

The baseline itself measured 9.37 s and 9.10 s across two runs - a 2.9% spread -
so every variant is repeated and averaged, and every fused variant lands inside
that spread. The negative overheads are that noise, not a speedup: a fused
watermark is not measurably slower than no watermark. Fused filters ride the
decode the encode already performs. A second pass decodes and re-encodes the
whole asset, so it doubles the cost, and it adds a generation of quality loss
(VMAF -0.31 against the source, on top of what the first pass already took).

The dynamic mark drifts in position and alpha as a function of `t`, so there is
no fixed pixel region to crop or box-blur out; the ghost layer runs at a
different phase, so cropping one mark still leaves the other in another frame
range. This is session identification, not a forensic watermark: it does not
survive a re-encode of the picture. Forensic marking needs a robust payload
spread across frames (DCT-band coefficients), which I have not built.

Artifact: [`bench/m3_watermark.json`](bench).

![M3 watermark](docs/figures/m3_watermark.svg)

### M4 - queue, progress, cancellation

Submit a 3-rung ladder and cancel it at `encode 1/3`:

```
$ vodpipe job --source src_clip/4k_master.mp4 --rungs 2160p,1440p,1080p --cancel-at 1/3
job: fd69c5d723af
  RUNNING p=0.01: gpu-probe
  RUNNING p=0.02: probe
  RUNNING p=0.233: encode 1/3
  -> cancel at encode 1/3
  CANCELLED p=1.0: cancelled
```

Result: status `CANCELLED`, the rungs finished before the cancel (`2160p` 30.07 s,
`1440p` 13.99 s) kept with their measured bitrates (29 123 and 11 564 kbps), the
third rung marked `cancelled`, ladder wall 44.09 s, report preserved. A cancelled
job is not a failed job: its finished output is still usable. Artifact:
[`bench/m4_cancelled_report.json`](bench).

A successful run of the same shape - 2 rungs, watermark on, no VMAF - finishes in
24.2 s from submit to `SUCCESS`, packages HLS and DASH from one CMAF feed (12
segments), and returns the URLs to read them: artifact
[`bench/m4_report.json`](bench). Fetching one of its variants with a `Range` header
comes back `206` with `Accept-Ranges: bytes` and the right `Content-Type`.

Two things had to be understood rather than guessed:

* **Celery's `revoke()` is wrong here.** It marks the task `REVOKED` *and wipes
  the stored result*, which throws away the rungs that finished successfully.
  Cancellation is instead a Redis flag checked between rungs, and a TTL so a
  stale flag can never cancel a later job.
* **Progress must be per rung, not per phase.** A five-rung ladder takes
  minutes; reporting only between phases means the client stares at one number
  the whole time. `on_rung_done` fires after every rung.

### M5 - one manifest with two codecs (HEVC primary, H.264 fallback)

`vodpipe demo --encoder hevc_nvenc --fallback h264_nvenc` encodes the same
five-rung ladder twice and packages **both** ladders into one master playlist, so
a client that cannot decode H.265 still finds a complete H.264 ladder instead of
nothing. Both codecs in one run: 10 HLS variants, 60 segments, 20 DASH
representations in 3 AdaptationSets (one per codec, plus audio).

| Rung | target | H.264 delivered | HEVC delivered | delta | H.264 VMAF | HEVC VMAF | Δ |
|---|---|---|---|---|---|---|---|
| 2160p | 28 000 | 31 656 kbps | 30 990 kbps | -2.1% | 94.90 | 94.76 | -0.14 |
| 1440p | 11 000 | 12 319 kbps | 12 105 kbps | -1.7% | 87.14 | 87.22 | +0.08 |
| 1080p | 6 500 | 7 485 kbps | 7 043 kbps | -5.9% | 82.16 | 82.20 | +0.04 |
| 720p | 3 200 | 3 680 kbps | 3 784 kbps | +2.8% | 76.12 | 76.16 | +0.04 |
| 432p | 1 400 | 1 625 kbps | 1 810 kbps | +11.4% | 59.61 | 59.72 | +0.11 |

Artifact: [`bench/m5_hevc_fallback.json`](bench). Ladder wall **128.7 s** for both
codecs (64.3 s HEVC + 64.4 s H.264, each summed over five rungs); per-rung wall
time and speed agree between the codecs to within ~1%, so HEVC is not measurably
slower than H.264 here.

What this run does **not** show is the "H.265 saves 40% at equal quality" figure,
and the reason is structural, not a missing measurement: the pipeline commands
every rung with an explicit `-b:v / -maxrate:v / -bufsize:v` triple, so both
codecs are pinned to the *same* target and the rate controller forces their
bitrates to agree. A ladder can only answer "quality at matched bitrate", and on
that axis the two codecs are indistinguishable on this source - every ΔVMAF is
≤ 0.14 on a 0-100 scale, i.e. three orders of magnitude below the differences the
ladder is designed to produce (94.90 at 2160p down to 59.61 at 432p). The delivered
bitrate delta is the two rate controllers filling the buffer differently, not
codec efficiency: its sign is inconsistent (HEVC below at 2160p/1080p, above at
720p/432p), which is the signature of noise, not of a win.

That the ΔVMAF column is tiny and not an artefact of run-to-run variation is
checkable: `demo.json` and this artifact measured the same `h264_nvenc` rungs in
two independent pipeline runs, and every VMAF mean and every delivered bitrate
reproduces **exactly** (94.90 / 31 656 kbps both times, all five rungs) - NVENC
whole-file encoding of the same source and settings is deterministic here, only
wall time drifts (29.8 s vs 30.2 s, ~1%). So the deltas in the table are real
differences between the codecs, and they are simply small on the axis a ladder can
measure.

Answering the equal-quality question needs the other axis - constant-quantizer
control, so the bitrate is free to move, with the two codecs matched on VMAF
rather than on QP (their QP scales are not comparable; `-rc constqp -cq N` is
measured to be ignored by these NVENC builds, and `-rc vbr -cq N -qmin N -qmax N`
is what actually pins the quantizer). That is a proper BD-rate measurement and I
have not run one to the standard the rest of this repo is set to, so it is stated
here as not measured. On this ladder the reason to ship HEVC is decoder coverage
and the fallback path, not a proven bitrate saving.

Two things had to be measured rather than read off the docs:

* **`-tag:v hvc1` does not survive MPEG-TS.** The pipeline encodes to TS and
  remuxes TS → MP4, and the MP4 handler rebuilds the stream description from the
  elementary stream, which resolves H.265 to `hev1` however the encoder was
  commanded. Measured consequence: with `hev1`, ffmpeg writes **no `CODECS=`
  attribute at all** for that HLS variant, so a client cannot tell the HEVC
  rendition from the H.264 one and never selects the fallback. `encoders.hevc_nvenc()`
  had been setting `tag="hvc1"` with a comment saying the fourcc matters for Apple
  and Smart TV - true, but it never reached the output. The tag is now re-asserted
  in `ts.normalize()` and read back out of the finished file (`ts.stream_tags`),
  because it is a property of the container a client parses, not of the encoder
  config.
* **Video and audio cannot share an AdaptationSet.** ffmpeg rejects it with
  "Codec type of stream 1 doesn't match AdaptationSet's media type", even though
  the DASH spec allows multiplexed sets. Splitting a mixed-codec ladder by codec
  family (`packager.codec_family`) gives one video set per codec plus one audio
  set, addressed by explicit output stream index. The index matters: `map_args`
  maps audio with `?`, so a silent rendition shifts every later output index and
  counting `2*i` silently points at the wrong stream.

## Design

**Pure core, imperative shell.** Every stage is split into a function that builds
the argv (no process, no GPU, unit-testable) and a runner that executes it:
`whole_args()` / `transcode_whole()`, `hls_args()` / `package_hls()`,
`vmaf_args()` / `vmaf()`. `ff.Runner(execute=..., timeout_s=..., env=...)` is the
only place a subprocess is created: tests substitute a fake `execute` that records
argv without running anything, and a bounded probe passes its own `timeout_s`.

That split is what makes the suite runnable on a GPU-less CI runner: the parts
that decide *what* to run are tested directly, the parts that run them carry the
`ffmpeg` / `gpu` markers and skip. The one exception is deliberate - a single
`ffmpeg`-marked test feeds a filtergraph-injection payload to the real binary,
because a unit test cannot prove that ffmpeg's *parser* agrees with our escaping,
and that assumption is exactly what used to be wrong.

| Module | Responsibility |
|---|---|
| `ff.py` | binary discovery, the one `Runner`, ffmpeg output parsers |
| `probe.py` | source metadata, `plan_chunks` (chunk boundaries on IDR frames only) |
| `ladder.py` | rung definition and the invariants a ladder must satisfy |
| `encoders.py` | per-encoder argv, option spellings resolved at runtime |
| `encode.py` | whole-file and chunked encode, parallel workers |
| `ts.py` | concat, and the packet-count integrity proof |
| `packager.py` | CMAF HLS + DASH in one pass, manifest parsing |
| `metrics.py` | VMAF (upscale methodology), measured bitrate |
| `watermark.py` | filter fragments and the escaping they need |
| `gpu.py` | card discovery and an actual NVENC probe per card |
| `pipeline.py` | orchestration, progress and cancellation hooks |
| `jobs.py` | queue state semantics, the job-id trust boundary |
| `mediarange.py` | RFC 9110 ranges and media MIME types |
| `server/` | FastAPI intake, Celery worker, byte-range delivery |

Five decisions I would defend in a review:

1. **Every rung is exactly 16:9 with dimensions divisible by 8.** The textbook
   "480p = 854x480" has DAR 427:240, and the DASH muxer then refuses the whole
   manifest ("Conflicting stream aspect ratios values in AdaptationSet"). The
   ladder uses 432p (768x432) instead. `ladder.validate()` rejects it up front.
2. **Chunk boundaries are IDR frames only.** Cutting mid-GOP produces a chunk
   whose first frame references frames that do not exist. `plan_chunks()` chooses
   from the keyframe list, and a test asserts contiguity and exact coverage.
   "Nearest keyframe to the ideal cut" is not enough on its own: on the 4K master
   it planned 3/6/6/9 s for 4 chunks, and wall time is the longest chunk. A skew
   guard falls back to a uniform grid when the keyframe plan is more than 2x
   lopsided, so the same input now gives 6/6/6/6 s. When the grid is used (too
   few keyframes, or the skew guard), its cuts land on whole frames, not even
   seconds: 24 s into 7 is 3.4286 s = 102.857 frames, and the partial-frame trims
   overshoot the concat to 723 packets, so the exact check below false-fails.
3. **Container duration is not trusted on concatenated TS.** Each chunk carries
   its own PCR, so ffprobe reports a duration that lies. Integrity is proven by
   counting video packets against the expected frame count (`ts.verify_concat`),
   with no tolerance - which is only sound because chunk cuts are frame-aligned.
4. **GPU index is never hardcoded.** The order changes with the hardware
   configuration - a hardcoded index 0 pointed at a datacenter card whose NVENC
   cannot even initialize. `gpu.pick()` ranks cards and then *probes* each with
   one second of real encoding, caching only successes.
5. **Option spellings are read, not guessed.** One build accepts `-spatial_aq`,
   another `-spatial-aq`; guessing wrong does not degrade quietly, it exits with
   "Unrecognized option". `ff.pick_option()` reads `ffmpeg -h encoder=<codec>`.

Bugs the tests caught, worth naming because they were silent:

* the HLS parser matched `BANDWIDTH=` and hit `AVERAGE-BANDWIDTH=` first, which
  ffmpeg emits *before* BANDWIDTH - every rung reported the wrong bitrate;
* a `-filters` regex assumed a 6-char flag column; ffmpeg 7.x prints 3, so every
  capability check silently returned "not available";
* `expected_video_frames` was called through the wrong module, raising
  `AttributeError` on the live path after every ladder.

And one the tests did *not* catch, which a review round did - because the earlier
round had declined the same finding on a weaker probe:

* **path traversal through a job id.** `/media/{job_id}/{rest}` joined the id into a
  path and then checked containment. A probe with `../` and `..%2f` 404s, so the
  route looked sealed. `%2e%2e` and `%5c` do not: the framework hands the handler the
  already-decoded segment, so `..` and a backslash arrive assembled, and `os.path.join`
  walks out of the root *before* the containment check runs. The check then made it
  worse: an absolute segment (`C:` + `\`) makes the candidate equal to the base, so
  "is this inside itself" is true for every file on the machine. It served a file from
  above the jobs root, and `C:\Windows\win.ini` with a `206`. The fix is not a stricter
  containment check but a validated id at the boundary (`^[0-9a-f]{12}\Z`, the shape
  `uuid4().hex[:12]` makes), with containment re-checked against the *root* - never
  against a value derived from the input being checked. One shared route guard, so a
  new route cannot forget it.

## Quality measurement

VMAF compares the **distorted rendition upscaled to the reference size** against
the full reference. Comparing a 1080p rung against a 1080p slice of the source
measures mostly the scaler. `n_threads=8` matters as much: libvmaf defaults to
one thread (~1.8 fps on 4K), and with 8 threads it ran at 13-14 fps across the rungs
(13.55 fps for 2160p). A metric
slower than the encode is a metric nobody runs on a catalogue.

Harmonic mean is the number quoted for a ladder because it punishes the bad
frames instead of averaging them away, and `vmaf_min` is reported per rung - the
mean hides exactly the frames that make a viewer quit.

## API

| Method | Purpose |
|---|---|
| `GET /health` | probes broker, result backend and queue depth separately |
| `POST /jobs` | 202 + `job_id`; source must live inside the media root |
| `GET /jobs/{id}` | state, progress, per-rung rows, artifacts |
| `POST /jobs/{id}/cancel` | cooperative cancel at the next rung boundary |
| `GET /jobs/{id}/report` | the run's JSON report |
| `GET /media/{job}/{path}` | manifests and segments, streamed byte ranges |

Job ids are validated at the route (`^[0-9a-f]{12}\Z`) before they reach a path or a
Redis key, and every job route shares that one guard. A `Range` header this server
cannot parse is ignored and the whole body is served with `200`; `416` is reserved
for a well-formed `bytes=` range past the end - the split RFC 9110 14.2 defines.

Queue depth is read with `queue_declare` straight from AMQP - declaring is
idempotent, so this needs no management API and no extra credentials.

Running it:

```bash
docker compose up -d rabbitmq redis       # infrastructure
python -m vodpipe.server.tasks            # worker on the host
python -m vodpipe.server.main             # API
```

On this machine the worker runs on the host because Docker Desktop cannot pass a
GPU into its WSL2 VM (`cuInit` -> `CUDA_ERROR_NO_DEVICE`). On a Linux server the
same image covers the worker: `docker compose --profile gpu up worker`. Celery on
Windows needs `-P threads` (no fork); concurrency is 1 either way, since one NVENC
session already saturates a card.

## Tests, lint, CI

```bash
pytest -q                 # 244 tests; ffmpeg/gpu marked tests skip without hardware
ruff check src tests
mypy
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs ruff and pytest on
Python 3.10 and 3.12 with ffmpeg installed and no GPU, which is exactly the split
the design is built around.

The tests concentrate on the parts that are invisible at a glance: argv
construction, ffmpeg's text output parsing, range handling, ladder invariants,
job state mapping, and the escaping rules that decide whether a filtergraph
parses at all.

## Honest limitations

* One RTX 3060. No multi-GPU measurement, and the chunking conclusion is only
  about a single card - across cards the picture is different.
* The source is synthetic. VMAF absolutes belong to it; ratios and the
  encoder/parallelism conclusions transfer, absolute quality numbers do not.
* Watermarking is session identification, not forensic marking.
* `hevc_nvenc` is benchmarked end to end and ships as a fallback ladder (M5), on
  the same 24 s synthetic master as everything else. `h264_qsv` is wired and
  verified to exist only - this machine has no Intel iGPU, so it has never
  produced a frame here.
* The HEVC/H.264 comparison is bitrate-at-matched-target (what the pipeline's
  rate control permits), not bitrate-at-matched-quality. The "H.265 saves 40%"
  figure is therefore neither confirmed nor refuted by this repo - see M5.
* No content-protection stack (no DRM, no AES-128 sample-AES in the manifests).
* The queue is proven on one host with one worker. No autoscaling, no sharding.

## License

MIT - see [LICENSE](LICENSE). Changes: [CHANGELOG.md](CHANGELOG.md).
