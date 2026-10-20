"""Compatibility module for :mod:`raven.jobs.progress`."""

import sys
from .jobs import progress as _implementation

sys.modules[__name__] = _implementation
