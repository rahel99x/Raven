"""Compatibility module for :mod:`raven.heavy.allocation_identity`."""

import sys
from .heavy import allocation_identity as _implementation

sys.modules[__name__] = _implementation
