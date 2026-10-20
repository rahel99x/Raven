"""Compatibility module for :mod:`raven.jobs.identity`."""

import sys
from .jobs import identity as _implementation

sys.modules[__name__] = _implementation
