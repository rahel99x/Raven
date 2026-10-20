"""Compatibility alias for tracing.summary."""
import sys
from .tracing import summary as _implementation

sys.modules[__name__] = _implementation
