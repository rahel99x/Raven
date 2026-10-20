"""Compatibility module for :mod:`raven.heavy.protocol`."""

import sys
from .heavy import protocol as _implementation

sys.modules[__name__] = _implementation
