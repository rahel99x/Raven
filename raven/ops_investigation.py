"""Compatibility identity for :mod:`raven.operation_adapters.investigation`."""

import sys
from .operation_adapters import investigation as _implementation
from .operations import run, apply

# This released composition facade retains the former workflow forwarding API.
# Lower adapters import canonical peers and never depend on this binding.
_implementation.bind_legacy_workflow(_implementation.LegacyWorkflowActions(run, apply))

sys.modules[__name__] = _implementation
