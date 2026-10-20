"""Compatibility alias for entry.command_options."""
import sys
from .entry import command_options as _implementation

sys.modules[__name__] = _implementation
