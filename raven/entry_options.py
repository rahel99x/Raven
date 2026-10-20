"""Compatibility alias for entry.grammar."""
import sys
from .entry import grammar as _implementation

sys.modules[__name__] = _implementation
