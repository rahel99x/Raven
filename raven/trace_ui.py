"""Compatibility alias for tracing.ui."""
import sys
from .tracing import ui as _implementation

sys.modules[__name__] = _implementation
