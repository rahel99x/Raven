"""Compatibility alias for launching.results."""
import sys
from .launching import results as _implementation

sys.modules[__name__] = _implementation
