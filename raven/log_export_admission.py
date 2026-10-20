"""Compatibility alias for execution.log_export."""
import sys
from .execution import log_export as _implementation

sys.modules[__name__] = _implementation
