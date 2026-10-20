"""Compatibility module for :mod:`raven.jobs.group_membership`."""

import sys
from .jobs import group_membership as _implementation

sys.modules[__name__] = _implementation
