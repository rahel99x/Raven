"""Compatibility identity for :mod:`raven.operation_adapters.runtime`."""
import sys
from .operation_adapters import runtime as _implementation

sys.modules[__name__] = _implementation
