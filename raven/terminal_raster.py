"""Compatibility alias for :mod:`raven.terminal.raster`."""

from __future__ import annotations
import sys
from .terminal import raster as _implementation

sys.modules[__name__] = _implementation
