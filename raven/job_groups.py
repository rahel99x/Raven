"""Compatibility module for :mod:`raven.jobs.group_domain`."""

import sys
from .jobs import group_domain as _implementation

sys.modules[__name__] = _implementation
