"""ABR ladder definition.

A ladder is a data structure, not a bag of magic numbers, so its invariants can
be asserted in tests. Two invariants are load-bearing and were learned the hard
way:

* **Every rung must be exactly 16:9 with both dimensions divisible by 8.** The
  DASH muxer refuses to write a manifest when rungs in one AdaptationSet have
  different display aspect ratios ("Conflicting stream aspect ratios values in
  AdaptationSet"). The textbook "480p = 854x480" has DAR 427:240, i.e. not 16:9,
  so the whole ladder fails to package. Hence 432p (768x432), 360p (640x360).
* **maxrate/bufsize are set per rung, not just bitrate.** Without them the
  encoder's rate control drifts away from the target, and the BANDWIDTH values
  in the master playlist stop matching the ladder you think you shipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

#: 16:9 reference used for the exact-aspect check below.
_ASPECT_W, _ASPECT_H = 16, 9


@dataclass(frozen=True)
class Rung:
    label: str
    height: int
    width: int
    bitrate: int      # target video bitrate, bit/s
    maxrate: int      # VBV peak
    bufsize: int      # VBV buffer, seconds worth of bits at maxrate
    devices: str = ""  # who this rung is for (documentation, ends up in reports)
    max_fps: int = 30
    audio: int = 128_000

    @property
    def kbps(self) -> int:
        return self.bitrate // 1000

    @property
    def aspect_ok(self) -> bool:
        """Exactly 16:9 (not "close to"): 854x480 is 2.77:1 and breaks DASH."""
        return self.width * _ASPECT_H == self.height * _ASPECT_W

    def scale(self, src_w: int | None = None, src_h: int | None = None) -> str:
        """Downscale filter preserving square pixels and even dimensions.

        Odd widths break H.264 (4:2:0 subsampling), so width is always even.
        """
        h = self.height
        w = self.width
        if not w and src_w and src_h:
            w = int(round(src_w * h / src_h / 2) * 2)
        return f"scale={w}:{h}:flags=lanczos"

def ladder(target_heights: list[int] | None = None) -> list[Rung]:
    """The reference 4K ladder.

    Bitrates are VBR targets chosen for a mixed film/TV catalogue: the top rung
    is deliberately generous (2160p is where a viewer judges quality), and each
    step down is roughly a 2x cut, which is what keeps ABR switching predictable
    on a bandwidth-constrained client.
    """
    rungs = [
        Rung("2160p", 2160, 3840, 28_000_000, 34_000_000, 42_000_000,
             devices="4K Smart TV (Tizen/webOS), desktop on fibre"),
        Rung("1440p", 1440, 2560, 11_000_000, 14_000_000, 17_000_000,
             devices="desktop, 4K panel below full rate"),
        Rung("1080p", 1080, 1920, 6_500_000, 8_000_000, 10_000_000,
             devices="Smart TV 1080, desktop, tablet on Wi-Fi"),
        Rung("720p", 720, 1280, 3_200_000, 4_000_000, 5_000_000,
             devices="mobile 4G, older Smart TV"),
        Rung("432p", 432, 768, 1_400_000, 1_800_000, 2_200_000,
             devices="mobile 3G, weak connections"),
    ]
    if target_heights:
        wanted = set(target_heights)
        rungs = [r for r in rungs if r.height in wanted]
    return sorted(rungs, key=lambda r: -r.height)


def select(labels: list[str] | None, rungs: list[Rung] | None = None,
           strict: bool = True) -> list[Rung]:
    """Filter a ladder by rung label, keeping the tallest-first order.

    One owner for "subset the ladder", because two copies had drifted apart: the
    queue path silently dropped an unknown label while the benchmark raised only
    when *nothing* matched, so `--rungs 1000p` quietly encoded a different set
    than was asked for. `strict` keeps the queue's lenient contract (it falls
    back to the default ladder) without re-implementing the selection.
    """
    rungs = rungs if rungs is not None else ladder()
    if not labels:
        return []
    wanted = set(labels)
    picked = sorted((r for r in rungs if r.label in wanted),
                    key=lambda r: -r.height)
    if strict:
        missing = wanted - {r.label for r in picked}
        if missing:
            known = ", ".join(sorted(r.label for r in rungs))
            raise ValueError(f"unknown rung(s) {sorted(missing)}; available: {known}")
    return picked


def validate(rungs: list[Rung]) -> list[str]:
    """Return human-readable ladder problems (empty list == ladder is sane)."""
    problems: list[str] = []
    for r in rungs:
        if not r.aspect_ok:
            problems.append(f"{r.label}: {r.width}x{r.height} is not exactly 16:9 "
                            f"(DASH will refuse the manifest)")
        if r.width % 8 or r.height % 8:
            problems.append(f"{r.label}: dimensions must be multiples of 8")
        if r.maxrate < r.bitrate:
            problems.append(f"{r.label}: maxrate < bitrate is not a usable VBV shape")
        if r.bufsize < r.maxrate:
            problems.append(f"{r.label}: bufsize < maxrate throttles peaks too hard")
    for a, b in pairwise(rungs):
        if a.bitrate <= b.bitrate:
            problems.append(f"{a.label} is not higher bitrate than {b.label}")
    return problems
