"""Compatibility alias for tracing.export."""
import sys
from .tracing import export as _implementation

sys.modules[__name__] = _implementation
