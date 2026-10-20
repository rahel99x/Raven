"""Compatibility alias for launching.io."""
import sys
from .launching import io as _implementation

sys.modules[__name__] = _implementation
