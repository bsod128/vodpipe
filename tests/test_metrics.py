"""Metric parsing and the filtergraph escaping that VMAF needs."""

from __future__ import annotations

from vodpipe import metrics

VMAF_LOG = {
    "pooled_metrics": {"vmaf": {"mean": 94.88, "harmonic_mean": 94.1}},
    "frames": [{"metrics": {"vmaf": 99.0}}, {"metrics": {"vmaf": 90.0}},
               {"metrics": {"vmaf": 55.5}}, {"metrics": {"vmaf": None}}],
}


def test_vmaf_prefers_pooled_numbers():
    out = metrics.parse_vmaf_json(VMAF_LOG)
    assert out["vmaf_mean"] == 94.88
    assert out["vmaf_harmonic"] == 94.1
    assert out["vmaf_min"] == 55.5


def test_vmaf_falls_back_to_per_frame_when_no_pooled_block():
    """Older libvmaf builds write `pooled.VMAF` or nothing at all; recomputing
    from frames keeps a run analysable instead of returning zeros."""
    out = metrics.parse_vmaf_json({"frames": VMAF_LOG["frames"]})
    assert out["vmaf_mean"] == round((99.0 + 90.0 + 55.5) / 3, 2)
    assert out["vmaf_min"] == 55.5


def test_vmaf_ignores_null_frame_values():
    out = metrics.parse_vmaf_json({"frames": [{"metrics": {"vmaf": None}}]})
    assert out["vmaf_mean"] == 0.0
    assert out["vmaf_min"] == 0.0


def test_percentile_uses_nearest_rank():
    """p99 is where ladder tuning lives: the mean hides the frames that look
    bad, the tail does not."""
    values = list(range(1, 101))
    assert metrics.percentile(values, 0.99) == 99
    assert metrics.percentile([10.0], 0.5) == 10.0
    assert metrics.percentile([], 0.9) == 0.0


def test_log_path_colon_is_escaped_for_the_filtergraph():
    r"""Inside `-lavfi`, `:` ends an option, so a drive colon must arrive at the
    graph level escaped: argv carries `\\:` (one literal backslash + colon)."""
    escaped = metrics.escape_log_path(r"C:\work\vmaf.json")
    assert escaped == "C\\\\:/work/vmaf.json"
    assert escaped.count("\\") == 2
    assert "\\" not in escaped.replace("\\\\", "")


def test_graph_upscales_the_distorted_rendition_to_the_reference():
    """Comparing a 1080p rung against a 1080p slice of the source measures the
    scaler, not the encoder: the distorted file goes up to the reference size."""
    graph = metrics.graph("3840x2160")
    assert "[0:v]scale=3840:2160" in graph
    assert "[1:v]format=yuv420p,setsar=1[r]" in graph
    assert graph.count("[d]") == 1 and graph.count("[r]") == 1


def test_graph_without_reference_size_stays_labelled():
    graph = metrics.graph(None)
    assert "[0:v]" in graph and "[1:v]" in graph


def test_vmaf_threads_are_capped():
    """libvmaf defaults to one thread (~1.8 fps on 4K). Capped at 8 it ran at
    ~13.7 fps measured, and cannot starve the encoder sharing the box."""
    assert 1 <= metrics.default_threads() <= 8


def test_bitrate_probe_separates_video_from_container():
    probe = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "bit_rate": "5000000",
             "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac", "bit_rate": "128000"},
        ],
        "format": {"bit_rate": "5140000"},
    }
    out = metrics.parse_bitrate_probe(probe)
    assert out["video_kbps"] == 5000
    assert out["audio_kbps"] == 128
    assert out["total_kbps"] == 5140
    assert out["resolution"] == "1920x1080"


def test_bitrate_probe_survives_a_missing_bit_rate():
    """A cancelled or truncated rung has no video stream at all; a crash here
    once took a whole job down (the metrics loop probed out=None)."""
    out = metrics.parse_bitrate_probe({"streams": [], "format": {}})
    assert out["video_kbps"] == 0
    assert out["total_kbps"] == 0
    assert out["resolution"] is None
    assert out["codec"] is None


def test_ts_video_bitrate_falls_back_to_the_container():
    """MPEG-TS has no per-stream video bit_rate - ffprobe answers N/A - so
    reading only that field reported 0 kbps for every rung of a real ladder."""
    probe = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "bit_rate": "N/A",
             "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac", "bit_rate": "130000"},
        ],
        "format": {"bit_rate": "6902000"},
    }
    out = metrics.parse_bitrate_probe(probe)
    assert out["video_kbps"] == (6_902_000 - 130_000) // 1000
    assert out["bitrate_source"] == "container"
    assert out["video_kbps"] > 0, "a rung that produced 20 MB is not 0 kbps"
