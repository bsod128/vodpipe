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
- VMAF with the distorted rendition upscaled to the reference size, plus SSIM and
  PSNR; p99/harmonic aggregates.
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
- Ladder of 5 rungs on 4K: 82.6 s wall, VMAF 94.88 (2160p) to 59.63 (432p).
- NVENC vs x264-medium: only 1.3-1.7x on this GPU, not the 10x folklore.
- Chunking on one GPU is strictly worse (2160p: 29.8 s whole, 35.3 s at 8x8) -
  the GPU is already saturated by one encode.
- Fused watermark: within measurement noise (+0.5% to +1.9%, against a baseline spread of 1.7%).
- Two-pass encode: +98.8% wall.
- Cancel at `encode 1/3`: status CANCELLED with the two finished rungs kept.
