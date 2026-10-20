"""Compatibility alias for tracing.stream_export."""
import sys
from .tracing import stream_export as _implementation

sys.modules[__name__] = _implementation
