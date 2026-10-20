"""Compatibility alias for tracing.document."""
import sys
from .tracing import document as _implementation

sys.modules[__name__] = _implementation
