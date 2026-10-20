"""Compatibility identity for :mod:`raven.operation_adapters.campaign_policy`."""
import sys
from .operation_adapters import campaign_policy as _implementation

sys.modules[__name__] = _implementation
