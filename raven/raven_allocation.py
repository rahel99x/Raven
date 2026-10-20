"""Compatibility module for :mod:`raven.heavy.allocation`."""

import sys
from .heavy import allocation as _implementation

sys.modules[__name__] = _implementation
