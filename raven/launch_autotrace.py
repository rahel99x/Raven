"""Compatibility alias for launching.autotrace."""
import sys
from .launching import autotrace as _implementation

sys.modules[__name__] = _implementation
