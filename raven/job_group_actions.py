"""Compatibility module for :mod:`raven.jobs.group_actions`."""

import sys
from .jobs import group_actions as _implementation

sys.modules[__name__] = _implementation
