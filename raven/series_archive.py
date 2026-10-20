"""Compatibility module for :mod:`raven.recordings.archive`."""

import sys
from .recordings import archive as _implementation

sys.modules[__name__] = _implementation
