"""Compatibility module for :mod:`raven.heavy.fork_task`."""

import sys
from .heavy import fork_task as _implementation

sys.modules[__name__] = _implementation
