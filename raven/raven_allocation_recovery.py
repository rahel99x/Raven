"""Compatibility module for :mod:`raven.heavy.allocation_recovery`."""

import sys
from .heavy import allocation_recovery as _implementation

sys.modules[__name__] = _implementation
