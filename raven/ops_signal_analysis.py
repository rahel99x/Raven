"""Compatibility identity for :mod:`raven.operation_adapters.signal_analysis`."""
import sys
from .operation_adapters import signal_analysis as _implementation

sys.modules[__name__] = _implementation
