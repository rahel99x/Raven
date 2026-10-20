"""Compatibility alias for ui.operations."""
import sys
from .ui import operations as _implementation

sys.modules[__name__] = _implementation
