"""Compatibility alias for entry.qualification."""
import sys
from .entry import qualification as _implementation

sys.modules[__name__] = _implementation
