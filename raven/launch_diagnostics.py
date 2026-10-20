"""Compatibility alias for launching.diagnostics."""
import sys
from .launching import diagnostics as _implementation

sys.modules[__name__] = _implementation
