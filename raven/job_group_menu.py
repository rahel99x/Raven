"""Compatibility module for :mod:`raven.jobs.group_menu`."""

import sys
from .jobs import group_menu as _implementation

sys.modules[__name__] = _implementation
