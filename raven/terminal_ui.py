"""Compatibility alias for :mod:`raven.terminal.ui`."""

from __future__ import annotations
import sys
from .terminal import ui as _implementation

sys.modules[__name__] = _implementation
