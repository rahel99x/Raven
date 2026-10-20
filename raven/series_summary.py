"""Compatibility module for :mod:`raven.recordings.summary`."""

import sys
from .recordings import summary as _implementation

sys.modules[__name__] = _implementation
