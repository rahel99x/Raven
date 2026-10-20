"""Compatibility module for :mod:`raven.recordings.storage`."""

import sys
from .recordings import storage as _implementation

sys.modules[__name__] = _implementation
