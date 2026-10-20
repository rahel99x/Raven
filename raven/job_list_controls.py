"""Compatibility module for :mod:`raven.jobs.list_controls`."""

import sys
from .jobs import list_controls as _implementation

sys.modules[__name__] = _implementation
