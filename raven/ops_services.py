"""Compatibility identity for :mod:`raven.operation_adapters.services`."""
import sys
from .operation_adapters import services as _implementation

sys.modules[__name__] = _implementation
