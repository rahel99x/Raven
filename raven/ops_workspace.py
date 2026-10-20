"""Compatibility identity for :mod:`raven.operation_adapters.workspace`."""
import sys
from .operation_adapters import workspace as _implementation

sys.modules[__name__] = _implementation
