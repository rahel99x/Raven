"""Compatibility alias for tracing.protocol."""
import sys
from .tracing import protocol as _implementation

sys.modules[__name__] = _implementation
