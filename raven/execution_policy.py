"""Compatibility alias for execution.policy."""

import sys
from .execution import policy as _implementation

sys.modules[__name__] = _implementation
