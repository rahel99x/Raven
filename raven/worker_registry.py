"""Compatibility alias for execution.registry."""
import sys
from .execution import registry as _implementation

sys.modules[__name__] = _implementation
