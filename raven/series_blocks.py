"""Compatibility module for :mod:`raven.recordings.blocks`."""

import sys
from .recordings import blocks as _implementation

sys.modules[__name__] = _implementation
