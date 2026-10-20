"""Compatibility identity for :mod:`raven.operation_adapters.scale`."""
import sys
from .operation_adapters import scale as _implementation

sys.modules[__name__] = _implementation
