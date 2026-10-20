"""Compatibility module for :mod:`raven.jobs.batch_sort`."""

import sys
from .jobs import batch_sort as _implementation

sys.modules[__name__] = _implementation
