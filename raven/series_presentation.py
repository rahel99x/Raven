"""Compatibility module for :mod:`raven.recordings.presentation`."""

import sys
from .recordings import presentation as _implementation

sys.modules[__name__] = _implementation
