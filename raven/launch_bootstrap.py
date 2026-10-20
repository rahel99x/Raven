"""Compatibility alias for launching.bootstrap."""
import sys
from .launching import bootstrap as _implementation

sys.modules[__name__] = _implementation
