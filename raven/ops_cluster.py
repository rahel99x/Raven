"""Compatibility identity for :mod:`raven.operation_adapters.cluster`."""
import sys
from .operation_adapters import cluster as _implementation

sys.modules[__name__] = _implementation
