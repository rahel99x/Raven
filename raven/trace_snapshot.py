"""Compatibility alias for tracing.snapshot."""
import sys
from .tracing import snapshot as _implementation

sys.modules[__name__] = _implementation
