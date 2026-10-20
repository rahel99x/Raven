"""Compatibility alias for launching.features."""
import sys
from .launching import features as _implementation

sys.modules[__name__] = _implementation
