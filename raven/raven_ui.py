"""Compatibility alias for ui.allocation."""
import sys
from .ui import allocation as _implementation

sys.modules[__name__] = _implementation
