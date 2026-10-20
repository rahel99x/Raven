"""Compatibility alias for tracing.native."""
import sys
from .tracing import native as _implementation

sys.modules[__name__] = _implementation
