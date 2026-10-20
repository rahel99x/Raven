"""Compatibility module for :mod:`raven.jobs.selection`."""

import sys
from .jobs import selection as _implementation

sys.modules[__name__] = _implementation
