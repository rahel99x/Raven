"""Compatibility module for :mod:`raven.collection.ledger`."""

import sys
from .collection import ledger as _implementation

sys.modules[__name__] = _implementation
