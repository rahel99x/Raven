"""Compatibility alias for entry.launch_options."""
import sys
from .entry import launch_options as _implementation

sys.modules[__name__] = _implementation
