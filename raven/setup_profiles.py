"""Compatibility alias for configuration.profiles."""
import sys
from .configuration import profiles as _implementation

sys.modules[__name__] = _implementation
