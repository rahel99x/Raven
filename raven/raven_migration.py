"""Compatibility alias and CLI for :mod:`raven.installation.config_migration`."""

from __future__ import annotations
import sys

if __package__ in (None, ""):
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from raven.installation import config_migration as _implementation
else:
    from .installation import config_migration as _implementation
if __name__ == "__main__":
    raise SystemExit(_implementation.cli())
else:
    sys.modules[__name__] = _implementation
