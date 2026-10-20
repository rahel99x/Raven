"""Compatibility alias for :mod:`raven.terminal.broker`."""

from __future__ import annotations

import sys

from .terminal import broker as _implementation

if __name__ == "__main__":
    _implementation.main()
else:
    sys.modules[__name__] = _implementation
