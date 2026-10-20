"""Compatibility identity for :mod:`raven.operation_adapters.process_profile`."""
import sys
from .operation_adapters import process_profile as _implementation

sys.modules[__name__] = _implementation
