"""vodpipe - a small, testable FFmpeg VOD pipeline.

The layout follows the "pure core / imperative shell" split:

* argument builders are plain functions returning an argv list. They never run
  anything, so they are unit-testable without ffmpeg installed or a GPU present;
* runners (`vodpipe.ff.Runner`) are the only place that touches subprocess.

Every stage therefore has two halves: `*_args()` (decide) and `*_run()` (do).
"""

# Only `Rung` and `Pipeline` are re-exported. `ladder()` deliberately is *not*:
# rebinding the name here shadows the `vodpipe.ladder` submodule on the package,
# so `from vodpipe import ladder` would hand back a function and every
# `ladder.validate(...)` call would fail with an AttributeError. Import the
# factory from its module: `from vodpipe.ladder import ladder`.
from .ladder import Rung
from .pipeline import Pipeline

__all__ = ["Pipeline", "Rung"]

__version__ = "1.0.2"
