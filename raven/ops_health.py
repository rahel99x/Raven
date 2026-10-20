"""Compatibility identity for :mod:`raven.operation_adapters.health`."""
import sys
from .operation_adapters import health as _implementation

sys.modules[__name__] = _implementation
