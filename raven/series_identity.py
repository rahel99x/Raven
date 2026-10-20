"""Compatibility module for :mod:`raven.recordings.identity`."""

import sys
from .recordings import identity as _implementation

sys.modules[__name__] = _implementation
