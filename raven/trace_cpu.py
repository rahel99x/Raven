"""Compatibility alias for tracing.cpu."""
import sys
from .tracing import cpu as _implementation

sys.modules[__name__] = _implementation
