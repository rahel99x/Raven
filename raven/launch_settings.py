"""Compatibility alias for launching.settings."""
import sys
from .launching import settings as _implementation

sys.modules[__name__] = _implementation
