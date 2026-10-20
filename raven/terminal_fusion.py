"""Compatibility alias for :mod:`raven.terminal.fusion`."""

from __future__ import annotations
import sys
from .terminal import fusion as _implementation

sys.modules[__name__] = _implementation
