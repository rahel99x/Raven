"""Compatibility alias for :mod:`raven.terminal.recording`."""

from __future__ import annotations

import sys

from .terminal import recording as _implementation

sys.modules[__name__] = _implementation
