"""Compatibility alias for tracing.cpu_timeline."""
import sys
from .tracing import cpu_timeline as _implementation

sys.modules[__name__] = _implementation
