"""Compatibility alias for configuration.policy."""
import sys
from .configuration import policy as _implementation

sys.modules[__name__] = _implementation
