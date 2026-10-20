"""Compatibility alias for tracing.mpi."""
import sys
from .tracing import mpi as _implementation

sys.modules[__name__] = _implementation
