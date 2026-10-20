"""Compatibility alias for entry.onboarding_service."""
import sys
from .entry import onboarding_service as _implementation

sys.modules[__name__] = _implementation
