"""Compatibility module for :mod:`raven.heavy.allocation_adapters`."""

import sys
from .heavy import allocation_adapters as _implementation

sys.modules[__name__] = _implementation
