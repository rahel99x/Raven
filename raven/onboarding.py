"""Compatibility alias for entry.onboarding_cli."""
import sys
from .entry import onboarding_cli as _implementation

sys.modules[__name__] = _implementation
