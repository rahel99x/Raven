"""Compatibility alias for launching.wizard."""
import sys
from .launching import wizard as _implementation

sys.modules[__name__] = _implementation
