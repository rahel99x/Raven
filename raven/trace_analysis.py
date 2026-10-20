"""Compatibility alias for tracing.analysis."""
import sys
from .tracing import analysis as _implementation

sys.modules[__name__] = _implementation
