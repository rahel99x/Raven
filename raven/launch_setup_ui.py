"""Compatibility alias for launching.setup_ui."""
import sys
from .launching import setup_ui as _implementation

sys.modules[__name__] = _implementation
