"""Compatibility alias for tracing.capture."""
import sys
from .tracing import capture as _implementation

sys.modules[__name__] = _implementation
