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


def test_var_stream_map_counts_audio_streams_separately_from_video():
    """A silent rendition must not be handed an `a:` selector, and must not shift
    the audio ordinals of the renditions after it.

    Measured: `v:1,a:1` over `[audio, silent, audio]` makes ffmpeg resolve `a:1`
    as the *second audio stream*, which does not exist, and packaging aborts with
    "Unable to map stream at a:1" then "Could not write header (incorrect codec
    parameters ?)". HLS addresses each codec type with its own counter - unlike
    DASH, which selects by output stream index (`output_stream_index`).
    """
    rungs = [("2160p", "a.mp4"), ("1080p", "b.mp4"), ("720p", "c.mp4")]
    assert packager.var_stream_map(rungs, audio=[True, False, True]) == (
        "v:0,a:0,name:2160p v:1,name:1080p v:2,a:1,name:720p"
    )


def test_var_stream_map_for_an_all_silent_ladder_has_no_audio_at_all():
    rungs = [("2160p", "a.mp4"), ("1080p", "b.mp4")]
    assert packager.var_stream_map(rungs, audio=[False, False]) == (
        "v:0,name:2160p v:1,name:1080p"
    )


def test_var_stream_map_defaults_to_audio_on_every_rung():
    """The pre-mixed-codec behaviour, so single-codec packaging is untouched."""
    rungs = [("2160p", "a.mp4"), ("1080p", "b.mp4")]
    assert packager.var_stream_map(rungs) == packager.var_stream_map(rungs, [True, True])


def test_variant_label_is_the_inverse_of_the_variant_playlist_naming():
    """`package_hls` derives the file name from `safe_label(label)`, so this is
    how a report joins a manifest row back to the encode row that produced it -
    the only key that survives a ladder delivering 1080p under two codecs."""
    assert packager.variant_label("stream_1080p_h264_nvenc.m3u8") == "1080p_h264_nvenc"
    assert packager.variant_label("stream_1080p.m3u8") == "1080p"
    assert packager.variant_label("") == ""


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
    rungs = [("1080p", "D:\\media\\work\\x.ts")]
    args = packager.hls_args(rungs, "D:\\media\\out")
    # args[0] is the resolved ffmpeg binary; that one may be a native path.
    joined = " ".join(args[1:])
    assert "\\" not in joined, "backslashes are escape chars to ffmpeg's parsers"
    assert "D:/media/out/stream_%v.m3u8" in joined


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
    assert all(r["label"].startswith("avc1_") for r in ladder_rows)


MIXED_MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=9000000,CODECS="hvc1.1.4.L93.B01,mp4a.40.2",RESOLUTION=1920x1080
stream_1080p_hevc_nvenc.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=7141530,CODECS="avc1.640032,mp4a.40.2",RESOLUTION=1920x1080
stream_1080p_h264_nvenc.m3u8
"""


def test_ladder_from_hls_keeps_two_codecs_at_the_same_resolution():
    """The whole point of a fallback ladder: a client gets 1080p twice, once per
    codec. Keying the ladder by resolution alone would silently drop a rung."""
    rows = packager.ladder_from_hls(MIXED_MASTER)
    assert len(rows) == 2
    assert {r["label"] for r in rows} == {"hvc1_1920x1080", "avc1_1920x1080"}


def test_ladder_from_hls_labels_an_missing_codec_instead_of_raising():
    rows = packager.ladder_from_hls(
        "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360\nv.m3u8\n")
    assert rows[0]["label"] == "unknown_640x360"


def test_dash_representations_read_the_codec_of_each_representation():
    """Without this a mixed-codec ladder reads back as a single-codec one: the
    resolutions and segment counts are identical either way."""
    reps = packager.parse_dash_representations(MPD)
    assert [r["codecs"] for r in reps if r["media"] == "video"] == ["avc1.640033", "avc1.640032"]
    assert [r["codecs"] for r in reps if r["media"] == "audio"] == ["mp4a.40.2"]


# ------------------------------------------------------- DASH argv / codecs


def test_codec_family_groups_fourcc_spellings_that_mean_one_codec():
    """`hvc1` and `hev1` are the same codec in different container orders.

    Grouping by the raw fourcc would put them in separate AdaptationSets, which
    is legal but wasteful; grouping an `avc1` with an `hvc1` would be illegal.
    """
    assert packager.codec_family("avc1.640033") == "avc"
    assert packager.codec_family("hvc1") == packager.codec_family("hev1") == "hevc"
    assert packager.codec_family("av01.0.09M.08") == "av1"
    assert packager.codec_family("") == "unknown"


def test_codec_family_keeps_unknown_tags_in_their_own_set():
    """An unrecognised fourcc must not be folded into an AVC set."""
    assert packager.codec_family("vp09") == "vp09"


def test_output_stream_index_counts_only_mapped_streams():
    """A rendition with no audio contributes one output stream, not two."""
    audio = [True, True, True]
    assert packager.output_stream_index(0, audio) == (0, 1)
    assert packager.output_stream_index(2, audio) == (4, 5)
    # the middle rendition is silent: indices after it shift down by one
    audio = [True, False, True]
    assert packager.output_stream_index(1, audio) == (2, None)
    assert packager.output_stream_index(2, audio) == (3, 4)


def test_dash_adaptation_sets_single_family_is_the_shape_committed_artifacts_have():
    """Byte-identical to the pre-mixed-codec value: adding the codec-awareness
    must not churn every committed report."""
    rungs = [("2160p", "a.mp4"), ("1080p", "b.mp4")]
    assert packager.dash_adaptation_sets(rungs, None) == "id=0,streams=v id=1,streams=a"
    assert packager.dash_adaptation_sets(rungs, ["avc", "avc"]) == "id=0,streams=v id=1,streams=a"


def test_dash_adaptation_sets_splits_codecs_into_separate_sets():
    """One AdaptationSet carries one `codecs=`, so H.264 and HEVC cannot share one."""
    rungs = [("2160p_hevc_nvenc", "a.mp4"), ("1080p_hevc_nvenc", "b.mp4"),
             ("1080p_h264_nvenc", "c.mp4")]
    sets = packager.dash_adaptation_sets(rungs, ["hevc", "hevc", "avc"])
    assert sets == "id=0,streams=0,2 id=1,streams=4 id=2,streams=1,3,5"
    assert len(sets.split(" ")) == 3


def test_dash_adaptation_sets_never_mixes_video_and_audio_in_one_set():
    """Measured: audio in a video set fails with "Codec type of stream 1 doesn't
    match AdaptationSet's media type", even though the DASH spec allows it."""
    rungs = [("h", "a.mp4"), ("l", "b.mp4")]
    sets = packager.dash_adaptation_sets(rungs, ["hevc", "avc"], audio=[True, True])
    assert sets == "id=0,streams=0 id=1,streams=2 id=2,streams=1,3"
    for group in sets.split(" "):
        ids = [int(s) for s in group.split("streams=")[1].split(",")]
        assert all(i % 2 == 0 for i in ids) or all(i % 2 == 1 for i in ids), \
            "a set must hold one media type: even ids are video, odd are audio"


def test_dash_adaptation_sets_gives_a_silent_ladder_no_audio_set():
    rungs = [("h", "a.mp4"), ("l", "b.mp4")]
    assert packager.dash_adaptation_sets(rungs, ["hevc", "avc"],
                                         audio=[False, False]) == "id=0,streams=0 id=1,streams=1"


def test_dash_args_get_adaptation_sets_for_both_codecs():
    args = packager.dash_args([("2160p", "a.mp4"), ("1080p", "b.mp4")], "/tmp/dash",
                              families=["hevc", "avc"])
    i = args.index("-adaptation_sets")
    assert args[i + 1] == "id=0,streams=0 id=1,streams=2 id=2,streams=1,3"


def test_group_by_family_keeps_first_seen_order():
    assert packager.group_by_family(["avc", "hevc", "avc"]) == [("avc", [0, 2]), ("hevc", [1])]


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
