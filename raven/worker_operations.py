"""Compatibility alias for execution.worker."""
import sys
from .execution import worker as _implementation

sys.modules[__name__] = _implementation
