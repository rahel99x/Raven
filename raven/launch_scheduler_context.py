"""Compatibility alias for launching.scheduler_context."""
import sys
from .launching import scheduler_context as _implementation

sys.modules[__name__] = _implementation
