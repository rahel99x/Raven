"""Compatibility identity for :mod:`raven.operation_adapters.trace`."""
import sys
from .operation_adapters import trace as _implementation

sys.modules[__name__] = _implementation
