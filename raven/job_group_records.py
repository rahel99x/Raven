"""Compatibility module for :mod:`raven.jobs.group_records`."""

import sys
from .jobs import group_records as _implementation

sys.modules[__name__] = _implementation
