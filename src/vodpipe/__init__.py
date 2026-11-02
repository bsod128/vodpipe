"""vodpipe - a small, testable FFmpeg VOD pipeline.

The layout follows the "pure core / imperative shell" split:

* argument builders are plain functions returning an argv list. They never run
  anything, so they are unit-testable without ffmpeg installed or a GPU present;
* runners (`vodpipe.ff.Runner`) are the only place that touches subprocess.

Every stage therefore has two halves: `*_args()` (decide) and `*_run()` (do).
"""

from .ladder import Rung, ladder
from .pipeline import Pipeline

__all__ = ["Pipeline", "Rung", "ladder"]

__version__ = "1.0.0"
