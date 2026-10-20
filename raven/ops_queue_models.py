"""Compatibility identity for :mod:`raven.operation_adapters.queue_models`."""
import sys
from .operation_adapters import queue_models as _implementation

sys.modules[__name__] = _implementation
