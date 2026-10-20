"""Compatibility module for :mod:`raven.jobs.row_cache`."""

import sys
from .jobs import row_cache as _implementation

sys.modules[__name__] = _implementation
