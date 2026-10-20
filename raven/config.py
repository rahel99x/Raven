"""Compatibility alias for configuration.dashboard."""
import sys
from .configuration import dashboard as _implementation

sys.modules[__name__] = _implementation
