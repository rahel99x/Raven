"""Compatibility module for :mod:`raven.collection.diagnostics`."""

import sys
from .collection import diagnostics as _implementation

sys.modules[__name__] = _implementation
