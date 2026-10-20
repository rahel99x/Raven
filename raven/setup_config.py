"""Compatibility alias for configuration.allocation."""
import sys
from .configuration import allocation as _implementation

sys.modules[__name__] = _implementation
