"""Compatibility module for :mod:`raven.heavy.allocation_ownership`."""

import sys
from .heavy import allocation_ownership as _implementation

sys.modules[__name__] = _implementation
