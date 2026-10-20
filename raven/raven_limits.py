"""Compatibility module for :mod:`raven.collection.retention`."""

import sys
from .collection import retention as _implementation

sys.modules[__name__] = _implementation
