"""Compatibility identity for :mod:`raven.operation_adapters.platform`."""
import sys
from .operation_adapters import platform as _implementation

sys.modules[__name__] = _implementation
