"""Compatibility identity for :mod:`raven.operation_adapters.campaigns`."""
import sys
from .operation_adapters import campaigns as _implementation

sys.modules[__name__] = _implementation
