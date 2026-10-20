"""Compatibility alias for execution.admission."""

import sys
from .execution import admission as _implementation

sys.modules[__name__] = _implementation
