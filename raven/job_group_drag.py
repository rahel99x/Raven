"""Compatibility module for :mod:`raven.jobs.group_drag`."""

import sys
from .jobs import group_drag as _implementation

sys.modules[__name__] = _implementation
