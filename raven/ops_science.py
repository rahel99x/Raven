"""Compatibility identity for :mod:`raven.operation_adapters.science`."""
import sys
from .operation_adapters import science as _implementation

sys.modules[__name__] = _implementation
