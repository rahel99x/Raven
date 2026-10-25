"""Compatibility facade for the engineering workbench's registered adapter."""

import sys
from .operation_adapters import engineering as _canonical

sys.modules[__name__] = _canonical
