"""Compatibility alias for launching.wizard_ui."""
import sys
from .launching import wizard_ui as _implementation

sys.modules[__name__] = _implementation
