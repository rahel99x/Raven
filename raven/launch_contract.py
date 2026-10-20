"""Compatibility alias for launching.contract."""
import sys
from .launching import contract as _implementation

sys.modules[__name__] = _implementation
