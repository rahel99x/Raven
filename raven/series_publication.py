"""Compatibility module for :mod:`raven.recordings.publication`."""

import sys
from .recordings import publication as _implementation

sys.modules[__name__] = _implementation
