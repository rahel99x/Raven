"""Compatibility alias for :mod:`raven.terminal.sync`."""

from __future__ import annotations
import sys
from .terminal import sync as _implementation

sys.modules[__name__] = _implementation
