"""Compatibility module for :mod:`raven.recordings.cli`."""

import sys
from .recordings import cli as _implementation

sys.modules[__name__] = _implementation
