"""Compatibility alias for tracing.browser."""
import sys
from .tracing import browser as _implementation

sys.modules[__name__] = _implementation
