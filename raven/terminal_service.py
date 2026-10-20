"""Compatibility alias for :mod:`raven.terminal.service`."""

from __future__ import annotations

import sys

from .terminal import service as _implementation

sys.modules[__name__] = _implementation
