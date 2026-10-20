"""Compatibility alias for execution.planning."""
import sys
from .execution import planning as _implementation

sys.modules[__name__] = _implementation
