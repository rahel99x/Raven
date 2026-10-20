"""Compatibility module for :mod:`raven.heavy.allocation_reuse`."""
import sys
from .heavy import allocation_reuse as _implementation
sys.modules[__name__] = _implementation
