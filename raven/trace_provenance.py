"""Compatibility alias for tracing.provenance."""
import sys
from .tracing import provenance as _implementation

sys.modules[__name__] = _implementation
