"""Compatibility alias for tracing.stream."""
import sys
from .tracing import stream as _implementation

sys.modules[__name__] = _implementation
