"""Compatibility module for :mod:`raven.jobs.panels`."""

import sys
from .jobs import panels as _implementation

sys.modules[__name__] = _implementation
