"""Compatibility module for :mod:`raven.recordings.settings`."""

import sys
from .recordings import settings as _implementation

sys.modules[__name__] = _implementation
