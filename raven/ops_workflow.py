"""Compatibility identity for :mod:`raven.operation_adapters.workflow`."""

import sys
from .operation_adapters import workflow as _implementation

sys.modules[__name__] = _implementation
