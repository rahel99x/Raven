"""Compatibility module for :mod:`raven.jobs.group_ui`."""

import sys
from .jobs import group_ui as _implementation

sys.modules[__name__] = _implementation
