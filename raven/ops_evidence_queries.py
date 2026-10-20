"""Compatibility identity for :mod:`raven.operation_adapters.evidence_queries`."""
import sys
from .operation_adapters import evidence_queries as _implementation

sys.modules[__name__] = _implementation
