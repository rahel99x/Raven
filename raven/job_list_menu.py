"""Compatibility module for :mod:`raven.jobs.list_menu`."""

import sys
from .jobs import list_menu as _implementation

sys.modules[__name__] = _implementation
