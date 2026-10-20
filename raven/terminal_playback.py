"""Compatibility alias for :mod:`raven.terminal.playback`."""

from __future__ import annotations
import sys

if __package__ in (None, ""):
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from raven.terminal import playback as _implementation
else:
    from .terminal import playback as _implementation
if __name__ == "__main__":
    raise SystemExit(_implementation.cli())
else:
    sys.modules[__name__] = _implementation
