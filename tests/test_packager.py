"""CMAF packaging: argv decisions and manifest parsing.

Everything here is pure - no ffmpeg. These are the bugs that cost hours when they
only appear at runtime:

  * `-var_stream_map` separators (space between variants, comma within one);
  * ADTS -> ASC bitstream filter for AAC coming out of MPEG-TS;
  * DASH attribute casing and where the media type lives;
  * ladder aspect-ratio compatibility, which the DASH muxer enforces.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from vodpipe import packager
from vodpipe.ladder import ladder, validate

RUNGS = [("2160p", "work/a.ts"), ("1080p", "work/b.ts"), ("432p", "work/c.mp4")]

MASTER = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-STREAM-INF:BANDWIDTH=32891926,AVERAGE-BANDWIDTH=32100000,CODECS="avc1.640033,mp4a.40.2",RESOLUTION=3840x2160,FRAME-RATE=30.000
stream_2160p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=7141530,AVERAGE-BANDWIDTH=7000000,CODECS="avc1.640032,mp4a.40.2",RESOLUTION=1920x1080,FRAME-RATE=30.000
stream_1080p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1569251,CODECS="avc1.64001F,mp4a.40.2",RESOLUTION=768x432
stream_432p.m3u8
"""

MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT24S">
  <Period>
    <AdaptationSet id="0" contentType="video" mimeType="video/mp4" segmentAlignment="start">
      <Representation id="0" bandwidth="32755940" codecs="avc1.640033" width="3840" height="2160"/>
      <Representation id="2" bandwidth="7005545" codecs="avc1.640032" width="1920" height="1080"/>
    </AdaptationSet>
    <AdaptationSet id="1" contentType="audio" mimeType="audio/mp4">
      <Representation id="1" bandwidth="129834" codecs="mp4a.40.2"/>
    </AdaptationSet>
  </Period>
</MPD>
"""


# --------------------------------------------------------------- argv shape


def test_var_stream_map_uses_spaces_between_variants_and_commas_within():
    var = packager.var_stream_map(RUNGS)
    assert var == "v:0,a:0,name:2160p v:1,a:1,name:1080p v:2,a:2,name:432p"
    assert ";" not in var, ("semicolons fail with 'Could not write header "
                            "(incorrect codec parameters ?): Invalid argument'")
    assert len(var.split(" ")) == len(RUNGS)


def test_hls_args_keep_copy_packaging_and_cmaf_segment_type():
    args = packager.hls_args(RUNGS, "/tmp/hls")
    assert "-c" in args and args[args.index("-c") + 1] == "copy"
    assert "-hls_segment_type" in args
    assert args[args.index("-hls_segment_type") + 1] == "fmp4"
    assert "independent_segments+omit_endlist" in args
    assert "-master_pl_name" in args
    # a filtergraph would make -c copy illegal, so packaging must never filter
    assert not any("filter" in a for a in args)


def test_one_input_and_two_maps_per_rung():
    args = packager.hls_args(RUNGS, "/tmp/hls")
    assert args.count("-i") == len(RUNGS)
    assert "-map" in args
    # audio mapped optionally so a silent master still packages
    assert "2:a:0?" in args


def test_adts_to_asc_only_when_input_is_mpegts():
    assert packager.bsf_args([("720p", "x.ts")]) == ["-bsf:a", "aac_adtstoasc"]
    assert packager.bsf_args([("720p", "x.mp4")]) == []


def test_hls_uses_the_bsf_because_the_ladder_is_ts():
    args = packager.hls_args([("1080p", "work/x.ts")], "/tmp/hls")
    assert "aac_adtstoasc" in args, ("without it: 'Malformed AAC bitstream detected'")


def test_labels_are_sanitized_for_file_names():
    assert packager.safe_label("1080p (mobile)") == "1080p_mobile"
    assert packager.safe_label("../evil") == "evil"
    assert packager.safe_label("###") == "v"


def test_windows_paths_are_forward_slashed_in_argv():
    rungs = [("1080p", "T:\\resume\\media\\work\\x.ts")]
    args = packager.hls_args(rungs, "T:\\resume\\media\\out")
    # args[0] is the resolved ffmpeg binary; that one may be a native path.
    joined = " ".join(args[1:])
    assert "\\" not in joined, "backslashes are escape chars to ffmpeg's parsers"
    assert "T:/resume/media/out/stream_%v.m3u8" in joined


# ------------------------------------------------------------ manifest read


def test_parse_hls_variants_reads_bandwidth_resolution_uri():
    variants = packager.parse_hls_variants(MASTER)
    assert len(variants) == 3
    assert variants[0]["bandwidth"] == 32891926
    assert variants[0]["resolution"] == "3840x2160"
    assert variants[0]["uri"] == "stream_2160p.m3u8"


def test_variant_without_resolution_is_skipped_not_misattributed():
    """An audio-only entry has no RESOLUTION; silently binding the next line's
    URI to the previous variant's resolution would corrupt the ladder report."""
    variants = packager.parse_hls_variants(MASTER + "#EXT-X-STREAM-INF:BANDWIDTH=129834\n")
    assert len(variants) == 3


def test_ladder_from_hls_is_sorted_by_delivered_bitrate():
    ladder_rows = packager.ladder_from_hls(MASTER)
    assert [r["total_kbps"] for r in ladder_rows] == [32891, 7141, 1569]


def test_dash_representations_inherit_media_type_from_adaptation_set():
    reps = packager.parse_dash_representations(MPD)
    video = [r for r in reps if r["media"] == "video"]
    audio = [r for r in reps if r["media"] == "audio"]
    assert len(video) == 2 and len(audio) == 1
    assert video[0]["bandwidth"] == 32755940
    assert (video[0]["width"], video[0]["height"]) == (3840, 2160)
    assert audio[0]["width"] is None, "audio has no dimensions"


def test_dash_parsing_is_case_insensitive():
    """ffmpeg writes lower-case attributes; the spec is case-insensitive."""
    upper = MPD.replace("bandwidth=", "bandwidth=").replace(
        '<AdaptationSet id="0" contentType="video"', '<AdaptationSet id="0" CONTENTTYPE="video"')
    reps = packager.parse_dash_representations(upper)
    assert [r["media"] for r in reps if r["media"] == "video"] == ["video", "video"]


def test_empty_manifests_parse_to_empty_lists():
    assert packager.parse_hls_variants("") == []
    assert packager.parse_dash_representations("") == []


# ------------------------------------------------------- ladder invariants


def test_reference_ladder_passes_its_own_invariants():
    assert validate(ladder()) == []


@pytest.mark.parametrize("width,height", [(854, 480), (640, 480)])
def test_classic_non_16_9_rungs_are_rejected(width, height):
    """The textbook "480p = 854x480" rung breaks DASH packaging:
    'Conflicting stream aspect ratios values in AdaptationSet'.

    Note 1024x576 *looks* like the same family but is exactly 16:9, so it
    passes - that is the invariant the ladder relies on.
    """
    from vodpipe.ladder import Rung

    rung = Rung("480p", height, width, 2_000_000, 2_400_000, 3_000_000)
    problems = validate([rung, Rung("2160p", 2160, 3840, 28_000_000, 34_000_000, 42_000_000)])
    assert any("not exactly 16:9" in p for p in problems)


def test_rung_1280x720_is_accepted():
    """1280x720 is exactly 16:9 and both sides are divisible by 8, so it passes:
    the check is 'exactly 16:9', not 'is one of my heights'."""
    from vodpipe.ladder import Rung

    rung = Rung("720p", 720, 1280, 3_200_000, 4_000_000, 5_000_000)
    assert validate([rung]) == []


def test_ladder_is_monotonically_decreasing_in_bitrate():
    rungs = ladder()
    assert all(a.bitrate > b.bitrate for a, b in pairwise(rungs))


def test_all_rungs_are_multiples_of_eight():
    for r in ladder():
        assert r.width % 8 == 0 and r.height % 8 == 0


def test_scale_filter_keeps_dimensions_even():
    """Odd widths break H.264 4:2:0 subsampling."""
    from vodpipe.ladder import Rung

    rung = Rung("auto", 540, 0, 4_000_000, 5_000_000, 6_000_000)
    spec = rung.scale(3840, 2160)
    w, h = spec.split("=")[1].split(":")[:2]
    assert int(w) % 2 == 0 and int(h) == 540
