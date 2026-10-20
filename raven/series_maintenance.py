"""Compatibility module for :mod:`raven.recordings.maintenance`."""

import sys
from .recordings import maintenance as _implementation

sys.modules[__name__] = _implementation
