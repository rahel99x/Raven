"""Compatibility alias for :mod:`raven.terminal.emulator`."""

from __future__ import annotations

import sys

from .terminal import emulator as _implementation

sys.modules[__name__] = _implementation
