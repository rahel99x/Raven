"""Compatibility module for :mod:`raven.jobs.copy`."""

import sys
from .jobs import copy as _implementation

sys.modules[__name__] = _implementation
