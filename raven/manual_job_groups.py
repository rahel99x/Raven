"""Compatibility module for :mod:`raven.jobs.manual_groups`."""

import sys
from .jobs import manual_groups as _implementation

sys.modules[__name__] = _implementation
