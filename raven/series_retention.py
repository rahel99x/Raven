"""Compatibility module for :mod:`raven.recordings.retention`."""

import sys
from .recordings import retention as _implementation

sys.modules[__name__] = _implementation
