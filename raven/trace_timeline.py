"""Compatibility alias for tracing.timeline."""
import sys
from .tracing import timeline as _implementation

sys.modules[__name__] = _implementation
