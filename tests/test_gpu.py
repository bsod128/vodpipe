"""GPU discovery: CSV parsing, ranking, and why an index is never hardcoded."""

from __future__ import annotations

import pytest
from conftest import requires_gpu

from vodpipe import gpu

SMI = """4, NVIDIA GeForce RTX 3060, 512, 12288, 3
3, Tesla V100-SXM2-16GB, 15800, 16384, 93
2, Tesla V100-SXM2-16GB, 15900, 16384, 93
0, Tesla V100-SXM2-16GB, 300, 16384, 0
"""


def test_parse_smi_reads_the_csv():
    cards = gpu.parse_smi(SMI)
    assert [c["index"] for c in cards] == [4, 3, 2, 0]
    assert cards[0]["name"] == "NVIDIA GeForce RTX 3060"
    assert cards[0]["total"] - cards[0]["used"] == 11776


def test_parse_smi_skips_garbage_lines():
    """nvidia-smi prints a warning banner on some drivers; a row that does not
    parse must not take down discovery of the GPUs that do."""
    noisy = "nvml warning: something\n" + SMI + "bad,row\n"
    assert len(gpu.parse_smi(noisy)) == 4
    assert gpu.parse_smi("") == []


def test_consumer_gpu_outranks_a_datacenter_card_even_when_idle():
    """The consumer card's NVENC block is guaranteed present; datacenter V100s
    need the driver's NVENC patch and here they fail cuCtxCreate entirely.
    Probing the idle V100 first wastes seconds and logs a misleading error."""
    consumer = {"index": 4, "name": "NVIDIA GeForce RTX 3060", "used": 11000,
                "total": 12288, "util": 40}
    idle_v100 = {"index": 0, "name": "Tesla V100-SXM2-16GB", "used": 300,
                 "total": 16384, "util": 0}
    assert gpu.rank(consumer) < gpu.rank(idle_v100)


def test_within_a_class_free_vram_then_idle_decides():
    busy = {"index": 1, "name": "RTX 3060", "used": 11000, "total": 12288, "util": 10}
    free = {"index": 2, "name": "RTX 3060", "used": 200, "total": 12288, "util": 10}
    busy_but_free = {"index": 3, "name": "RTX 3060", "used": 200, "total": 12288, "util": 90}
    order = sorted([busy, free, busy_but_free], key=gpu.rank)
    assert [c["index"] for c in order] == [2, 3, 1]


def test_ranking_the_real_machine_picks_the_3060_first():
    """Regression for the day the GPU index changed with the hardware config:
    hardcoding index 0 pointed at a V100 whose NVENC cannot init."""
    order = sorted(gpu.parse_smi(SMI), key=gpu.rank)
    assert order[0]["index"] == 4


def test_probe_argv_uses_one_second_of_synthetic_video():
    argv = gpu.probe_gpu_args("h264_nvenc", 4)
    assert "testsrc2" in " ".join(argv)
    assert argv[argv.index("-c:v") + 1] == "h264_nvenc"
    assert "duration=1" in " ".join(argv)


def test_summary_names_the_picked_card_and_the_reasons():
    report = {"codec": "h264_nvenc", "picked": 4, "cards": [
        {"index": 4, "name": "RTX 3060", "used": 512, "total": 12288, "util": 3,
         "nvenc": True, "reason": "ok"},
        {"index": 0, "name": "V100", "used": 300, "total": 16384, "util": 0,
         "nvenc": False, "reason": "cuCtxCreate failed"},
    ]}
    text = gpu.summary(report)
    assert "CUDA_VISIBLE_DEVICES=4" in text
    assert "cuCtxCreate failed" in text
    assert "NVENC OK" in text and "NVENC FAIL" in text


def test_summary_says_so_when_nothing_can_encode():
    text = gpu.summary({"codec": "hevc_nvenc", "picked": None, "cards": []})
    assert "no card can encode" in text


@requires_gpu
def test_probe_returns_a_decision_and_a_reason_on_real_hardware():
    """Run the actual 1-second encode against every card this machine has.

    The assertion is not "it worked" - on a machine whose only GPU has no NVENC
    block it must fail - it is that a probe always terminates with a verdict and
    a human-readable reason. That is the property production depends on: a hung
    probe is what pins a single-slot worker, and "failed" without a reason is
    what turns a three-hour debugging session into a guess.
    """
    cards = gpu.gpus()
    if not cards:
        pytest.skip("no NVIDIA GPU on this machine")
    for card in cards:
        ok, reason = gpu.probe_index(card["index"])
        assert isinstance(ok, bool)
        assert reason, f"card {card['index']} failed without a reason"
        assert len(reason) <= 200
