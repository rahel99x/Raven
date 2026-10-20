"""Compatibility alias for launching.runtime_cache."""
import sys
from .launching import runtime_cache as _implementation

sys.modules[__name__] = _implementation
