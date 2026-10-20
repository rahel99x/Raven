"""Compatibility alias for :mod:`raven.terminal.workflows`."""

from __future__ import annotations
import sys

if __package__ in (None, ""):
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from raven.terminal import workflows as _implementation
else:
    from .terminal import workflows as _implementation
if __name__ == "__main__":
    raise SystemExit(
        _implementation._recipe_helper_main() if sys.argv[1:] == ["recipe-io"] else 2
    )
else:
    sys.modules[__name__] = _implementation
