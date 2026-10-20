"""Compatibility module for :mod:`raven.recordings.cache_limits`."""

import sys
from .recordings import cache_limits as _implementation

sys.modules[__name__] = _implementation
