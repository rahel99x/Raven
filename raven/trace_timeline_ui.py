"""Compatibility alias for tracing.timeline_ui."""
import sys
from .tracing import timeline_ui as _implementation

sys.modules[__name__] = _implementation
