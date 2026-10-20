"""Compatibility alias for launching.trace."""
import sys
from .launching import trace as _implementation

sys.modules[__name__] = _implementation
