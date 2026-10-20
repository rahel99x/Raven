"""Compatibility identity for :mod:`raven.operation_adapters.queue_order`."""
import sys
from .operation_adapters import queue_order as _implementation

sys.modules[__name__] = _implementation
